"""Serve one completed event to holders of its expiring link."""

import hmac
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from seetong_recording import (CREATE_NO_WINDOW, EVENT_NAME, SHOT_NAME,
                               playback_command, probe_recording, recording_duration)

RETENTION = "retained"
ROUTE = re.compile(r"/event/([^/]+)/(retained|\d{10,12})/([0-9a-f]{64})/(snapshot\.jpg|video\.mp4|download\.mp4)?")
RANGE = re.compile(r"bytes=(\d{0,20})-(\d{0,20})")


class PlaybackCache:
    def __init__(self, timeout=120):
        self.video_lock = threading.Lock()
        self.cached_name = None
        self.cached_video = None
        self.directory = None
        self.timeout = timeout

    def close(self):
        self.cached_video = self.cached_name = None
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None

    def prepare_video(self, path):
        if self.cached_name == path:
            return self.cached_video
        self.close()
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise OSError("ffmpeg unavailable")
        self.directory = tempfile.TemporaryDirectory(prefix="seetong-playback-")
        destination = Path(self.directory.name) / "preview.mp4"
        try:
            codecs = set((probe_recording(path, "stream=codec_name") or b"").splitlines())
            subprocess.run(playback_command(ffmpeg, path, copy_video=codecs == {b"h264"}, destination=destination),
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=self.timeout,
                           creationflags=CREATE_NO_WINDOW)
            if not destination.stat().st_size:
                raise OSError("empty MP4")
            self.cached_video = destination
        except (OSError, subprocess.SubprocessError):
            self.close()
            raise
        self.cached_name = path
        return self.cached_video


class EventShare(PlaybackCache):
    def __init__(self, base_url, key, recordings, snapshots):
        origin = urlsplit(base_url)
        if (origin.scheme != "https" or not origin.hostname or origin.username is not None
                or origin.path not in ("", "/") or origin.query or origin.fragment
                or not re.fullmatch(r"[a-zA-Z0-9.\-]+(?::[0-9]{1,5})?", origin.netloc)
                or (origin.port is not None and origin.port == 0)):
            raise ValueError("事件回看地址必须是 HTTPS 站点地址")
        if len(key) != 32:
            raise ValueError("事件回看密钥格式无效")
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.recordings = recordings
        self.snapshots = snapshots
        super().__init__()

    def signature(self, name, lifetime):
        return hmac.digest(self.key, f"{name}\n{lifetime}".encode("ascii"), "sha256").hex()

    def link(self, name):
        if self.recording(name) is None:
            return None
        return f"{self.base_url}/event/{name}/{RETENTION}/{self.signature(name, RETENTION)}/"

    def latest_link(self):
        for recording in sorted(self.recordings.glob("*-event.ts"), reverse=True):
            if link := self.link(recording.name):
                return link

    def recording(self, name):
        path = self.recordings / name
        if EVENT_NAME.fullmatch(name) and not path.is_symlink() and path.is_file():
            return path

    def snapshot(self, timestamp, channel):
        for path in sorted(self.snapshots.glob(f"*-ch{channel}-*-{timestamp}.jpg")):
            match = SHOT_NAME.fullmatch(path.name)
            if match and not path.is_symlink() and path.is_file():
                return path, (int(match[3]), int(match[4]))
        return None, None

class EventShareHandler(BaseHTTPRequestHandler):
    timeout = 20

    def log_message(self, *_):
        pass

    def send_error(self, code, message=None, explain=None):
        self.reply("请求无效", code)

    def headers_for(self, status, mime, length):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'")

    def reply(self, data, status=200, mime="text/plain; charset=utf-8"):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.headers_for(status, mime, len(data))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.serve_event()
        except (ConnectionError, TimeoutError):
            pass

    def serve_event(self):
        route = ROUTE.fullmatch(self.path)
        if not route:
            return self.reply("找不到事件", 404)
        name, lifetime, signature, asset = route.groups()
        event = EVENT_NAME.fullmatch(name)
        share = self.server.share
        if not event or not hmac.compare_digest(signature, share.signature(name, lifetime)):
            return self.reply("找不到事件", 404)
        if lifetime != RETENTION and int(lifetime) <= time.time():
            return self.reply("链接已过期", 410)
        recording = share.recording(name)
        if recording is None:
            return self.reply("录像已清理", 410)
        if asset in ("video.mp4", "download.mp4"):
            filename = f"{name.removesuffix('.ts')}.mp4" if asset == "download.mp4" else None
            return serve_recorded_video(self, recording, share, filename=filename)
        timestamp, channel = event.groups()
        snapshot, counts = share.snapshot(timestamp, channel)
        if asset == "snapshot.jpg":
            if snapshot:
                try:
                    return self.reply(snapshot.read_bytes(), mime="image/jpeg")
                except FileNotFoundError:
                    pass
            return self.reply("抓拍图已清理或未保存", 404)
        local_time = datetime.fromtimestamp(int(timestamp) // 1_000_000_000)
        count = f" · {counts[0]} → {counts[1]} 人" if counts else ""
        preview = '<img src="snapshot.jpg" alt="区域事件抓拍">' if snapshot else '<p>本次事件暂无抓拍图</p>'
        duration = recording_duration(recording)
        duration_label = ""
        if duration is not None:
            minutes, seconds = divmod(int(duration + 0.5), 60)
            duration_label = f" · 总时长 {minutes:02d}:{seconds:02d}"
        return self.reply(f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>区域事件回看</title>
<style>
  :root {{ color-scheme:dark; font:16px system-ui,sans-serif; background:#111718; color:#edf4f2 }}
  body {{ margin:0 auto; max-width:760px; padding:clamp(16px,4vw,36px) }}
  h1 {{ margin:0 0 6px; font-size:clamp(24px,5vw,32px) }} h2 {{ margin-top:26px; font-size:18px }}
  p {{ margin:0 0 16px; color:#afc4be }} img,video {{ display:block; width:100%; border-radius:12px; background:#000 }}
  .download {{ display:inline-block; margin:16px 0 12px; padding:10px 14px; border-radius:8px; background:#8ae0b3; color:#12251c; font-weight:650; text-decoration:none }}
  details {{ color:#afc4be }} summary {{ cursor:pointer }} textarea {{ width:100%; box-sizing:border-box; margin-top:8px; font:inherit }}
</style>
<main><h1>第 {channel} 路 · 区域事件</h1><p>{local_time:%Y-%m-%d %H:%M:%S}{count}</p>
{preview}<h2>事件录像{duration_label}</h2><video src="video.mp4" controls playsinline preload="none"></video>
<a class="download" href="download.mp4" download>下载录像（MP4）</a>
<details><summary>App 内无法下载？</summary>
<p>复制下方回看链接，用 Safari 或系统浏览器打开，再点「下载录像」。</p>
<textarea readonly rows="3" aria-label="回看链接">{share.base_url}/event/{name}/{lifetime}/{signature}/</textarea>
</details></main>
</html>""", mime="text/html; charset=utf-8")

def serve_recorded_video(handler, recording, cache, *, filename=None):
    with cache.video_lock:
        try:
            source = cache.prepare_video(recording).open("rb")
        except (OSError, subprocess.SubprocessError):
            return handler.reply("暂时无法播放录像", 503)
        with source:
            return send_video_range(handler, source, filename=filename)


def send_video_range(handler, source, *, filename=None):
    size = source.seek(0, 2)
    start, end = 0, size - 1
    requested = handler.headers.get("Range")
    if requested is not None:
        match = RANGE.fullmatch(requested)
        if match and match[1]:
            start = int(match[1])
            if match[2]:
                end = min(end, int(match[2]))
        elif match and match[2]:
            start = max(0, size - int(match[2]))
        else:
            start = size
        if start > end:
            handler.headers_for(416, "video/mp4", 0)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.end_headers()
            return
    remaining = end - start + 1
    handler.headers_for(206 if requested else 200, "video/mp4", remaining)
    handler.send_header("Accept-Ranges", "bytes")
    if filename is not None:
        handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    if requested:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.end_headers()
    if handler.command == "HEAD":
        return
    source.seek(start)
    while remaining:
        data = source.read(min(remaining, 64 * 1024))
        if not data:
            break
        handler.wfile.write(data)
        remaining -= len(data)
