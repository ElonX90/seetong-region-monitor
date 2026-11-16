"""Local Seetong preview and person-count snapshots."""

import argparse
import ctypes as c
import importlib.util
import io
import json
import os
import queue
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from bisect import bisect_right
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
from PIL import Image, ImageDraw
from seetong_alerts import MobileAlerts, PushError, TOKEN, load_recipients
from seetong_event_share import EventShare, EventShareHandler
from seetong_recording import (DEFAULT_LIMITS, EVENT_NAME, NAME, SHOT_NAME,
                               Recording, RecordingStorage, playback_command)
from seetong_wine import WineCapture, native_capture

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "seetong-data"
SHOTS = DATA / "snapshots"
RECORDINGS = DATA / "recordings"
VAULT = DATA / ("account.dpapi" if os.name == "nt" else "account.json")
ALERT_VAULT = DATA / ("wxpusher.dpapi" if os.name == "nt" else "wxpusher.json")
SETTINGS = DATA / "regions.json"
STORAGE_SETTINGS = DATA / "storage.json"
MODEL = ROOT / "seetong-yolox.onnx"
WIDTH, HEIGHT = 640, 360
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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


def read_secret(path):
    if os.name == "nt":
        return protect(path.read_bytes(), decrypt=True).decode("utf-8")
    with path.open("rb") as source:
        if stat.S_IMODE(os.fstat(source.fileno()).st_mode) & 0o077:
            raise OSError(f"请将 {path.name} 的权限设为 600")
        return source.read().decode("utf-8")


def write_secret(path, value):
    data = value.encode("utf-8")
    if os.name == "nt":
        atomic_write(path, protect(data))
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as pending:
        temporary = Path(pending.name)
        try:
            pending.write(data)
            pending.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class PersonDetector:
    def __init__(self):
        import onnxruntime as ort

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
        return self.suppress_duplicates(boxes, scores[mask][keep])

    @staticmethod
    def suppress_duplicates(boxes, scores):
        order = np.argsort(scores)[::-1][:150]
        sizes = boxes[:, 2:] - boxes[:, :2]
        areas = sizes.prod(axis=1)
        center_x = (boxes[:, 0] + boxes[:, 2]) / 2
        picked = []
        while len(order):
            index, rest = order[0], order[1:]
            box = boxes[index]
            picked.append(box)
            overlap = np.maximum(0, np.minimum(box[2:], boxes[rest, 2:]) -
                                 np.maximum(box[:2], boxes[rest, :2]))
            intersection = overlap.prod(axis=1)
            union = areas[index] + areas[rest] - intersection
            # A box around part of the same person can have low IoU with the full box.
            # Require strong containment and near-aligned horizontal centers.
            smaller_width = np.minimum(sizes[index, 0], sizes[rest, 0])
            centered = np.abs(center_x[rest] - center_x[index]) <= 0.35 * smaller_width
            contained = intersection >= 0.8 * np.minimum(areas[index], areas[rest])
            duplicate = (intersection >= 0.5 * union) | (centered & contained)
            order = rest[~duplicate]
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
    def __init__(self, wine_capture=None, event_share=None):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.worker = None
        self.capture = wine_capture if wine_capture is not None else native_capture()
        self.channel = 1
        self.message = "就绪"
        self.frame = None
        self.sequence = 0
        self.count = None
        self.detect = False
        self.recording = False
        self.event_recording = False
        self.revision = 0
        self.regions = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
        limits = json.loads(STORAGE_SETTINGS.read_text(encoding="utf-8")) if STORAGE_SETTINGS.exists() else DEFAULT_LIMITS
        self.storage = RecordingStorage(RECORDINGS, SHOTS, limits)
        self.event_share = event_share
        self.alerts = MobileAlerts(event_share.link if event_share else None)
        if ALERT_VAULT.exists():
            try:
                saved = read_secret(ALERT_VAULT)
                self.alerts.configure(load_recipients(saved))
            except (OSError, ValueError, TypeError):
                self.alerts.last_result = "无法读取本机推送配置，请重新保存 SPT"

    def save_alert_devices(self, recipients):
        if recipients:
            write_secret(ALERT_VAULT, json.dumps([asdict(recipient) for recipient in recipients]))
        else:
            ALERT_VAULT.unlink(missing_ok=True)
        self.alerts.configure(recipients)

    def jpeg(self, image):
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=85)
        return encoded.getvalue()

    def stream(self, packets, sdk_done, recording=None):
        try:
            with recording if recording else nullcontext():
                self.decode(packets, sdk_done, recording)
        finally:
            if recording:
                with self.lock:
                    self.event_recording = False
        if recording and recording.error:
            raise RuntimeError(recording.error)

    def decode(self, packets, sdk_done, recording):
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
        env = None
        if recording:
            command.extend(recording.ffmpeg_args())
            env = {name: value for name, value in os.environ.items()
                   if name.lower() != "http_proxy"}
        player = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  env=env, creationflags=CREATE_NO_WINDOW)
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
                if recording:
                    if recording.error:
                        raise RuntimeError(recording.error)
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
                    if recording and recording.events:
                        recording.events.observe(bool(detect and region and stable.count), now, increase, jpeg)
                        self.event_recording = recording.events.file is not None
                    elif increase:
                        self.storage.snapshot(self.channel, increase, jpeg)
        finally:
            sdk_done.set()
            try:
                player.wait(timeout=3)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait()
            for thread in threads:
                thread.join(2)
            player.stdout.close()

    def start(self, credentials, channel, remember, record=False, detect=False):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("请先停止当前通道")
            self.channel, self.message = channel, "正在连接"
            self.frame, self.count, self.detect = None, None, detect
            self.recording = record
            self.stop.clear()

            def run():
                remember_pending = remember

                def consume(packets, done):
                    nonlocal remember_pending
                    if remember_pending and not self.stop.is_set():
                        with self.lock:
                            write_secret(VAULT, json.dumps(credentials))
                            remember_pending = False
                    recording = Recording(RECORDINGS, channel, continuous=record, events=True,
                                          on_event=self.alerts.enqueue, storage=self.storage)
                    self.stream(packets, done, recording)

                try:
                    self.run_capture(channel, credentials, consume)
                    with self.lock:
                        self.message = "已停止" if self.stop.is_set() else "连接结束，请检查账号、设备和网络"
                except Exception as exc:
                    with self.lock:
                        self.message = str(exc) if isinstance(exc, RuntimeError) and type(exc) is RuntimeError else "预览失败，请检查模型、FFmpeg 和设备连接"
                finally:
                    with self.lock:
                        self.detect, self.count, self.frame = False, None, None
                        self.recording = False
                        self.event_recording = False

            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()

    def run_capture(self, channel, credentials, consume):
        delay = 5
        while not self.stop.is_set():
            sequence = self.sequence
            try:
                self.capture.run(channel, credentials, consume, self.stop)
                reason = "视频连接结束"
            except (OSError, RuntimeError) as error:
                reason = str(error) if isinstance(error, RuntimeError) else "取流或录像发生系统错误"
            if self.stop.is_set():
                break
            if self.sequence != sequence:
                delay = 5
            with self.lock:
                self.frame = None
                self.count = None
                self.message = f"{reason}，{delay} 秒后重连"
            print(f"{datetime.now():%Y-%m-%d %H:%M:%S} 第 {channel} 路：{self.message}", flush=True)
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 60)


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
        url = urlsplit(self.path)
        route = url.path
        if self.headers.get("Host") != self.server.host:
            return self.reply({}, 403)
        if route == "/":
            html = (ROOT / "seetong-monitor.html").read_bytes().replace(
                b"__SESSION_TOKEN__", self.server.token.encode("ascii"))
            return self.reply(html, mime="text/html; charset=utf-8")
        if route.startswith(("/api/event/play/", "/api/event/download/",
                             "/api/recordings/play/", "/api/recordings/download/")):
            if url.query != "session=" + self.server.token:
                return self.reply({}, 403)
            return self.recording_video(route)
        if route.startswith("/api/shot/"):
            if (self.headers.get("X-Seetong-Session") != self.server.token
                    and url.query != "session=" + self.server.token):
                return self.reply({}, 403)
            name = route.rsplit("/", 1)[1]
            shot = SHOTS / name
            if SHOT_NAME.fullmatch(name) and not shot.is_symlink():
                try:
                    return self.reply(shot.read_bytes(), mime="image/jpeg")
                except FileNotFoundError:
                    pass
            return self.reply({}, 404)
        if self.headers.get("X-Seetong-Session") != self.server.token:
            return self.reply({}, 403)
        if route == "/api/events":
            return self.reply([path.name for path in sorted(RECORDINGS.glob("*-event.ts"), reverse=True)
                               if EVENT_NAME.fullmatch(path.name) and not path.is_symlink()])
        if route == "/api/recordings":
            return self.reply(self.recording_history())
        if route == "/api/event-cards":
            return self.reply(self.recording_history(events_only=True)[:12])
        monitor = self.server.monitor
        with monitor.lock:
            if route == "/api/state":
                storage = monitor.storage.status()
                return self.reply(dict(
                    saved=VAULT.exists(), channel=monitor.channel, message=monitor.message,
                    active=bool(monitor.worker and monitor.worker.is_alive()), detect=monitor.detect,
                    count=monitor.count, sequence=monitor.sequence if monitor.frame else 0,
                    recording=monitor.recording, recorded_bytes=sum(item["used"] for item in storage.values()),
                    storage=storage,
                    event_recording=monitor.event_recording,
                    alerts=monitor.alerts.status(),
                    event_share=monitor.event_share is not None,
                    regions=monitor.regions,
                ))
            if route == "/api/frame" and monitor.frame:
                return self.reply(monitor.frame, mime="image/jpeg")
        return self.reply({}, 404)

    def recording_history(self, events_only=False):
        entries = []
        events = {}
        legacy = {channel: [] for channel in range(1, 4)}
        for path in RECORDINGS.glob("*-event.ts" if events_only else "*.ts"):
            event = EVENT_NAME.fullmatch(path.name)
            match = event or NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                details = path.stat()
            except FileNotFoundError:
                continue
            if details.st_size:
                timestamp, channel = match.groups()
                channel = int(channel)
                saved = details.st_mtime_ns // 1_000_000
                entry = {"name": path.name, "type": "event" if event else "continuous",
                         "channel": channel, "size": details.st_size,
                         "time": int(timestamp) // 1_000_000 if event else saved}
                entries.append(entry)
                if event:
                    entry.update(shots=[], saved=saved)
                    events[channel, timestamp] = entry
                    legacy[channel].append(entry)
        for channel_entries in legacy.values():
            channel_entries.sort(key=lambda entry: entry["time"])
        for path in sorted(SHOTS.glob("*.jpg")):
            match = SHOT_NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            timestamp, channel, previous, count, event_id = match.groups()
            try:
                at = int(datetime.strptime(timestamp, "%Y%m%d-%H%M%S-%f").timestamp() * 1000)
            except ValueError:
                continue
            channel = int(channel)
            entry = events.get((channel, event_id))
            if event_id is None:
                candidates = legacy[channel]
                index = bisect_right(candidates, at + 1000, key=lambda item: item["time"]) - 1
                if index >= 0 and at <= candidates[index]["saved"] + 1000:
                    entry = candidates[index]
            if entry is None:
                start = int(event_id) // 1_000_000 if event_id else at
                entry = {"name": None, "type": "event", "channel": channel, "size": 0,
                         "time": start, "shots": []}
                entries.append(entry)
                if event_id:
                    events[channel, event_id] = entry
            entry["shots"].append({"name": path.name, "time": at, "previous": int(previous), "count": int(count)})
        return sorted(entries, key=lambda entry: entry["time"], reverse=True)

    def recording_video(self, route):
        name = route.rsplit("/", 1)[1]
        path = RECORDINGS / name
        match = EVENT_NAME.fullmatch(name) or NAME.fullmatch(name)
        if not match or path.is_symlink() or not path.is_file():
            return self.reply({}, 404)
        if route.startswith(("/api/event/download/", "/api/recordings/download/")):
            try:
                with path.open("rb") as source:
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    shutil.copyfileobj(source, self.wfile)
            except ConnectionError:
                pass
            except FileNotFoundError:
                self.reply({}, 404)
            return

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return self.reply({}, 503)
        # The stored stream can be HEVC. Convert only while playing in the browser.
        with subprocess.Popen(playback_command(ffmpeg, path), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW) as player:
            try:
                initial = player.stdout.read1(64 * 1024)
                if not initial:
                    return self.reply({}, 422)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(initial)
                shutil.copyfileobj(player.stdout, self.wfile)
            except ConnectionError:
                pass
            finally:
                if player.poll() is None:
                    player.kill()

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
            if route == "/api/alerts/test":
                link = None
                if data.get("event") is True:
                    if not monitor.event_share:
                        raise ValueError("请先配置手机可访问的事件回看地址")
                    link = monitor.event_share.latest_link()
                    if not link:
                        raise ValueError("请先保存一次区域事件录像")
                monitor.alerts.test(data.get("id"), link)
                return self.reply({"ok": True})
            with monitor.lock:
                if route == "/api/alerts/configure":
                    token = data.get("spt")
                    if not isinstance(token, str) or not TOKEN.fullmatch(token.strip()):
                        raise ValueError("请输入有效的 WxPusher SPT")
                    name = data.get("name", "")
                    if not isinstance(name, str) or len(name.strip()) > 40:
                        raise ValueError("设备名称最多 40 字")
                    monitor.save_alert_devices(monitor.alerts.updated(name.strip(), token.strip()))
                elif route == "/api/alerts/unbind":
                    monitor.save_alert_devices(monitor.alerts.without(data.get("id")))
                elif route == "/api/alerts/disable":
                    monitor.save_alert_devices([])
                elif route == "/api/storage":
                    limits = {}
                    for kind in DEFAULT_LIMITS:
                        amount = data.get(kind)
                        if type(amount) not in (int, float) or not 0.01 <= amount <= 10000:
                            raise ValueError("请输入 0.01 到 10000 GB 的容量上限")
                        limits[kind] = round(amount * 1_000_000_000)
                    monitor.storage.set_limits(limits, lambda value: atomic_write(STORAGE_SETTINGS, json.dumps(value).encode("utf-8")))
                elif route == "/api/start":
                    username, password = data.get("username"), data.get("password")
                    if not username and not password and VAULT.exists():
                        username, password = json.loads(read_secret(VAULT))
                    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                        raise ValueError("请输入完整的云平台账号和密码")
                    channel = data.get("channel")
                    if type(channel) is not int or channel not in (1, 2, 3):
                        raise ValueError("无效通道")
                    monitor.start((username.strip(), password), channel,
                                  data.get("remember") is True, data.get("record") is True)
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
                    if enabled and importlib.util.find_spec("onnxruntime") is None:
                        raise ValueError("本机缺少 ONNX Runtime，请先安装检测依赖")
                    monitor.detect = enabled
                    monitor.revision += 1
                else:
                    return self.reply({}, 404)
            self.reply({"ok": True, "alerts": monitor.alerts.status()} if route.startswith("/api/alerts/") else {"ok": True})
        except PushError as exc:
            self.reply({"error": str(exc)}, 502)
        except (ValueError, TypeError, AttributeError) as exc:
            self.reply({"error": str(exc) if type(exc) is ValueError else "请求无效"}, 400)
        except OSError:
            self.reply({"error": "无法读取或保存本机设置"}, 500)


def main():
    global DATA, SHOTS, RECORDINGS, VAULT, ALERT_VAULT, SETTINGS, STORAGE_SETTINGS
    parser = argparse.ArgumentParser(description="Seetong 区域监控")
    parser.add_argument("--wine-python", type=Path, help="Windows x64 python.exe 的 Linux 路径")
    parser.add_argument("--wine-diagnostics", action="store_true", help="输出取流进程的诊断信息")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--autostart", type=int, choices=(1, 2, 3), metavar="通道")
    parser.add_argument("--port", type=int, default=8765, metavar="PORT")
    parser.add_argument("--share-base-url", help="手机访问事件的 HTTPS 站点地址")
    parser.add_argument("--share-port", type=int, default=18766, metavar="PORT",
                        help="事件回看服务的本机端口，默认 18766")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    if args.share_base_url and (not 1 <= args.share_port <= 65535 or args.share_port == args.port):
        parser.error("事件回看端口必须有效且与监控端口不同")
    if os.name != "nt" and not args.wine_python:
        parser.error("Linux 运行需要 --wine-python")
    if os.name == "nt" and args.wine_python:
        parser.error("Windows 运行使用本机 SDK")
    try:
        capture = WineCapture(args.wine_python, diagnostics=args.wine_diagnostics) if args.wine_python else None
    except ValueError as error:
        parser.error(str(error))
    DATA = args.data_dir.resolve()
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    SHOTS, RECORDINGS = DATA / SHOTS.name, DATA / RECORDINGS.name
    VAULT, ALERT_VAULT = DATA / VAULT.name, DATA / ALERT_VAULT.name
    SETTINGS, STORAGE_SETTINGS = DATA / SETTINGS.name, DATA / STORAGE_SETTINGS.name
    SHOTS.mkdir(exist_ok=True)
    share = None
    if args.share_base_url:
        key_file = DATA / "event-share.key"
        try:
            saved_key = key_file.exists()
            key = bytes.fromhex(read_secret(key_file)) if saved_key else secrets.token_bytes(32)
            share = EventShare(args.share_base_url, key, RECORDINGS, SHOTS)
            if not saved_key:
                write_secret(key_file, key.hex())
        except ValueError as error:
            parser.error(str(error))
        except OSError:
            parser.error("无法读取或保存事件回看密钥")
    monitor = Monitor(capture, share)
    share_context = (ThreadingHTTPServer(("127.0.0.1", args.share_port), EventShareHandler)
                     if share else nullcontext())
    with share_context as share_server, ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.host = f"127.0.0.1:{server.server_port}"
        server.token = secrets.token_hex(32)
        server.monitor = monitor
        if args.autostart and VAULT.is_file():
            try:
                credentials = json.loads(read_secret(VAULT))
                if (not isinstance(credentials, list) or len(credentials) != 2 or
                        not all(isinstance(value, str) and value for value in credentials)):
                    raise ValueError
            except (OSError, ValueError):
                parser.error("无法读取已记住的账号，请重新保存账号")
            detect = (str(args.autostart) in monitor.regions and MODEL.is_file()
                      and importlib.util.find_spec("onnxruntime") is not None)
            monitor.start(credentials, args.autostart, remember=False, record=True, detect=detect)
        url = "http://" + server.host
        print(url, flush=True)
        if not args.no_browser and os.name == "nt":
            webbrowser.open(url)

        def shutdown(_signum, _frame):
            raise KeyboardInterrupt

        if os.name != "nt":
            signal.signal(signal.SIGTERM, shutdown)
        if share_server:
            share_server.share = share
            threading.Thread(target=share_server.serve_forever, name="seetong-event-share", daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            monitor.stop.set()
            if monitor.worker:
                monitor.worker.join(15)
        finally:
            if share_server:
                share_server.shutdown()


if __name__ == "__main__":
    main()
