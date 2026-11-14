"""Local Seetong preview and person-count snapshots."""

import ctypes as c
import importlib.util
import io
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "seetong-data"
SHOTS = DATA / "snapshots"
VAULT = DATA / "account.dpapi"
SETTINGS = DATA / "regions.json"
MODEL = ROOT / "seetong-yolox.onnx"
WIDTH, HEIGHT = 640, 360
RETENTION = 100

spec = importlib.util.spec_from_file_location("seetong_probe", ROOT / "seetong-login-probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Blob(c.Structure):
    _fields_ = [("size", c.c_ulong), ("data", c.c_void_p)]


def protect(data, decrypt=False):
    buffer = c.create_string_buffer(data)
    source = Blob(len(data), c.cast(buffer, c.c_void_p))
    result = Blob()
    crypt = c.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [c.POINTER(Blob), c.c_void_p, c.c_void_p, c.c_void_p,
                         c.c_void_p, c.c_ulong, c.POINTER(Blob)]
    if not function(c.byref(source), None, None, None, None, 1, c.byref(result)):
        raise c.WinError(c.get_last_error())
    try:
        return c.string_at(result.data, result.size)
    finally:
        free = c.windll.kernel32.LocalFree
        free.argtypes = [c.c_void_p]
        free.restype = c.c_void_p
        free(result.data)


def atomic_write(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)


class PersonDetector:
    def __init__(self):
        options = ort.SessionOptions()
        options.intra_op_num_threads = min(4, os.cpu_count() or 1)
        self.model = ort.InferenceSession(str(MODEL), options, providers=["CPUExecutionProvider"])
        self.input = self.model.get_inputs()[0].name
        grids = []
        scales = []
        for stride in (8, 16, 32):
            side = 640 // stride
            x, y = np.meshgrid(np.arange(side), np.arange(side))
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            scales.extend([stride] * side ** 2)
        self.grid = np.concatenate(grids)
        self.strides = np.asarray(scales)[:, None]

    def boxes(self, image):
        tensor = np.full((640, 640, 3), 114, dtype=np.float32)
        tensor[:HEIGHT] = np.asarray(image)
        result = self.model.run(None, {self.input: tensor.transpose(2, 0, 1)[None]})[0][0]
        scores = result[:, 4] * result[:, 5]
        mask = (scores >= 0.40) & (np.argmax(result[:, 5:], axis=1) == 0)
        centers = (result[mask, :2] + self.grid[mask]) * self.strides[mask]
        sizes = np.exp(np.clip(result[mask, 2:4], -10, 10)) * self.strides[mask]
        boxes = np.column_stack((centers - sizes / 2, centers + sizes / 2))
        boxes[:, (0, 2)] = np.clip(boxes[:, (0, 2)], 0, WIDTH - 1)
        boxes[:, (1, 3)] = np.clip(boxes[:, (1, 3)], 0, HEIGHT - 1)
        keep = (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1])
        boxes = boxes[keep]
        order = np.argsort(scores[mask][keep])[::-1][:150]
        picked = []
        while len(order):
            index, rest = order[0], order[1:]
            box = boxes[index]
            picked.append(box)
            intersection = np.maximum(0, np.minimum(box[2:], boxes[rest, 2:]) -
                                      np.maximum(box[:2], boxes[rest, :2])).prod(axis=1)
            union = (box[2:] - box[:2]).prod() + (boxes[rest, 2:] - boxes[rest, :2]).prod(axis=1)
            order = rest[intersection / union < 0.5]
        return picked


class StableCount:
    def __init__(self):
        self.count = 0
        self.candidate = 0
        self.since = 0

    def observe(self, number, now):
        if number != self.candidate:
            self.candidate, self.since = number, now
        if number == self.count or now - self.since < 1:
            return None
        previous, self.count = self.count, number
        return (previous, number) if number > previous else None


class Monitor:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.channel = 1
        self.message = "就绪"
        self.frame = None
        self.sequence = 0
        self.count = None
        self.detect = False
        self.revision = 0
        self.regions = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}

    def jpeg(self, image):
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=85)
        return encoded.getvalue()

    def stream(self, packets, sdk_done):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 FFmpeg")
        size = WIDTH * HEIGHT * 3
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-probesize", "65536",
            "-analyzeduration", "500000", "-threads", "1", "-i", "pipe:0", "-an",
            "-vf", f"fps=5,scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
            "-pix_fmt", "rgb24", "-threads", "1", "-flush_packets", "1",
            "-f", "rawvideo", "pipe:1",
        ]
        player = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        latest = queue.Queue(maxsize=1)

        def feed():
            try:
                with player.stdin:
                    while not sdk_done.is_set() and not self.stop.is_set():
                        try:
                            packet = packets.get(timeout=0.2)
                        except queue.Empty:
                            continue
                        player.stdin.write(packet)
                        player.stdin.flush()
            except OSError:
                pass

        def read():
            while True:
                raw = player.stdout.read(size)
                if len(raw) != size:
                    return
                try:
                    latest.get_nowait()
                except queue.Empty:
                    pass
                latest.put_nowait(raw)

        threads = [threading.Thread(target=fn, daemon=True) for fn in (feed, read)]
        for thread in threads:
            thread.start()
        stable = StableCount()
        revision = -1
        detector = None
        last_frame = time.monotonic()
        try:
            while not self.stop.is_set() and not sdk_done.is_set():
                try:
                    raw = latest.get(timeout=0.2)
                except queue.Empty:
                    if player.poll() is not None or time.monotonic() - last_frame > 30:
                        raise RuntimeError("视频解码中断或等待画面超时")
                    continue
                now = last_frame = time.monotonic()
                image = Image.frombytes("RGB", (WIDTH, HEIGHT), raw)
                with self.lock:
                    detect, current_revision = self.detect, self.revision
                    region = self.regions.get(str(self.channel))
                if current_revision != revision:
                    stable, revision = StableCount(), current_revision
                increase = None
                if detect and region:
                    detector = detector or PersonDetector()
                    x0, y0, x1, y1 = region
                    visible = [box for box in detector.boxes(image)
                               if x0 <= (box[0] + box[2]) / (2 * WIDTH) <= x1
                               and y0 <= box[3] / HEIGHT <= y1]
                    increase = stable.observe(len(visible), now)
                    overlay = ImageDraw.Draw(image)
                    for box in visible:
                        overlay.rectangle(tuple(map(int, box)), outline="#69e5ab", width=2)
                jpeg = self.jpeg(image)
                with self.lock:
                    self.frame = jpeg
                    self.sequence += 1
                    if current_revision != self.revision:
                        continue
                    self.count = stable.count if detect else None
                    self.message = "检测中" if detect else "预览中"
                    if increase:
                        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                        filename = f"{timestamp}-ch{self.channel}-{increase[0]}-{increase[1]}.jpg"
                        atomic_write(SHOTS / filename, jpeg)
                        for old in sorted(SHOTS.glob("*.jpg"), reverse=True)[RETENTION:]:
                            old.unlink(missing_ok=True)
        finally:
            sdk_done.set()
            if player.poll() is None:
                player.kill()
            player.wait()
            for thread in threads:
                thread.join(2)
            player.stdout.close()

    def start(self, credentials, channel, remember):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("请先停止当前通道")
            self.channel, self.message = channel, "正在连接"
            self.frame, self.count, self.detect = None, None, False
            self.stop.clear()

            def run():
                def consume(packets, done):
                    if remember and not self.stop.is_set():
                        with self.lock:
                            atomic_write(VAULT, protect(json.dumps(credentials).encode("utf-8")))
                    self.stream(packets, done)

                try:
                    probe.main(["--capture", "--channel", str(channel)],
                               credentials=credentials, consumer=consume)
                    with self.lock:
                        self.message = "已停止" if self.stop.is_set() else "连接结束，请检查账号、设备和网络"
                except Exception as exc:
                    with self.lock:
                        self.message = str(exc) if isinstance(exc, RuntimeError) and type(exc) is RuntimeError else "预览失败，请检查模型、FFmpeg 和设备连接"
                finally:
                    with self.lock:
                        self.detect, self.count, self.frame = False, None, None

            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, value, status=200, mime="application/json; charset=utf-8"):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' blob:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        route = urlsplit(self.path).path
        if self.headers.get("Host") != self.server.host:
            return self.reply({}, 403)
        if route == "/":
            html = (ROOT / "seetong-monitor.html").read_bytes().replace(
                b"__SESSION_TOKEN__", self.server.token.encode("ascii"))
            return self.reply(html, mime="text/html; charset=utf-8")
        if self.headers.get("X-Seetong-Session") != self.server.token:
            return self.reply({}, 403)
        monitor = self.server.monitor
        with monitor.lock:
            if route == "/api/state":
                return self.reply(dict(
                    saved=VAULT.exists(), channel=monitor.channel, message=monitor.message,
                    active=bool(monitor.worker and monitor.worker.is_alive()), detect=monitor.detect,
                    count=monitor.count, sequence=monitor.sequence if monitor.frame else 0,
                    regions=monitor.regions,
                    shots=[p.name for p in sorted(SHOTS.glob("*.jpg"), reverse=True)],
                ))
            if route == "/api/frame" and monitor.frame:
                return self.reply(monitor.frame, mime="image/jpeg")
            if re.fullmatch(r"/api/shot/\d{8}-\d{6}-\d{6}-ch[1-3]-\d+-\d+\.jpg", route):
                shot = SHOTS / route.rsplit("/", 1)[1]
                if shot.is_file():
                    return self.reply(shot.read_bytes(), mime="image/jpeg")
        return self.reply({}, 404)

    def do_POST(self):
        if (self.headers.get("Host") != self.server.host or
                self.headers.get("Origin") != "http://" + self.server.host or
                self.headers.get("X-Seetong-Session") != self.server.token):
            return self.reply({}, 403)
        monitor = self.server.monitor
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 8192:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(length))
            route = self.path
            with monitor.lock:
                if route == "/api/start":
                    username, password = data.get("username"), data.get("password")
                    if not username and not password and VAULT.exists():
                        username, password = json.loads(protect(VAULT.read_bytes(), decrypt=True))
                    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                        raise ValueError("请输入完整的云平台账号和密码")
                    channel = data.get("channel")
                    if type(channel) is not int or channel not in (1, 2, 3):
                        raise ValueError("无效通道")
                    monitor.start((username.strip(), password), channel, data.get("remember") is True)
                elif route == "/api/forget":
                    VAULT.unlink(missing_ok=True)
                elif route == "/api/stop":
                    monitor.stop.set()
                    monitor.message = "正在停止"
                elif route == "/api/region":
                    if not monitor.frame:
                        raise ValueError("请先打开实时画面")
                    points = data.get("region")
                    if (not isinstance(points, list) or len(points) != 4
                            or any(type(point) not in (int, float) or not 0 <= point <= 1 for point in points)
                            or points[2] - points[0] < .02 or points[3] - points[1] < .02):
                        raise ValueError("请拖出一个有效区域")
                    updated = monitor.regions | {str(monitor.channel): points}
                    atomic_write(SETTINGS, json.dumps(updated).encode("utf-8"))
                    monitor.regions = updated
                    monitor.revision += 1
                elif route == "/api/detect":
                    enabled = data.get("enabled") is True
                    if enabled and (not monitor.frame or str(monitor.channel) not in monitor.regions):
                        raise ValueError("请先预览并框选区域")
                    if enabled and not MODEL.is_file():
                        raise ValueError("本机缺少人体检测模型")
                    monitor.detect = enabled
                    monitor.revision += 1
                else:
                    return self.reply({}, 404)
            self.reply({"ok": True})
        except (ValueError, TypeError, AttributeError) as exc:
            self.reply({"error": str(exc) if type(exc) is ValueError else "请求无效"}, 400)
        except OSError:
            self.reply({"error": "无法读取或保存本机设置"}, 500)


if __name__ == "__main__":
    SHOTS.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(("127.0.0.1", 8765), Handler) as server:
        server.host = "127.0.0.1:8765"
        server.token = secrets.token_hex(32)
        server.monitor = Monitor()
        url = "http://" + server.host
        print(url, flush=True)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.monitor.stop.set()
            if server.monitor.worker:
                server.monitor.worker.join(10)
