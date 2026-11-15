"""Run the Seetong SDK in Wine and relay its video to the native monitor."""

import importlib.util
import json
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path, PureWindowsPath

SCRIPT = Path(__file__).resolve()


def load_probe():
    directory = str(SCRIPT.parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location("seetong_probe", SCRIPT.with_name("seetong-login-probe.py"))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    return probe


class SocketPackets:
    def __init__(self, connection, done):
        self.connection = connection
        self.done = done

    def get(self, timeout):
        self.connection.settimeout(timeout)
        try:
            data = self.connection.recv(64 * 1024)
        except (socket.timeout, BlockingIOError):
            raise queue.Empty from None
        except ConnectionError:
            data = b""
        if not data:
            self.done.set()
            raise queue.Empty
        return data


class WineCapture:
    def __init__(self, python_exe, diagnostics=False):
        wine = shutil.which("wine") or shutil.which("wine64")
        if not wine:
            raise ValueError("找不到 Wine，请先安装 64 位 Wine")
        python_exe = Path(python_exe).resolve()
        if not python_exe.is_file():
            raise ValueError("指定的 Windows Python 不存在")
        script = PureWindowsPath("Z:/", *SCRIPT.parts[1:])
        self.command = [wine, str(python_exe), "-B", "-u", str(script)]
        self.log = None if diagnostics else subprocess.DEVNULL

    def run(self, channel, credentials, consumer, stop):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(.25)
            token = secrets.token_hex(32)
            request = json.dumps({"port": listener.getsockname()[1], "token": token,
                                  "channel": channel, "credentials": credentials})
            with subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=self.log, stderr=self.log) as worker:
                try:
                    with worker.stdin:
                        worker.stdin.write(request.encode("ascii") + b"\n")
                    until = time.monotonic() + 100
                    while not stop.is_set() and time.monotonic() < until:
                        if worker.poll() is not None:
                            raise RuntimeError("Wine 取流进程已退出，请检查 Seetong 登录和 SDK")
                        try:
                            client, _ = listener.accept()
                        except socket.timeout:
                            continue
                        with client:
                            client.settimeout(1)
                            try:
                                received = b""
                                while len(received) < len(token):
                                    chunk = client.recv(len(token) - len(received))
                                    if not chunk:
                                        break
                                    received += chunk
                            except OSError:
                                continue
                            if secrets.compare_digest(received, token.encode("ascii")):
                                done = threading.Event()
                                consumer(SocketPackets(client, done), done)
                                return
                    if not stop.is_set():
                        raise RuntimeError("等待 Wine 取流超时")
                finally:
                    try:
                        worker.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        worker.terminate()
                        try:
                            worker.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            worker.kill()


def run_worker():
    request = json.loads(sys.stdin.buffer.readline(16 * 1024))
    probe = load_probe()

    def relay(packets, done):
        with socket.create_connection(("127.0.0.1", request["port"]), timeout=5) as connection:
            connection.sendall(request["token"].encode("ascii"))
            try:
                while not done.is_set():
                    try:
                        packet = packets.get(timeout=.25)
                    except queue.Empty:
                        continue
                    connection.sendall(packet)
            except OSError:
                done.set()

    probe.main(["--capture", "--channel", str(request["channel"])],
               credentials=request["credentials"], consumer=relay)


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        load_probe().main(["--self-test", "--capture"])
    else:
        run_worker()
