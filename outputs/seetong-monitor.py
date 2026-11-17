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
from collections import deque
from bisect import bisect_right
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np
from PIL import Image, ImageDraw
from seetong_alerts import MobileAlerts, PushError, TOKEN, load_recipients
from seetong_event_share import EventShare, EventShareHandler, PlaybackCache, serve_recorded_video
from seetong_recording import (DEFAULT_LIMITS, EVENT_NAME, NAME, SHOT_NAME,
                               Recording, RecordingStorage, recording_duration)
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
MODELS = {"nano": ROOT / "seetong-yolox-nano.onnx", "original": MODEL}
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
    def __init__(self, model_key="original"):
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        # Frames arrive intermittently. Park inference threads between runs.
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.model = ort.InferenceSession(str(MODELS[model_key]), options, providers=["CPUExecutionProvider"])
        model_input = self.model.get_inputs()[0]
        self.input = model_input.name
        self.nano = model_key == "nano"
        self.side = 416 if self.nano else 640
        if model_input.shape != [1, 3, self.side, self.side]:
            raise ValueError("人体检测模型的输入尺寸不匹配")
        self.ratio = min(self.side / WIDTH, self.side / HEIGHT)
        grids = []
        scales = []
        for stride in (8, 16, 32):
            side = self.side // stride
            x, y = np.meshgrid(np.arange(side), np.arange(side))
            grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
            scales.extend([stride] * side ** 2)
        self.grid = np.concatenate(grids)
        self.strides = np.asarray(scales)[:, None]

    def boxes(self, image):
        tensor = np.full((self.side, self.side, 3), 114, dtype=np.float32)
        if self.nano:
            scaled = np.asarray(image.resize((int(WIDTH * self.ratio), int(HEIGHT * self.ratio)),
                                             Image.Resampling.BILINEAR))
            tensor[:scaled.shape[0], :scaled.shape[1]] = scaled[:, :, ::-1]
        else:
            tensor[:HEIGHT] = np.asarray(image)
        result = self.model.run(None, {self.input: tensor.transpose(2, 0, 1)[None]})[0][0]
        scores = result[:, 4] * result[:, 5]
        mask = (scores >= 0.40) & (np.argmax(result[:, 5:], axis=1) == 0)
        centers = (result[mask, :2] + self.grid[mask]) * self.strides[mask]
        sizes = np.exp(np.clip(result[mask, 2:4], -10, 10)) * self.strides[mask]
        boxes = np.column_stack((centers - sizes / 2, centers + sizes / 2)) / self.ratio
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


def region_vertices(region):
    if len(region) == 4 and type(region[0]) in (int, float):
        x0, y0, x1, y1 = region
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return region


def cross(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def on_edge(a, b, p):
    return (abs(cross(a, b, p)) <= 1e-10
            and all(min(s, t) - 1e-10 <= q <= max(s, t) + 1e-10 for s, t, q in zip(a, b, p)))


def valid_region(region):
    def coordinate(value):
        return type(value) in (int, float) and 0 <= value <= 1

    if not isinstance(region, list):
        return False
    if len(region) == 4 and all(map(coordinate, region)):
        return region[2] - region[0] >= .02 and region[3] - region[1] >= .02
    if not 3 <= len(region) <= 32 or not all(
            isinstance(point, list) and len(point) == 2 and all(map(coordinate, point)) for point in region):
        return False
    if len({tuple(point) for point in region}) != len(region):
        return False
    edges = list(zip(region, region[1:] + region[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            c, d = edges[j]
            if (on_edge(a, b, c) or on_edge(a, b, d) or on_edge(c, d, a) or on_edge(c, d, b)
                    or (cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0)):
                return False
    return abs(sum(a[0]*b[1] - a[1]*b[0] for a, b in edges)) >= .0008


def in_region(vertices, x, y):
    inside = False
    a = vertices[-1]
    for b in vertices:
        if on_edge(a, b, (x, y)):
            return True
        if (a[1] > y) != (b[1] > y) and x < a[0] + (y-a[1]) * (b[0]-a[0]) / (b[1]-a[1]):
            inside = not inside
        a = b
    return inside


def box_inside_regions(box, inclusion, exclusion):
    x = (box[0] + box[2]) / (2 * WIDTH)
    y = box[3] / HEIGHT
    return in_region(inclusion, x, y) and (not exclusion or not in_region(exclusion, x, y))


class StableCount:
    RISE_SECONDS = 1.0
    FALL_SECONDS = 2.0
    CLEAR_SECONDS = 5.0
    MAX_SAMPLE_GAP = 2.0

    def __init__(self):
        self.count = 0
        self.candidate = 0
        self.since = 0
        self.last_sample = None

    def observe(self, number, now, *, continuous=None):
        if self.last_sample is not None and continuous is None:
            continuous = now - self.last_sample <= self.MAX_SAMPLE_GAP
        if number != self.candidate or self.last_sample is None or not continuous:
            self.candidate, self.since = number, now
        self.last_sample = now
        delay = (self.CLEAR_SECONDS if number == 0 else
                 self.RISE_SECONDS if number > self.count else self.FALL_SECONDS)
        if number == self.count or now - self.since < delay:
            return None
        previous, self.count = self.count, number
        return previous, number


class Monitor:
    def __init__(self, wine_capture=None, event_share=None, *, shared=None, infer=None):
        self.infer = infer
        self.lock = shared.lock if shared else threading.RLock()
        self.playback = shared.playback if shared else PlaybackCache(timeout=300)
        self.stop = threading.Event()
        self.worker = None
        self.capture = wine_capture if wine_capture is not None else native_capture()
        self.channel = 1
        self.message = "就绪"
        self.frame = None
        self.sequence = 0
        self.count = None
        self.detect = False
        self.active_model_key = None
        self.recording = False
        self.event_recording = False
        self.revision = 0
        if shared:
            self.storage, self.alerts, self.event_share = shared.storage, shared.alerts, shared.event_share
            self.regions, self.exclusion_regions = shared.regions, shared.exclusion_regions
            self.exclusion_settings, self.model_setting = shared.exclusion_settings, shared.model_setting
            self.model_key = shared.model_key
            return
        self.model_setting = DATA / "detector-model.txt"
        try:
            preferred = self.model_setting.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            preferred = ""
        self.model_key = preferred if preferred in MODELS and MODELS[preferred].is_file() else (
            "nano" if MODELS["nano"].is_file() else "original")
        self.regions = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
        self.exclusion_settings = DATA / "excluded-regions.json"
        self.exclusion_regions = (json.loads(self.exclusion_settings.read_text(encoding="utf-8"))
                                  if self.exclusion_settings.exists() else {})
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
            with self.lock:
                self.active_model_key = None
                if recording:
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
        preview_boxes = ()
        preview_revision = -1

        def draw_boxes(image, boxes):
            if boxes:
                overlay = ImageDraw.Draw(image)
                for box in boxes:
                    overlay.rectangle(tuple(map(int, box)), outline="#69e5ab", width=2)

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
            previous_frame = None
            stream_epoch = 0
            while True:
                raw = player.stdout.read(size)
                if len(raw) != size:
                    return
                captured = time.monotonic()
                if previous_frame is not None and captured - previous_frame > StableCount.MAX_SAMPLE_GAP:
                    stream_epoch += 1
                previous_frame = captured
                try:
                    latest.get_nowait()
                except queue.Empty:
                    pass
                latest.put_nowait((captured, stream_epoch, raw))
                with self.lock:
                    boxes = preview_boxes if self.detect and self.revision == preview_revision else ()
                image = Image.frombytes("RGB", (WIDTH, HEIGHT), raw)
                draw_boxes(image, boxes)
                jpeg = self.jpeg(image)
                with self.lock:
                    self.frame = jpeg
                    self.sequence += 1

        threads = [threading.Thread(target=fn, daemon=True) for fn in (feed, read)]
        for thread in threads:
            thread.start()
        stable = StableCount()
        revision = -1
        detector = None
        loaded_model = None
        detection_config = None
        next_detection = 0.0
        detection_epoch = None
        last_frame = time.monotonic()
        try:
            while not self.stop.is_set() and not sdk_done.is_set():
                if recording:
                    if recording.error:
                        raise RuntimeError(recording.error)
                try:
                    captured, stream_epoch, raw = latest.get(timeout=0.2)
                except queue.Empty:
                    if player.poll() is not None or time.monotonic() - last_frame > 30:
                        raise RuntimeError("视频解码中断或等待画面超时")
                    continue
                last_frame = time.monotonic()
                with self.lock:
                    detect, current_revision = self.detect, self.revision
                    region = self.regions.get(str(self.channel))
                    excluded = self.exclusion_regions.get(str(self.channel))
                    model_key = self.model_key
                if current_revision != revision:
                    config = (detect, region, excluded)
                    if config != detection_config:
                        stable = StableCount()
                        detection_config = config
                    else:
                        stable.last_sample = None
                    revision = current_revision
                    next_detection = 0.0
                    detection_epoch = None
                change = None
                shot = None
                visible = None
                if detect and region and captured >= next_detection:
                    with self.infer.turn(self.stop) if self.infer else nullcontext(True) as acquired:
                        if not acquired or sdk_done.is_set():
                            continue
                        with self.lock:
                            if current_revision != self.revision:
                                continue
                        try:
                            captured, stream_epoch, raw = latest.get_nowait()
                        except queue.Empty:
                            pass
                        next_detection = captured + 0.45
                        if self.infer:
                            detector = self.infer.detector(model_key)
                        elif loaded_model != model_key:
                            detector = PersonDetector(model_key)
                            loaded_model = model_key
                        image = Image.frombytes("RGB", (WIDTH, HEIGHT), raw)
                        boxes = detector.boxes(image)
                    vertices = region_vertices(region)
                    excluded_vertices = region_vertices(excluded) if excluded else ()
                    visible = [box for box in boxes
                               if box_inside_regions(box, vertices, excluded_vertices)]
                    change = stable.observe(len(visible), captured, continuous=detection_epoch == stream_epoch)
                    detection_epoch = stream_epoch
                    if change:
                        draw_boxes(image, visible)
                        shot = self.jpeg(image)
                with self.lock:
                    if current_revision != self.revision:
                        continue
                    if visible is not None:
                        preview_boxes, preview_revision = visible, revision
                        self.active_model_key = model_key
                    elif not detect or not region:
                        self.active_model_key = None
                    self.count = stable.count if detect else None
                    self.message = "检测中" if detect else "预览中"
                    if recording and recording.events:
                        recording.events.observe(bool(detect and region and stable.count), time.monotonic(), change, shot)
                        self.event_recording = recording.events.file is not None
                    elif change:
                        self.storage.snapshot(self.channel, change, shot)
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

            self.worker = threading.Thread(target=run, name=f"seetong-camera-{channel}", daemon=True)
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


class InferenceQueue:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = deque()
        self.models = {}

    @contextmanager
    def turn(self, stop):
        ticket = object()
        with self.condition:
            self.queue.append(ticket)
            while self.queue[0] is not ticket and not stop.is_set():
                self.condition.wait(.2)
        try:
            yield not stop.is_set()
        finally:
            with self.condition:
                self.queue.remove(ticket)
                self.condition.notify_all()

    def detector(self, model_key):
        if model_key not in self.models:
            self.models[model_key] = PersonDetector(model_key)
        return self.models[model_key]


class Channels:
    def __init__(self, capture, event_share, autostart=None):
        inference = InferenceQueue()
        self.primary = Monitor(capture, event_share, infer=inference)
        self.lock = self.primary.lock
        self.items = {1: self.primary}
        self.items.update({channel: Monitor(capture, shared=self.primary, infer=inference) for channel in (2, 3)})
        self.settings = DATA / "channels.json"
        self.credentials = None
        self.remember = False
        self.done = threading.Event()
        self.wake = threading.Event()
        self.config = {}
        for channel, monitor in self.items.items():
            monitor.channel = channel
            self.config[channel] = {"enabled": channel == autostart, "record": channel == autostart,
                                    "detect": channel == autostart and str(channel) in monitor.regions
                                    and MODELS[monitor.model_key].is_file()
                                    and importlib.util.find_spec("onnxruntime") is not None,
                                    "model": monitor.model_key}
        if self.settings.exists():
            saved = json.loads(self.settings.read_text(encoding="utf-8"))
            self.config = self.validate(saved, startup=True)
        if VAULT.is_file():
            self.credentials = self.read_credentials()
        self.worker = threading.Thread(target=self.reconcile, name="seetong-channels", daemon=True)
        self.worker.start()

    @staticmethod
    def read_credentials():
        value = json.loads(read_secret(VAULT))
        if (not isinstance(value, list) or len(value) != 2
                or not all(isinstance(item, str) and item for item in value)):
            raise ValueError("无法读取已记住的账号，请重新保存账号")
        return tuple(value)

    def validate(self, raw, *, startup=False):
        if not isinstance(raw, dict) or set(raw) != {"1", "2", "3"}:
            raise ValueError("请提交第 1、2、3 路的完整设置")
        parsed = {}
        for channel, monitor in self.items.items():
            config = raw[str(channel)]
            if not isinstance(config, dict) or any(type(config.get(key)) is not bool for key in
                                                  ("enabled", "detect", "record")):
                raise ValueError("通道开关无效")
            model = config.get("model")
            if not isinstance(model, str) or model not in MODELS:
                raise ValueError("未知的人体检测模型")
            if startup and not MODELS[model].is_file():
                model = monitor.model_key
            detect = config["enabled"] and config["detect"]
            if detect and (
                    str(channel) not in monitor.regions or not MODELS[model].is_file()
                    or importlib.util.find_spec("onnxruntime") is None):
                if startup:
                    detect = False
                else:
                    raise ValueError(f"第 {channel} 路需要先保存检测区域并安装所选模型")
            parsed[channel] = dict(enabled=config["enabled"], detect=detect,
                                   record=config["enabled"] and config["record"], model=model)
        return parsed

    def apply(self, raw, data=None):
        parsed = self.validate(raw)
        credentials, remember = self.credentials, self.remember
        if data is not None:
            username, password = data.get("username"), data.get("password")
            if username or password:
                if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                    raise ValueError("请输入完整的云平台账号和密码")
                credentials = (username.strip(), password)
                if self.credentials and credentials != self.credentials and any(
                        m.worker and m.worker.is_alive() for m in self.items.values()):
                    raise ValueError("更换云账号前请先停用所有通道")
                remember = data.get("remember") is True
        if not credentials and any(config["enabled"] for config in parsed.values()):
            raise ValueError("请输入或保存云平台账号和密码")
        atomic_write(self.settings, json.dumps(parsed).encode("utf-8"))
        self.config, self.credentials, self.remember = parsed, credentials, remember
        self.wake.set()

    def update(self, channel, **changes):
        raw = {str(key): dict(value) for key, value in self.config.items()}
        raw[str(channel)].update(changes)
        self.apply(raw)

    def reconcile(self):
        while not self.done.is_set():
            with self.lock:
                for channel, monitor in self.items.items():
                    config = self.config[channel]
                    running = monitor.worker and monitor.worker.is_alive()
                    if running and (not config["enabled"] or monitor.recording != config["record"]):
                        monitor.stop.set()
                        monitor.message = "正在停止" if not config["enabled"] else "正在更新录像设置"
                    elif running and not monitor.stop.is_set() and (
                            monitor.detect != config["detect"] or monitor.model_key != config["model"]):
                        monitor.detect, monitor.model_key = config["detect"], config["model"]
                        monitor.revision += 1
                    elif not running:
                        monitor.model_key = config["model"]
                        if config["enabled"] and self.credentials:
                            monitor.start(self.credentials, channel, self.remember and not VAULT.exists(),
                                          config["record"], config["detect"])
            self.wake.wait(.25)
            self.wake.clear()

    def shutdown(self):
        self.done.set()
        self.wake.set()
        self.worker.join(2)
        for monitor in self.items.values():
            monitor.stop.set()
        deadline = time.monotonic() + 15
        for monitor in self.items.values():
            if monitor.worker:
                monitor.worker.join(max(0, deadline-time.monotonic()))


def channel_state(monitor):
    running = bool(monitor.worker and monitor.worker.is_alive())
    return dict(channel=monitor.channel, message=monitor.message, active=running, detect=monitor.detect,
                count=monitor.count, sequence=monitor.sequence if monitor.frame else 0,
                recording=monitor.recording, event_recording=monitor.event_recording,
                detector_model=monitor.model_key, active_detector_model=monitor.active_model_key)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def channel_monitor(self, channel=None):
        manager = getattr(self.server, "channels", None)
        if channel is None:
            return self.server.monitor
        if type(channel) is not int or channel not in (1, 2, 3):
            raise ValueError("无效通道")
        if manager:
            return manager.items[channel]
        if channel != self.server.monitor.channel:
            raise ValueError("无效通道")
        return self.server.monitor

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
        if route.startswith(("/api/event/duration/", "/api/recordings/duration/")):
            path = self.recording_path(route)
            return self.reply({"seconds": recording_duration(path)}) if path else self.reply({}, 404)
        if route == "/api/events":
            return self.reply([path.name for path in sorted(RECORDINGS.glob("*-event.ts"), reverse=True)
                               if EVENT_NAME.fullmatch(path.name) and not path.is_symlink()])
        if route == "/api/recordings":
            return self.reply(self.recording_history())
        if route == "/api/event-cards":
            return self.reply(self.recording_history(events_only=True)[:12])
        manager = getattr(self.server, "channels", None)
        try:
            requested_channel = parse_qs(url.query, keep_blank_values=True).get("channel")
            monitor = self.channel_monitor(int(requested_channel[0]) if requested_channel else None)
        except (ValueError, TypeError):
            return self.reply({"error": "无效通道"}, 400)
        with monitor.lock:
            if route == "/api/state":
                storage = monitor.storage.status()
                return self.reply(dict(
                    **channel_state(monitor), saved=VAULT.exists(), recorded_bytes=sum(item["used"] for item in storage.values()),
                    storage=storage,
                    alerts=monitor.alerts.status(),
                    event_share=monitor.event_share is not None,
                    regions=monitor.regions,
                    exclusion_regions=monitor.exclusion_regions,
                    detector_models={key: path.is_file() for key, path in MODELS.items()},
                    channels=[dict(**channel_state(item), config=manager.config[channel])
                              for channel, item in manager.items.items()] if manager else [],
                    can_start=bool(manager.credentials) if manager else VAULT.exists(),
                ))
            frame = monitor.frame if route == "/api/frame" else None
        if frame:
            return self.reply(frame, mime="image/jpeg")
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

    def recording_path(self, route):
        name = route.rsplit("/", 1)[1]
        path = RECORDINGS / name
        match = EVENT_NAME.fullmatch(name) or NAME.fullmatch(name)
        if not match or path.is_symlink() or not path.is_file():
            return None
        return path

    def recording_video(self, route):
        path = self.recording_path(route)
        if path is None:
            return self.reply({}, 404)
        name = path.name
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

        try:
            serve_recorded_video(self, path, self.server.monitor.playback)
        except (ConnectionError, TimeoutError):
            pass

    def headers_for(self, status, mime, length):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")

    def do_POST(self):
        if (self.headers.get("Host") != self.server.host or
                self.headers.get("Origin") != "http://" + self.server.host or
                self.headers.get("X-Seetong-Session") != self.server.token):
            return self.reply({}, 403)
        manager = getattr(self.server, "channels", None)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 8192:
                raise ValueError("请求大小无效")
            data = json.loads(self.rfile.read(length))
            route = self.path
            if not isinstance(data, dict):
                raise ValueError("请求无效")
            monitor = self.channel_monitor(data.get("channel") if manager else None)
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
                elif route == "/api/channels" and manager:
                    manager.apply(data.get("channels"), data)
                elif route == "/api/start":
                    username, password = data.get("username"), data.get("password")
                    if not username and not password and VAULT.exists():
                        username, password = json.loads(read_secret(VAULT))
                    if not isinstance(username, str) or not username.strip() or not isinstance(password, str) or not password:
                        raise ValueError("请输入完整的云平台账号和密码")
                    channel = data.get("channel")
                    if type(channel) is not int or channel not in (1, 2, 3):
                        raise ValueError("无效通道")
                    if manager:
                        raw = {str(key): dict(value) for key, value in manager.config.items()}
                        raw[str(channel)].update(enabled=True, record=data.get("record") is True)
                        manager.apply(raw, data)
                    else:
                        monitor.start((username.strip(), password), channel,
                                      data.get("remember") is True, data.get("record") is True)
                elif route == "/api/forget":
                    VAULT.unlink(missing_ok=True)
                    if manager:
                        manager.remember = False
                        if not any(config["enabled"] for config in manager.config.values()):
                            manager.credentials = None
                elif route == "/api/stop":
                    if manager:
                        manager.update(monitor.channel, enabled=False)
                    else:
                        monitor.stop.set()
                        monitor.message = "正在停止"
                elif route in ("/api/region", "/api/exclusion-region"):
                    if not monitor.frame:
                        raise ValueError("请先打开实时画面")
                    points = data.get("region")
                    if data.get("channel", monitor.channel) != monitor.channel:
                        raise ValueError("通道已切换，请重新绘制区域")
                    exclusion = route == "/api/exclusion-region"
                    if "region" not in data:
                        raise ValueError("缺少区域参数")
                    if not (exclusion and points is None) and not valid_region(points):
                        raise ValueError("区域太小或边线相交，请调整边界")
                    channel = str(monitor.channel)
                    if exclusion:
                        updated = monitor.exclusion_regions.copy()
                        if points is None:
                            updated.pop(channel, None)
                        else:
                            updated[channel] = points
                        atomic_write(monitor.exclusion_settings, json.dumps(updated).encode("utf-8"))
                        monitor.exclusion_regions.clear()
                        monitor.exclusion_regions.update(updated)
                    else:
                        updated = monitor.regions | {channel: points}
                        atomic_write(SETTINGS, json.dumps(updated).encode("utf-8"))
                        monitor.regions.clear()
                        monitor.regions.update(updated)
                    monitor.revision += 1
                elif route == "/api/detector-model":
                    selected = data.get("model")
                    if not isinstance(selected, str) or selected not in MODELS:
                        raise ValueError("未知的人体检测模型")
                    if not MODELS[selected].is_file():
                        raise ValueError("本机尚未安装这个检测模型")
                    if manager:
                        manager.update(monitor.channel, model=selected)
                    elif selected != monitor.model_key:
                        atomic_write(monitor.model_setting, selected.encode("utf-8"))
                        monitor.model_key = selected
                        monitor.revision += 1
                elif route == "/api/detect":
                    enabled = data.get("enabled") is True
                    if enabled and (not monitor.frame or str(monitor.channel) not in monitor.regions):
                        raise ValueError("请先预览并框选区域")
                    if enabled and not MODELS[monitor.model_key].is_file():
                        raise ValueError("本机缺少人体检测模型")
                    if enabled and importlib.util.find_spec("onnxruntime") is None:
                        raise ValueError("本机缺少 ONNX Runtime，请先安装检测依赖")
                    if manager:
                        manager.update(monitor.channel, detect=enabled)
                    else:
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
    channels = Channels(capture, share, args.autostart)
    share_context = (ThreadingHTTPServer(("127.0.0.1", args.share_port), EventShareHandler)
                     if share else nullcontext())
    with share_context as share_server, ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.host = f"127.0.0.1:{server.server_port}"
        server.token = secrets.token_hex(32)
        server.channels = channels
        server.monitor = channels.primary
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
            pass
        finally:
            channels.shutdown()
            if share_server:
                share_server.shutdown()


if __name__ == "__main__":
    main()
