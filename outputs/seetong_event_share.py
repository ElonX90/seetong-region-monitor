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
from urllib.parse import urlsplit

from seetong_recording import CREATE_NO_WINDOW, EVENT_NAME, SHOT_NAME, playback_command

LINK_SECONDS = 24 * 60 * 60
ROUTE = re.compile(r"/event/([^/]+)/(\d{10,12})/([0-9a-f]{64})/(snapshot\.jpg|video\.mp4)?")
RANGE = re.compile(r"bytes=(\d{0,20})-(\d{0,20})")


class EventShare:
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
        self.video_lock = threading.Lock()
        self.cached_name = None
        self.cached_video = None

    def signature(self, name, expires):
        return hmac.digest(self.key, f"{name}\n{expires}".encode("ascii"), "sha256").hex()

    def link(self, name):
        if self.recording(name) is None:
            return None
        expires = int(time.time()) + LINK_SECONDS
        return f"{self.base_url}/event/{name}/{expires}/{self.signature(name, expires)}/"

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

    def close(self):
        if self.cached_video is not None:
            self.cached_video.close()
            self.cached_video = self.cached_name = None

    def prepare_video(self, path):
        if self.cached_name == path.name:
            return self.cached_video
        self.close()
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise OSError("ffmpeg unavailable")
        video = tempfile.TemporaryFile()
        try:
            subprocess.run(playback_command(ffmpeg, path), stdin=subprocess.DEVNULL,
                           stdout=video, stderr=subprocess.DEVNULL, check=True, timeout=120,
                           creationflags=CREATE_NO_WINDOW)
            if not video.seek(0, 2):
                raise OSError("empty MP4")
        except (OSError, subprocess.SubprocessError):
            video.close()
            raise
        self.cached_name, self.cached_video = path.name, video
        return video


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
        name, expires, signature, asset = route.groups()
        event = EVENT_NAME.fullmatch(name)
        share = self.server.share
        if not event or not hmac.compare_digest(signature, share.signature(name, expires)):
            return self.reply("找不到事件", 404)
        if int(expires) <= time.time():
            return self.reply("链接已过期", 410)
        recording = share.recording(name)
        if recording is None:
            return self.reply("录像已清理", 410)
        if asset == "video.mp4":
            return self.video(recording)
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
        return self.reply(f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>区域事件回看</title>
<style>
  :root {{ color-scheme:dark; font:16px system-ui,sans-serif; background:#111718; color:#edf4f2 }}
  body {{ margin:0 auto; max-width:760px; padding:clamp(16px,4vw,36px) }}
  h1 {{ margin:0 0 6px; font-size:clamp(24px,5vw,32px) }} h2 {{ margin-top:26px; font-size:18px }}
  p {{ margin:0 0 16px; color:#afc4be }} img,video {{ display:block; width:100%; border-radius:12px; background:#000 }}
</style>
<main><h1>第 {channel} 路 · 区域事件</h1><p>{local_time:%Y-%m-%d %H:%M:%S}{count}</p>
{preview}<h2>事件录像</h2><video src="video.mp4" controls playsinline preload="none"></video></main>
</html>""", mime="text/html; charset=utf-8")

    def video(self, recording):
        share = self.server.share
        with share.video_lock:
            try:
                source = share.prepare_video(recording)
            except (OSError, subprocess.SubprocessError):
                return self.reply("暂时无法播放录像", 503)
            size = source.seek(0, 2)
            start, end = 0, size - 1
            requested = self.headers.get("Range")
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
                    self.headers_for(416, "video/mp4", 0)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            remaining = end - start + 1
            self.headers_for(206 if requested else 200, "video/mp4", remaining)
            self.send_header("Accept-Ranges", "bytes")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            source.seek(start)
            while remaining:
                data = source.read(min(remaining, 64 * 1024))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)
