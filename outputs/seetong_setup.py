"""Prepare and launch the Windows monitor from a Git checkout."""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import webbrowser
from pathlib import Path
from urllib.request import urlopen

from seetong_sdk import ROOT, pin

MODEL = ROOT / "seetong-yolox.onnx"
MODEL_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
    "models/object_detection_yolox/object_detection_yolox_2022nov_int8.onnx"
)
MODEL_SHA256 = "01a3b0f400b30bc1e45230e991b2e499ab42622485a330021947333fbaf03935"
VENV = ROOT.parent / ".venv"
ADDRESS = "http://127.0.0.1:8765/"


def install_model():
    if MODEL.exists():
        return  # The operator may already have selected a compatible model.
    print("正在下载 YOLOX 人体检测模型…", flush=True)
    with tempfile.NamedTemporaryFile(dir=ROOT, suffix=".tmp", delete=False) as file:
        pending = Path(file.name)
    try:
        with urlopen(MODEL_URL, timeout=30) as response, pending.open("wb") as target:
            shutil.copyfileobj(response, target)
        with pending.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != MODEL_SHA256:
                raise RuntimeError("YOLOX 下载文件的 SHA-256 校验失败")
        pending.replace(MODEL)
    finally:
        pending.unlink(missing_ok=True)


def find_ffmpeg():
    if ffmpeg := shutil.which("ffmpeg"):
        return Path(ffmpeg)
    winget = Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet"
    link = winget / "Links" / "ffmpeg.exe"
    if link.is_file():
        return link.resolve()
    return next((winget / "Packages").glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe"), None)


def main():
    try:
        with urlopen(ADDRESS, timeout=1) as response:
            page = response.read(128 * 1024)
            dashboard = re.search(rb"const session = '[0-9a-f]{64}';", page) is not None
    except OSError:
        dashboard = False
    if dashboard:
        webbrowser.open(ADDRESS)
        return

    try:
        pin()
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            "请先安装匹配的 Seetong 1.0.12.3 Windows 客户端，或按 README 放入已校验的 SDK 副本。"
            f"\n{error}"
        ) from error

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("正在通过 WinGet 安装 FFmpeg…", flush=True)
        subprocess.run(["winget", "install", "--id", "Gyan.FFmpeg", "--exact", "--source", "winget",
                        "--accept-source-agreements", "--accept-package-agreements"], check=True)
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("找不到 FFmpeg。请确认 ffmpeg.exe 在 PATH 中，再双击 start.cmd。")

    python = VENV / "Scripts" / "python.exe"
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(VENV)
    dependencies = subprocess.run(
        [str(python), "-c", "import numpy, PIL, onnxruntime"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if dependencies.returncode:
        pip = subprocess.run([str(python), "-m", "pip", "--version"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if pip.returncode:
            subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "numpy", "Pillow", "onnxruntime"], check=True)
    install_model()

    env = dict(os.environ, PATH=str(ffmpeg.parent) + os.pathsep + os.environ.get("PATH", ""))
    subprocess.run([str(python), "-B", str(ROOT / "seetong-monitor.py")], env=env, check=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
