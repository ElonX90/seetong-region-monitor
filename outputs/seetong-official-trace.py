"""Compare the official Seetong client's successful preview calls."""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "work" / "seetong-deps"))
import frida


SOURCE = r"""
const stats = {};
const registered = [];
const hooks = {
  'Funclib.dll': [
    'FC_AddWatch', 'FC_AddWatchEx', 'FC_Loc_RealPlay', 'FC_Loc_RealPlayEx',
    'FC_SetMediaRecvCallBack', 'FC_SetRelayMediaTransport', 'FC_AddDeviceStream'
  ],
  'PlayCtrl.dll': ['PC_OpenStream', 'PC_InputVideoData']
};

function watchKey(pointer) {
  if (pointer.isNull()) return {shape: 'null'};
  try {
    const value = pointer.readCString();
    if (!value) return {shape: 'empty'};
    const suffix = value.match(/-CH-(\d+)$/i);
    const base = suffix ? value.slice(0, suffix.index) : value;
    const matches = new Set();
    for (const record of registered) {
      for (const field of ['devId', 'devSn', 'id', 'cloudId', 'gatewayId']) {
        if (record[field] !== undefined && String(record[field]) === base) matches.add(field);
      }
    }
    return {
      shape: suffix ? 'channel suffix' : /^\d+$/.test(value) ? 'digits' :
        /^[0-9a-f]+$/i.test(value) ? 'hexadecimal' : 'other',
      length: value.length, matches: [...matches],
      channel: suffix ? Number(suffix[1]) : null
    };
  } catch (_) { return {shape: 'unreadable'}; }
}

Process.attachModuleObserver({
  onAdded(module) {
    const names = Object.keys(hooks).find(name => name.toLowerCase() === module.name.toLowerCase());
    if (!names) return;
    for (const symbol of hooks[names]) {
      const address = module.findExportByName(symbol);
      if (!address) continue;
      stats[symbol] = 0;
      Interceptor.attach(address, {
        onEnter(args) {
          stats[symbol]++;
          if (symbol === 'FC_AddDeviceStream') {
            try {
              const records = JSON.parse(args[0].readUtf8String());
              if (!Array.isArray(records)) return;
              registered.length = 0;
              for (const record of records) {
                if (!record || typeof record !== 'object') continue;
                const fields = ['devId', 'devSn', 'id', 'cloudId', 'gatewayId'];
                registered.push(Object.fromEntries(fields.filter(field => field in record)
                  .map(field => [field, record[field]])));
              }
              send({symbol, records: registered.length,
                fields: [...new Set(records.flatMap(record => record && typeof record === 'object'
                  ? Object.keys(record) : []))].sort()});
            } catch (_) { send({symbol, error: 'registration JSON unavailable'}); }
          } else if (symbol === 'FC_AddWatch' || symbol === 'FC_AddWatchEx') {
            const options = [];
            for (let i = 1; i <= (symbol === 'FC_AddWatchEx' ? 4 : 3); i++)
              options.push(args[i].toInt32());
            send({symbol, key: watchKey(args[0]), options});
            this.reportResult = true;
          } else if (symbol.startsWith('FC_Loc_RealPlay')) {
            send({symbol});
            this.reportResult = true;
          }
        },
        onLeave(retval) {
          if (this.reportResult) send({symbol, result: retval.toInt32()});
        }
      });
    }
    send({module: module.name, hooked: hooks[names].filter(name => name in stats)});
  }
});
rpc.exports = { snapshot() { return stats; } };
"""


def main():
    root = Path(os.environ["ProgramFiles(x86)"]) / "Seetong"
    device = frida.get_local_device()
    spawned = None
    session = None
    try:
        if "--launch" in sys.argv:
            spawned = device.spawn([str(root / "Seetong.exe")], cwd=str(root))
            pid = spawned
        else:
            pid = int(sys.argv[1]) if len(sys.argv) > 1 else device.get_process("Seetong.exe").pid
        session = device.attach(pid)
        script = session.create_script(SOURCE)
        script.on("message", lambda message, _data: print(json.dumps(message), flush=True))
        script.load()
        if spawned is not None:
            device.resume(spawned)
            spawned = None
        print("Open or restart live preview in Seetong. Tracing for 60 seconds.", flush=True)
        time.sleep(60)
        print("Call counts:", json.dumps(script.exports_sync.snapshot()), flush=True)
    except (frida.ProcessNotFoundError, frida.PermissionDeniedError, frida.NotSupportedError) as exc:
        print(type(exc).__name__ + ":", exc, flush=True)
        return 1
    finally:
        if session is not None:
            session.detach()
        if spawned is not None:
            device.resume(spawned)
    return 0


if __name__ == "__main__":
    sys.exit(main())
