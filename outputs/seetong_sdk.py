"""Pin the verified Seetong runtime from the local Windows installation."""

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PINNED = ROOT / "seetong-sdk" / "1.0.12.3"


def installed_root():
    return Path(os.environ["ProgramFiles(x86)"]) / "Seetong"


def validate(root):
    checksums = json.loads((ROOT / "seetong-sdk.sha256.json").read_text(encoding="utf-8"))
    for name, expected in checksums.items():
        file = root / name
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Seetong SDK 文件缺失或版本不同：{name}")
    return checksums


def runtime_root():
    if PINNED.exists():
        validate(PINNED)
        return PINNED
    root = installed_root()
    validate(root)
    return root


def pin():
    if not PINNED.exists():
        source = installed_root()
        checksums = validate(source)
        PINNED.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="staging-", dir=PINNED.parent))
        try:
            for name in checksums:
                shutil.copyfile(source / name, staging / name)
            validate(staging)
            try:
                staging.rename(PINNED)
            except FileExistsError:
                validate(PINNED)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    validate(PINNED)
    return PINNED


if __name__ == "__main__":
    print(pin())
