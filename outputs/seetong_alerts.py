"""Deliver notices for completed event recordings through WxPusher."""

import json
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from html import escape
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seetong_recording import EVENT_NAME

API = "https://wxpusher.zjiecode.com/api/send/message/simple-push"
TOKEN = re.compile(r"SPT_[A-Za-z0-9_-]{1,120}")


@dataclass(frozen=True)
class Recipient:
    id: str
    name: str
    token: str


def load_recipients(value):
    if TOKEN.fullmatch(value):
        return [Recipient("original", "手机 1", value)]
    return [Recipient(**entry) for entry in json.loads(value)]


class PushError(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


def send(token, title, content, link=None):
    payload = {"spt": token, "summary": title, "content": content, "contentType": 1}
    if link:
        payload.update(url=link, contentType=2,
                       content=f'<p>{escape(content)}</p><p><a href="{escape(link)}">查看抓拍和录像</a></p>')
    request = Request(API, method="POST", headers={"Content-Type": "application/json"},
                      data=json.dumps(payload).encode("utf-8"))
    try:
        with urlopen(request, timeout=8) as response:
            result = json.loads(response.read(32 * 1024))
    except HTTPError as error:
        error.close()
        raise PushError(f"推送服务返回 HTTP {error.code}",
                        retry=error.code == 429 or 500 <= error.code < 600) from None
    except OSError:
        raise PushError("暂时无法连接 WxPusher", retry=True) from None
    except ValueError:
        raise PushError("无法读取 WxPusher 的响应") from None
    if not isinstance(result, dict) or result.get("code") != 1000:
        raise PushError("WxPusher 拒绝了推送，请检查 SPT 或手机绑定")


class MobileAlerts:
    def __init__(self, event_link=None):
        self.lock = threading.Lock()
        self.event_link = event_link
        self.recipients = {}
        self.last_result = ""
        self.pending = queue.Queue(maxsize=128)
        self.worker = None

    def configure(self, recipients):
        with self.lock:
            self.recipients = {recipient.id: recipient for recipient in recipients}
            self.last_result = ""

    def updated(self, name, token):
        with self.lock:
            recipients = list(self.recipients.values())
        for index, recipient in enumerate(recipients):
            if recipient.token == token:
                if name:
                    recipients[index] = Recipient(recipient.id, name, token)
                return recipients
        recipients.append(Recipient(secrets.token_hex(8), name or "WxPusher 手机", token))
        return recipients

    def without(self, device_id):
        with self.lock:
            if device_id not in self.recipients:
                raise ValueError("此设备已解绑")
            return [recipient for recipient in self.recipients.values() if recipient.id != device_id]

    def status(self):
        with self.lock:
            return {"enabled": bool(self.recipients), "last_result": self.last_result,
                    "devices": [{"id": recipient.id, "name": recipient.name}
                                for recipient in self.recipients.values()]}

    def test(self, device_id=None, link=None):
        with self.lock:
            if device_id is not None and device_id not in self.recipients:
                raise ValueError("此设备已解绑")
            recipients = ([self.recipients[device_id]] if device_id is not None
                          else list(self.recipients.values()))
        if not recipients:
            raise ValueError("请先保存 WxPusher SPT")
        content = "这是一条来自 Seetong 监控的测试提醒。"
        if link:
            content += "可打开最近一次已保存的区域事件。"
        for recipient in recipients:
            send(recipient.token, "Seetong 手机提醒测试", content, link)

    def enqueue(self, path):
        with self.lock:
            if not self.recipients:
                return
            try:
                self.pending.put_nowait((tuple(self.recipients), path.name))
            except queue.Full:
                self.last_result = "提醒队列已满，最新事件未推送"
                return
            if self.worker is None:
                self.worker = threading.Thread(target=self.deliver, name="seetong-alerts", daemon=True)
                self.worker.start()

    def deliver(self):
        while True:
            device_ids, name = self.pending.get()
            timestamp, channel = EVENT_NAME.fullmatch(name).groups()
            local_time = datetime.fromtimestamp(int(timestamp) // 1_000_000_000)
            title = f"Seetong 第 {channel} 路事件录像"
            content = f"第 {channel} 路在 {local_time:%Y-%m-%d %H:%M:%S} 触发事件，录像已保存。"
            link = self.event_link(name) if self.event_link else None
            if link is None:
                content += "请在监控页的「历史录像」查看。"
            for device_id in device_ids:
                self.send_event(device_id, title, content, link)

    def send_event(self, device_id, title, content, link):
        for delay in (0, 4, 12):
            if delay:
                time.sleep(delay)
            with self.lock:
                recipient = self.recipients.get(device_id)
            if recipient is None:
                return
            try:
                send(recipient.token, title, content, link)
            except PushError as error:
                result = str(error)
                retry = error.retry
            else:
                result = "提醒已提交"
                retry = False
            with self.lock:
                if device_id in self.recipients:
                    self.last_result = f"{datetime.now():%m-%d %H:%M:%S} · {recipient.name} · {result}"
            if not retry:
                return
