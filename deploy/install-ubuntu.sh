#!/usr/bin/env bash
set -euo pipefail

project=$(cd -- "$(dirname -- "$0")/.." && pwd)
app=/opt/seetong-monitor
state=/var/lib/seetong-monitor
service=seetong-monitor.service
unit=/etc/systemd/system/$service
account=seetong-monitor
sdk_relative=seetong-sdk/1.0.12.3

if [[ ${1:-} == --help ]]; then
  printf 'Usage: sudo bash deploy/install-ubuntu.sh [path-to-verified-sdk-directory]\n'
  exit 0
fi
if (( $# > 1 )); then
  echo 'Pass the SDK directory as the only argument.' >&2
  exit 2
fi
if [[ $(id -u) != 0 || $(uname -m) != x86_64 ]]; then
  echo 'Run as root on x86_64 Ubuntu 24.04.' >&2
  exit 1
fi
source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 24.04 ]]; then
  echo 'This installer supports Ubuntu 24.04. See README for manual setup.' >&2
  exit 1
fi
if [[ $(ps -p 1 -o comm=) != systemd ]]; then
  echo 'This installer needs systemd as PID 1. See README for manual setup.' >&2
  exit 1
fi

sdk=${1:-$project/outputs/$sdk_relative}
if [[ $# == 0 && ! -d $sdk ]]; then
  sdk=$app/outputs/$sdk_relative
fi
if [[ ! -d $sdk ]]; then
  echo 'Supply the checked Seetong 1.0.12.3 DLL directory. See README.' >&2
  exit 1
fi

umask 022
exec 9>/run/lock/seetong-monitor-install.lock
flock -n 9 || { echo 'Another Seetong installation is running.' >&2; exit 1; }
existing_unit=$(systemctl show "$service" -p FragmentPath --value)
if [[ -n $existing_unit ]] && ! grep -Fq 'WorkingDirectory=/opt/seetong-monitor' "$existing_unit"; then
  echo 'A different systemd unit already uses the seetong-monitor service name.' >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3-venv wine wine64 xvfb xauth ffmpeg curl unzip ca-certificates

stage=$(mktemp -d /opt/.seetong-monitor.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/outputs/$sdk_relative" "$stage/win-python"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$project/outputs" python3 - "$sdk" "$stage/outputs/$sdk_relative" <<'PY'
import shutil
import sys
from pathlib import Path

from seetong_sdk import validate

source, target = map(Path, sys.argv[1:])
try:
    for name in validate(source):
        shutil.copyfile(source / name, target / name)
    validate(target)
except (OSError, RuntimeError) as error:
    raise SystemExit(str(error)) from None
PY

fetch() {
  local url=$1 destination=$2 expected=$3
  curl --proto '=https' -fLsS --retry 2 --connect-timeout 15 --max-time 180 "$url" -o "$destination"
  printf '%s  %s\n' "$expected" "$destination" | sha256sum -c -
}

fetch 'https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg' \
  "$stage/windows-python.nupkg" '0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8'
unzip -q "$stage/windows-python.nupkg" 'tools/*' -d "$stage/win-python"
fetch 'https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/object_detection_yolox/object_detection_yolox_2022nov_int8.onnx' \
  "$stage/outputs/seetong-yolox.onnx" '01a3b0f400b30bc1e45230e991b2e499ab42622485a330021947333fbaf03935'

for name in seetong-monitor.py seetong-monitor.html seetong-login-probe.py \
  seetong_sdk.py seetong-sdk.sha256.json seetong_wine.py seetong_recording.py seetong_alerts.py seetong_event_share.py; do
  install -m 0644 "$project/outputs/$name" "$stage/outputs/$name"
done

if ! id "$account" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$state" --shell /usr/sbin/nologin "$account"
fi
install -d -o "$account" -g "$account" -m 0700 "$state" "$state/data"
install -d -m 0755 "$app" "$app/outputs" "$app/win-python"
if [[ ! -x $app/venv/bin/python ]]; then
  python3 -m venv "$app/venv"
fi
"$app/venv/bin/python" -m pip install --disable-pip-version-check numpy onnxruntime Pillow
"$app/venv/bin/python" -c 'import numpy, onnxruntime, PIL'

cp -R -- "$stage/outputs/." "$app/outputs/"
cp -R -- "$stage/win-python/." "$app/win-python/"
if [[ -z $existing_unit ]]; then
  runuser -u "$account" -- env HOME="$state" WINEPREFIX="$state/wine" WINEDEBUG=-all \
    timeout 120s xvfb-run -a wine "$app/win-python/tools/python.exe" -B \
    'Z:\opt\seetong-monitor\outputs\seetong_wine.py' --self-test
  install -m 0644 "$project/deploy/$service" "$unit"
fi
systemctl daemon-reload
systemctl enable "$service"
systemctl restart "$service"
systemctl is-active "$service"
printf 'Seetong service installed. See README for SSH forwarding and first login.\n'
