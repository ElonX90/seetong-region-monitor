"""Validate Seetong cloud authentication and optionally probe independent playback."""

import argparse
import ctypes
import getpass
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from seetong_sdk import runtime_root
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def business_origin(host):
    if not isinstance(host, str):
        return None
    value = urlsplit(host if "://" in host else "https://" + host)
    if (value.scheme != "https" or not value.hostname
            or not re.fullmatch(r"(?:[a-z0-9-]+\.)+(?:seetong\.com|seetong\.cn)", value.hostname)
            or value.netloc.lower() != value.hostname
            or value.path not in ("", "/") or value.query or value.fragment):
        return None
    return "https://" + value.hostname


def describe_device_list(payload):
    if not isinstance(payload, dict):
        return
    print("Device response fields:", ", ".join(sorted(payload)))
    data = payload.get("data")
    if isinstance(data, dict):
        print("Device data fields:", ", ".join(sorted(data)))
    if isinstance(data, list):
        lists = {"data": data}
    else:
        lists = {key: value for key, value in (data or {}).items()
                 if isinstance(value, list)} if isinstance(data, dict) else {}
    for key, items in lists.items():
        keys = sorted({field for item in items if isinstance(item, dict) for field in item})
        print(f"Device list {key}: {len(items)} entries")
        if keys:
            print("Device entry fields:", ", ".join(keys))


def describe_signaling(payload):
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if isinstance(code, int):
        print("API code:", code)
    data = payload.get("data", payload)
    if isinstance(data, dict):
        print("Signal data fields:", ", ".join(sorted(data)))
        for key in ("relayServers", "vipRelayServers", "thirdParty", "accessNode"):
            if key in data:
                describe_structure(key, data[key])
    elif isinstance(data, list):
        print("Signal records:", len(data))
        keys = sorted({key for record in data if isinstance(record, dict) for key in record})
        print("Signal record fields:", ", ".join(keys))
        for record in data[:1]:
            if isinstance(record, dict) and "p2pServer" in record:
                describe_structure("p2pServer", record["p2pServer"])


def describe_structure(label, value):
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, dict):
        known_fields = {
            "accessNode", "address", "host", "ip", "keyVersion", "port", "relayNodes",
            "relayServers", "relayToken", "server", "sigClientId", "sigToken", "token",
        }
        fields = ", ".join(sorted(known_fields.intersection(value)))
        print(f"{label}: object ({len(value)} fields)" + (f", known fields: {fields}" if fields else ""))
    elif isinstance(value, list):
        print(f"{label}: array ({len(value)} entries)")
        if value and isinstance(value[0], dict):
            describe_structure(label + "[0]", value[0])
    else:
        print(f"{label}: {type(value).__name__}, populated: {bool(value)}")


def request_json(url, headers, data=None, *, redact=()):
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers, data=data), timeout=20) as response:
            print(f"HTTP {response.status}")
            return json.loads(response.read(1_000_000))
    except HTTPError as error:
        print(f"HTTP {error.code}")
        if error.code == 405 and error.headers.get("Allow"):
            print("Allowed methods:", error.headers["Allow"])
        try:
            payload = json.loads(error.read(65536))
            if isinstance(payload, dict):
                print("Error fields:", ", ".join(sorted(payload)))
                for field in ("code", "msg"):
                    value = payload.get(field)
                    if not isinstance(value, (str, int)):
                        continue
                    message = str(value)
                    for secret in redact:
                        if secret:
                            message = message.replace(secret, "[redacted]")
                    print(f"{field}: {message[:300]}")
        except (ValueError, UnicodeError):
            pass
    except (URLError, TimeoutError, ValueError) as error:
        print("Request failed:", type(error).__name__)
    return None


MEDIA_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_void_p,
)
EVENT_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int,
)
LOG_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)
FUNCLIB_SHA256 = "363cd5b6d9490dbb47ac0e65a1f3f3d72a14611d1d0a38b3287ca5caf9077b88"


def bind(sdk, name, *argtypes):
    function = getattr(sdk, name)
    function.argtypes = argtypes
    function.restype = ctypes.c_int
    return function


def native_header(headers):
    header = bytearray(0x1c0)
    values = (
        (0x00, headers["Seetong-os"]),
        (0x20, headers["Seetong-Lang"]),
        (0x40, headers["Seetong-Phone-Brand"]),
        (0x60, headers["Seetong-Phone-Type"]),
        (0x80, headers["Seetong-App-Name"]),
        (0xa0, headers["Seetong-App-Version"]),
        (0xc0, headers["Seetong-Client-Id"]),
        (0x100, "app-auth.seetong.com"),
        (0x140, "app-auth-bak.seetong.com"),
    )
    for offset, value in values:
        encoded = value.encode("ascii")
        limit = 32 if offset < 0xc0 else 64
        if len(encoded) >= limit:
            raise ValueError("SDK header field too long")
        header[offset:offset + len(encoded)] = encoded
    return ctypes.create_string_buffer(bytes(header))


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def summarize_stream_response(body):
    tags = tuple(sorted({
        name.decode("ascii").upper()
        for name in re.findall(rb"<\s*([a-zA-Z][a-zA-Z0-9_]{0,31})(?=[\s/>])", body)
    }))[:24]
    code_names = (
        rb"msg_code|msg_flag|msg_chn|channel_id|channelid|videostream|"
        rb"result|ret|error_?code|errcode|errno|status|code"
    )
    attributes = re.findall(
        rb"\b(" + code_names + rb")\s*=\s*[\"'](-?\d{1,9})[\"']",
        body, re.IGNORECASE,
    )
    elements = re.findall(
        rb"<(" + code_names + rb")>\s*(-?\d{1,9})\s*</\1\s*>",
        body, re.IGNORECASE,
    )
    fields = tuple(sorted({(key.decode("ascii").lower(), int(value))
                           for key, value in attributes + elements}))[:24]
    return len(body), tags, fields


def watch_key(device_id, channel):
    return f"{device_id}-CH-{channel}".encode("ascii")


def play_live(ffplay, channel, packets, done, first_packet):
    player = subprocess.Popen(
        [ffplay, "-hide_banner", "-loglevel", "warning", "-nostats", "-an",
         "-window_title", f"Seetong - Channel {channel}", "-i", "pipe:0"],
        stdin=subprocess.PIPE,
    )

    def feed_player():
        try:
            with player.stdin:
                while not done.is_set():
                    try:
                        packet = packets.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    player.stdin.write(packet)
                    player.stdin.flush()
        except (BrokenPipeError, OSError):
            done.set()

    feeder = threading.Thread(target=feed_player, daemon=True)
    feeder.start()
    try:
        deadline = time.monotonic() + 30
        while player.poll() is None and not done.wait(0.2):
            if not first_packet.is_set() and time.monotonic() >= deadline:
                print("No live video received within 30 seconds")
                break
        if player.poll() not in (None, 0):
            print("FFplay exited with status:", player.returncode)
    finally:
        done.set()
        if player.poll() is None:
            player.terminate()
        player.wait(timeout=5)
        feeder.join(timeout=2)


def capture_probe(sdk, headers, account, origin, profile, device, signal_config, channel, ffplay=None, consumer=None):
    string = ctypes.c_char_p
    integer = ctypes.c_int
    init = bind(sdk, "FC_initWithHeader", ctypes.c_void_p, string, string)
    use_https = bind(sdk, "FC_SetHttpsProtocol")
    set_tokens = bind(sdk, "FC_SetAccessToken", string, string, string, string, string, ctypes.c_uint)
    set_profile = bind(sdk, "FC_SetUserInfo2", string)
    signal_login = bind(sdk, "FC_LoginSigServer", string, string)
    add_device = bind(sdk, "FC_AddDeviceStream", string)
    watch = bind(sdk, "FC_AddWatch", string, integer, integer, integer)
    stop = bind(sdk, "FC_StopWatch", string)
    callback = bind(sdk, "FC_SetMediaRecvCallBack", MEDIA_CALLBACK)
    event_callback = bind(sdk, "FC_SetMsgRspCallBack", EVENT_CALLBACK)
    log_callback = bind(sdk, "FC_SetfcLogCallBackEx", LOG_CALLBACK, integer)
    cleanup = bind(sdk, "FC_Free")

    def as_bytes(value):
        return str(value if value is not None else "").encode("utf-8")

    frames = []
    lock = threading.Lock()
    done = threading.Event()
    received_bytes = 0
    packet_count = 0
    queue_full = False
    first_packet = threading.Event()
    packets = queue.Queue(maxsize=64) if ffplay or consumer else None
    media_counts = {}
    event_counts = {}
    event_sequence = []
    sdk_events = set()
    sdk_status = set()
    stream_responses = set()
    event_patterns = (
        ("device authentication accepted", b"authentication response ok!"),
        ("device authentication rejected", b"authentication response failed!"),
        ("device authentication rejected", b"authentication failed, username"),
        ("device authentication rejected", b"tps_msg_notify_auth_failed"),
        ("device key negotiation failed", b"tps encrypt method version get error"),
        ("signaling client ID assigned", "信令服务器返回clientid".encode()),
        ("stream parameters rejected", b"getlivestreamparam failed"),
        ("stream response accepted", b"new start stream ok"),
        ("stream request rejected", b"request video stream return error"),
        ("stream subscription rejected", b"add watch failed"),
        ("stream subscription accepted", b"add watch success"),
        ("stream timed out", b"no video stream timeout"),
        ("network read timed out", b"on_pg_event_read_timeout"),
        ("network event timed out", b"read event timeout"),
        ("network TCP timed out", b"pg_event_tcp_timeout"),
        ("signaling refresh started", b"redirectsiotaccess start"),
        ("session closed", b"on_pg_event_close"),
    )

    @LOG_CALLBACK
    def receive_log(_level, message):
        if not message:
            return
        normalized = message.lower()
        matches = {name for name, marker in event_patterns if marker in normalized}
        statuses = re.findall(
            rb"\b(ret|err|errorcode|errno|result|status|reason|code)\s*[=:]\s*(-?\d{1,9})\b",
            normalized,
        ) if matches else []
        reply = None
        if b"getlivestreamparam failed" in normalized and b"recv=" in normalized:
            reply = summarize_stream_response(normalized.split(b"recv=", 1)[1])
        elif b"start stream failed[" in normalized:
            code = re.search(rb"start stream failed\[(-?\d{1,6})\]", normalized)
            if code:
                statuses.append((b"stream-parser-return", code[1]))
        with lock:
            sdk_events.update(matches)
            if len(sdk_status) < 32:
                sdk_status.update((label.decode("ascii"), int(value)) for label, value in statuses[:4])
            if reply and len(stream_responses) < 8:
                stream_responses.add(reply)

    @EVENT_CALLBACK
    def receive_event(event_type, _data, _size, _context, _status):
        with lock:
            if event_type not in event_counts:
                event_sequence.append(event_type)
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        return 0

    @MEDIA_CALLBACK
    def receive(_source, media_type, payload, size, _metadata):
        nonlocal received_bytes, packet_count, queue_full
        with lock:
            media_counts[media_type] = media_counts.get(media_type, 0) + 1
        if media_type != 0 or not payload or not 0 < size <= 4 * 1024 * 1024:
            return 0
        with lock:
            if done.is_set():
                return 0
            packet = ctypes.string_at(payload, size)
            if packets is not None:
                try:
                    packets.put_nowait(packet)
                except queue.Full:
                    queue_full = True
                    done.set()
                    return 0
                first_packet.set()
            else:
                frames.append(packet)
            packet_count += 1
            received_bytes += size
            if packets is None and (received_bytes >= 12 * 1024 * 1024 or len(frames) >= 180):
                done.set()
        return 0

    device_key = watch_key(device["devId"], channel)
    header = native_header(headers)
    use_https()
    log_callback(receive_log, 5)
    result = init(header, b"", b"")
    print("SDK initialization:", result, flush=True)
    if result != 0:
        return
    watch_started = False
    try:
        backup = business_origin(account.get("bizDomainBackup")) or origin
        expiry = int(account.get("expires_in") or 0)
        if expiry:
            expiry += int(time.time())
        result = set_tokens(
            as_bytes(account["access_token"]), as_bytes(account.get("refresh_token")),
            as_bytes(urlsplit(origin).hostname), as_bytes(urlsplit(backup).hostname),
            b"", expiry,
        )
        print("SDK access token configuration:", result, flush=True)
        result = set_profile(compact_json(profile))
        print("SDK user profile:", result, flush=True)
        if result < 0:
            return
        event_callback(receive_event)
        signal_id = signal_config.get("sigClientId") or headers["Seetong-Client-Id"]
        # The second argument is a JSON object consumed by the access-node parser.
        signal_params = {key: signal_config[key] for key in (
            "accessNode", "accessJwtToken", "relayJwtToken", "signKeyVer", "relayNodes", "vipRelayNodes",
        ) if key in signal_config}
        print("SDK signal parameters supplied:", ", ".join(sorted(signal_params)) or "none", flush=True)
        result = signal_login(as_bytes(signal_id), compact_json(signal_params) if signal_params else b"")
        print("SDK signaling start:", result, flush=True)
        callback(receive)
        result = add_device(compact_json([device]))
        print("SDK device registration:", result, flush=True)
        if result < 0:
            return
        # The official client used FC_AddWatch(key, 1, 0, 1) during successful playback.
        result = watch(device_key, 1, 0, 1)
        print(f"SDK start preview (channel {channel}, stream 1):", result, flush=True)
        watch_started = result >= 0
        if watch_started and consumer:
            consumer(packets, done)
        elif watch_started and ffplay:
            print("Opening live preview. Close the player or press Ctrl+C to stop.", flush=True)
            play_live(ffplay, channel, packets, done, first_packet)
        elif watch_started:
            print("Waiting up to 30 seconds for live video", flush=True)
            done.wait(30)
    finally:
        done.set()
        if watch_started:
            stop(device_key)
        callback(MEDIA_CALLBACK())
        cleanup()
    print("Video packets received:", packet_count)
    if queue_full:
        print("Preview stopped because FFplay could not keep up with incoming video")
    print("SDK event counts:", dict(sorted(event_counts.items())))
    print("SDK first occurrence order:", event_sequence)
    print("SDK media type counts:", dict(sorted(media_counts.items())))
    print("SDK recognized messages:", ", ".join(sorted(sdk_events)) or "none")
    print("SDK numeric statuses:", sorted(sdk_status))
    print("SDK stream response summaries (bytes, XML tags, numeric fields):",
          sorted(stream_responses))
    if frames:
        verify_decode(b"".join(frames))


def verify_decode(payload):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("FFmpeg unavailable; decode not verified")
        return
    attempts = ([], ["-f", "h264"], ["-f", "hevc"])
    for input_flags in attempts:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-xerror",
                 *input_flags, "-i", "pipe:0", "-an", "-frames:v", "1",
                 "-f", "framemd5", "pipe:1"],
                input=payload, capture_output=True, timeout=12,
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0 and any(
            line.split(b",", 1)[0].strip().isdigit()
            for line in result.stdout.splitlines()
        ):
            print("FFmpeg decoded a live frame:", input_flags[-1] if input_flags else "auto")
            return
    print("FFmpeg could not decode a frame from the captured packets")


def main(argv=None, *, credentials=None, consumer=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--play", action="store_true", help="Open a live FFplay preview")
    parser.add_argument("--channel", type=int, choices=range(1, 4), default=1)
    args = parser.parse_args(argv)
    root = runtime_root()
    library = root / "Funclib.dll"
    binary = library.read_bytes()
    basic = re.search(rb"c2VldG9uZ19jbG91ZF9tZW1iZXI6[A-Za-z0-9+/=]+", binary)
    if basic is None:
        raise SystemExit("Unsupported Funclib.dll build")
    self_test = args.self_test
    capture = args.capture or args.play
    ffplay = shutil.which("ffplay") if args.play else None
    if args.play and not ffplay:
        parser.error("FFplay was not found on PATH")
    if capture and hashlib.sha256(binary).hexdigest() != FUNCLIB_SHA256:
        raise SystemExit("Capture probe supports the analyzed Funclib.dll build only")
    if credentials is not None:
        username, password = credentials
    else:
        try:
            username = "local-test" if self_test else input("Seetong cloud username: ").strip()
            password = "local-test" if self_test else getpass.getpass("Seetong cloud password: ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("No request sent")
    if not username or not password:
        raise SystemExit("No request sent")
    with os.add_dll_directory(str(root)):
        sdk = ctypes.CDLL(str(library))
        password_transform = sdk.FC_MD5Password
        password_transform.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
        password_transform.restype = None
        transformed = ctypes.create_string_buffer(1024)
        password_transform(password.encode("utf-8"), transformed)
    if self_test:
        print("Local DLL loaded and password transform returned", len(transformed.value), "bytes")
        return
    headers = {
            "Authorization": "Basic " + basic.group().decode("ascii"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/xml, */*",
            "User-Agent": "tpsee/app",
            "Seetong-os": "win",
            "Seetong-Lang": "zh_CN",
            "Seetong-Phone-Brand": "Windows",
            "Seetong-Phone-Type": "none",
            "Seetong-App-Name": "seetong-pc",
            "Seetong-App-Version": "x64_1.0.12.3",
            "Seetong-Client-Id": "seetong-pc-" + uuid.uuid4().hex,
    }
    login = request_json(
        "https://app-auth.seetong.com/seetong-member-auth/oauth/token",
        headers,
        urlencode({
            "grant_type": "seetong_password",
            "username": username,
            "password": transformed.value.decode("ascii"),
        }).encode("ascii"),
        redact=(username, password, transformed.value.decode("ascii")),
    )
    if not isinstance(login, dict):
        return
    print("Login response fields:", ", ".join(sorted(login)))
    if isinstance(login.get("data"), dict):
        print("Login data fields:", ", ".join(sorted(login["data"])))
    account = login if login.get("access_token") else login.get("data")
    token = account.get("access_token") if isinstance(account, dict) else None
    print("Access token received:", bool(token))
    if not isinstance(token, str) or not token:
        return
    origin = business_origin(account.get("bizDomain") or login.get("bizDomain"))
    if origin is None:
        print("Device query skipped: unsupported business domain")
        return
    device_headers = {key: value for key, value in headers.items() if key != "Authorization"}
    device_headers["Seetong-Auth"] = "bearer " + token
    print("Querying cloud device list")
    devices = request_json(
        origin + "/seetong-member-device/device/list?current=1&size=100",
        device_headers,
        redact=(username, password, token, account.get("refresh_token")),
    )
    describe_device_list(devices)
    data = devices.get("data") if isinstance(devices, dict) else None
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        print("Signal probe skipped: expected exactly one device")
        return
    device = records[0]
    dev_id = device.get("devId")
    if not isinstance(dev_id, (str, int)) or not dev_id:
        print("Signal probe skipped: no device ID")
        return
    # Keep identifiers, account tokens and device credentials out of diagnostics.
    sensitive = tuple(str(value) for value in (
        username, password, token, account.get("refresh_token"), dev_id,
        device.get("devSn"), device.get("devLoginPassword"), device.get("devLoginUser"),
    ) if value)
    print("Querying signaling profile")
    profile_response = request_json(
        "https://app-auth.seetong.com/seetong-client/user/info?refreshServer=1",
        device_headers, redact=sensitive,
    )
    describe_signaling(profile_response)
    print("Querying P2P device configuration")
    p2p_response = request_json(
        origin + "/seetong-member-device/device/p2p/list?" + urlencode({"devIds": dev_id}),
        device_headers, redact=sensitive,
    )
    describe_signaling(p2p_response)
    if capture:
        profile = profile_response.get("data") if isinstance(profile_response, dict) else None
        p2p = p2p_response.get("data") if isinstance(p2p_response, dict) else None
        matched = next((record for record in p2p
                        if isinstance(record, dict) and str(record.get("devId")) == str(dev_id)), None) if isinstance(p2p, list) else None
        if not isinstance(profile, dict) or not matched:
            print("Capture probe skipped: signaling profile or matching P2P record missing")
            return
        with os.add_dll_directory(str(root)):
            print("Querying authenticated signaling access node")
            access_response = request_json(
                "https://app-auth.seetong.com/seetong-client/client/access-node",
                device_headers, redact=sensitive,
            )
            describe_signaling(access_response)
            access = access_response.get("data") if isinstance(access_response, dict) else None
            if isinstance(access, str):
                try:
                    access = json.loads(access)
                except ValueError:
                    access = None
            # The official client registers the original device list record.
            capture_probe(sdk, headers, account, origin, profile, device,
                          access if isinstance(access, dict) else {}, args.channel, ffplay, consumer)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped")
