"""Receive FFmpeg segments locally and enforce the disk limit before writing."""

import math
import re
import secrets
import shutil
import subprocess
import threading
import time
from bisect import insort
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

LIMIT = 10_000_000_000
DEFAULT_LIMITS = {"event": 2_000_000_000, "continuous": 8_000_000_000}
SEGMENT_SECONDS = 600
CHUNK_BYTES = 64 * 1024
NAME = re.compile(r"(\d{19})-ch([1-3])-\d{6,}\.ts")
EVENT_NAME = re.compile(r"(\d{19})-ch([1-3])-event\.ts")
SHOT_NAME = re.compile(r"(\d{8}-\d{6}-\d{6})-ch([1-3])-(\d+)-(\d+)(?:-(\d{19}))?\.jpg")
EVENT_SEGMENT_SECONDS = 2
PRE_ROLL_SECONDS = 8
POST_ROLL_SECONDS = 2
EVENT_BUFFER_BYTES = 24 * 1024 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def probe_recording(path, field):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", field,
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=4, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def recording_duration(path):
    try:
        seconds = float(probe_recording(path, "format=duration") or 0)
        return seconds if math.isfinite(seconds) and seconds > 0 else None
    except ValueError:
        return None


def playback_command(ffmpeg, path, *, copy_video=False, destination=None):
    encoding = (["-c:v", "copy"] if copy_video else
                ["-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-g", "30"])
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-threads", "2", "-i", str(path),
        "-map", "0:v:0", "-an", *encoding, "-movflags",
        "+faststart" if destination else "+frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", str(destination) if destination else "pipe:1",
    ]


class Quota:
    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.closed = []

    def remember(self, path, size, timestamp):
        insort(self.closed, (timestamp, path, size))

    def make_room(self, incoming):
        while self.closed and self.size + incoming > self.limit:
            _, path, size = self.closed[0]
            try:
                path.unlink(missing_ok=True)
            except PermissionError:
                return False
            self.closed.pop(0)
            self.size -= size
        return self.size + incoming <= self.limit


class RecordingStorage:
    def __init__(self, folder, shots, limits):
        self.folder = folder
        self.shots = shots
        self.lock = threading.RLock()
        self.quotas = {kind: Quota(limit) for kind, limit in limits.items()}
        for path in folder.glob("*.part"):
            name = path.with_suffix(".ts").name
            if (NAME.fullmatch(name) or EVENT_NAME.fullmatch(name)) and not path.is_symlink() and path.is_file():
                if path.stat().st_size:
                    path.rename(path.with_suffix(".ts"))
                else:
                    path.unlink()
        for path in [*folder.glob("*.ts"), *shots.glob("*.jpg")]:
            if path.is_symlink() or not path.is_file():
                continue
            if NAME.fullmatch(path.name):
                quota = self.quotas["continuous"]
            elif EVENT_NAME.fullmatch(path.name) or SHOT_NAME.fullmatch(path.name):
                quota = self.quotas["event"]
            else:
                continue
            info = path.stat()
            quota.size += info.st_size
            quota.remember(path, info.st_size, info.st_mtime_ns)
        for quota in self.quotas.values():
            quota.make_room(0)

    def status(self):
        with self.lock:
            return {kind: {"limit": quota.limit, "used": quota.size} for kind, quota in self.quotas.items()}

    def set_limits(self, limits, persist=None):
        with self.lock:
            for kind, limit in limits.items():
                quota = self.quotas[kind]
                active = quota.size - sum(size for _, _, size in quota.closed)
                if active > limit:
                    raise ValueError("新上限小于正在录制的片段，请等片段完成后重试")
            if persist:
                persist(limits)
            for kind, limit in limits.items():
                quota = self.quotas[kind]
                quota.limit = limit
                quota.make_room(0)

    def write(self, kind, file, data):
        with self.lock:
            quota = self.quotas[kind]
            if len(data) > quota.limit or file.tell() + len(data) > quota.limit or not quota.make_room(len(data)):
                return False
            count = file.write(data)
            quota.size += count
            if count != len(data):
                raise OSError("incomplete recording write")
            return True

    def finish(self, kind, path):
        with self.lock:
            info = path.stat()
            if info.st_size:
                final = path.with_suffix(".ts")
                if path != final:
                    path.rename(final)
                self.quotas[kind].remember(final, info.st_size, info.st_mtime_ns)
                return final
            else:
                path.unlink()

    def snapshot(self, channel, increase, jpeg, event_id=None):
        with self.lock:
            quota = self.quotas["event"]
            if len(jpeg) > quota.limit or not quota.make_room(len(jpeg)):
                return
            now = datetime.now()
            suffix = f"-{event_id}" if event_id else ""
            path = self.shots / f"{now:%Y%m%d-%H%M%S-%f}-ch{channel}-{increase[0]}-{increase[1]}{suffix}.jpg"
            self.shots.mkdir(parents=True, exist_ok=True)
            pending = path.with_suffix(".tmp")
            pending.write_bytes(jpeg)
            pending.replace(path)
            quota.size += len(jpeg)
            quota.remember(path, len(jpeg), path.stat().st_mtime_ns)


class Recording:
    def __init__(self, folder, channel, limit=LIMIT, segment_seconds=SEGMENT_SECONDS,
                 *, continuous=True, events=False, on_event=None, storage=None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.segment_seconds = segment_seconds
        self.continuous = continuous
        self.prefix = f"{time.time_ns():019d}-ch{channel}-"
        self.token = secrets.token_hex(16)
        self.error = None
        self.storage = storage or RecordingStorage(folder, folder.parent / "snapshots",
                                                    {"event": limit, "continuous": limit})
        self.lock = self.storage.lock
        self.on_event = on_event
        self.events = EventClips(self, channel) if events else None

    def __enter__(self):
        self.servers = {}
        try:
            for mode, enabled in [("continuous", self.continuous), ("event", self.events is not None)]:
                if enabled:
                    server = HTTPServer(("127.0.0.1", 0), SegmentHandler)
                    server.recording, server.mode = self, mode
                    self.servers[mode] = server
            for server in self.servers.values():
                threading.Thread(target=server.serve_forever, daemon=True).start()
        except OSError:
            for server in self.servers.values():
                server.server_close()
            raise
        return self

    def __exit__(self, *_):
        for server in self.servers.values():
            server.shutdown()
            server.server_close()
        if self.events:
            self.events.finish()

    def ffmpeg_args(self):
        args = self.segment_args("continuous", self.segment_seconds) if self.continuous else []
        if self.events:
            args.extend(self.segment_args("event", EVENT_SEGMENT_SECONDS))
        return args

    def segment_args(self, mode, seconds):
        url = f"http://127.0.0.1:{self.servers[mode].server_port}/{self.token}/%06d.ts"
        if mode == "event":
            # Cameras can send keyframes tens of seconds apart. Encode the event
            # buffer separately so it gets a decodable segment every two seconds.
            fps = 10
            gop = str(fps * seconds)
            encoding = [
                "-vf", f"fps={fps},scale=w='min(iw,1280)':h=-2",
                "-c:v", "libx264", "-threads", "2", "-preset", "ultrafast",
                "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-crf", "25",
                "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
            ]
        else:
            # Raw SDK frames can have placeholder timestamps.
            timing = "setts=ts='N*max(DURATION,0.04/TB)'"
            encoding = ["-c:v", "copy", "-bsf:v", timing]
        return [
            "-map", "0:v:0", "-an", *encoding, "-f", "segment",
            "-segment_format", "mpegts", "-segment_time", str(seconds),
            "-reset_timestamps", "1" if mode == "continuous" else "0",
            "-segment_format_options", "mpegts_flags=+initial_discontinuity",
            "-flush_packets", "1", url,
        ]


class EventClips:
    def __init__(self, recording, channel):
        self.recording = recording
        self.channel = channel
        self.recent = deque()
        self.recent_bytes = 0
        self.file = None
        self.full = False
        self.tail_until = None

    def observe(self, occupied, now, increase=None, jpeg=None):
        with self.recording.lock:
            if occupied:
                if self.tail_until is not None:
                    # The stable counter already confirmed the previous departure.
                    # A new entry is a separate event even before the tail is flushed.
                    self.finish()
                if self.file is None:
                    self.trim(now)
                    self.path = self.recording.folder / f"{time.time_ns():019d}-ch{self.channel}-event.part"
                    self.file = self.path.open("xb", buffering=0)
                    self.full = False
                    for _, segment in self.recent:
                        if not self.recording.storage.write("event", self.file, segment):
                            self.full = True
                            break
            elif self.file is not None and self.tail_until is None:
                self.tail_until = now + POST_ROLL_SECONDS
            if increase:
                event_id = self.path.name.split("-")[0] if self.file is not None else None
                self.recording.storage.snapshot(self.channel, increase, jpeg, event_id)

    def trim(self, now):
        while self.recent and (self.recent[0][0] < now - PRE_ROLL_SECONDS
                               or self.recent_bytes > EVENT_BUFFER_BYTES):
            _, segment = self.recent.popleft()
            self.recent_bytes -= len(segment)

    def segment(self, data):
        if not data:
            return
        with self.recording.lock:
            now = time.monotonic()
            self.recent.append((now, data))
            self.recent_bytes += len(data)
            self.trim(now)
            if self.file is not None:
                if not self.full:
                    self.full = not self.recording.storage.write("event", self.file, data)
                if self.tail_until is not None and now >= self.tail_until:
                    self.finish()

    def finish(self):
        if self.file is not None:
            self.file.close()
            self.file = None
            self.tail_until = None
            saved = self.recording.storage.finish("event", self.path)
            if saved and self.recording.on_event:
                self.recording.on_event(saved)


class SegmentHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def chunks(self):
        self.connection.settimeout(30)
        while header := self.rfile.readline(128):
            remaining = int(header.strip(), 16)
            if remaining == 0:
                self.rfile.readline(128)
                return
            if remaining < 0:
                raise ValueError("invalid chunk size")
            while remaining:
                data = self.rfile.read(min(CHUNK_BYTES, remaining))
                if not data:
                    raise EOFError
                yield data
                remaining -= len(data)
            if self.rfile.read(2) != b"\r\n":
                raise EOFError

    def do_POST(self):
        recording = self.server.recording
        match = re.fullmatch(rf"/{recording.token}/(\d{{6,}}\.ts)", self.path)
        if not match or self.headers.get("Transfer-Encoding", "").lower() != "chunked":
            return self.send_error(400)
        name = match[1]
        try:
            if self.server.mode == "event":
                segment = bytearray()
                try:
                    for data in self.chunks():
                        if len(segment) + len(data) > EVENT_BUFFER_BYTES:
                            raise RuntimeError("事件录像等待关键帧超时，片段超过缓存上限")
                        segment.extend(data)
                finally:
                    recording.events.segment(segment)
            else:
                path = recording.folder / (recording.prefix + name.removesuffix(".ts") + ".part")
                file = path.open("xb", buffering=0)
                try:
                    with file:
                        writable = True
                        for data in self.chunks():
                            if writable:
                                writable = recording.storage.write("continuous", file, data)
                finally:
                    recording.storage.finish("continuous", path)
            self.send_response(200)
            self.end_headers()
        except RuntimeError as exc:
            recording.error = str(exc)
        except (ConnectionError, TimeoutError, EOFError):
            pass
        except (OSError, ValueError):
            recording.error = "无法保存录像，请检查磁盘空间和目录权限"
