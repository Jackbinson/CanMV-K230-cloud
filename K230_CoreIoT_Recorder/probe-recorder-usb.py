#!/usr/bin/env python3
"""Inspect K230 runtime capabilities over USBDBG; no camera files are written."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
SOURCE = '''
import sys, os, time, ujson, ubinascii
_result = {'implementation': str(sys.implementation), 'modules': {}}
for _name in ('_thread', 'socket', 'ssl', 'requests', 'urequests', 'uhashlib'):
 try:
  _module = __import__(_name)
  _result['modules'][_name] = {'available': True, 'file': getattr(_module, '__file__', ''), 'request': hasattr(_module, 'request')}
 except ImportError:
  _result['modules'][_name] = {'available': False}
try:
 import _thread
 _done = []
 def _probe_thread():
  _done.append(1)
 _thread.start_new_thread(_probe_thread, ())
 _thread.start_new_thread(_probe_thread, ())
 _begin = time.ticks_ms()
 while len(_done) < 2 and time.ticks_diff(time.ticks_ms(), _begin) < 2000:
  time.sleep_ms(20)
 _result['two_threads_work'] = len(_done) == 2
except Exception:
 _result['two_threads_work'] = False
try:
 with open('/sdcard/recorder/recording_config.json') as _f:
  _cfg = ujson.load(_f)
 _result['recorder_config'] = {k: _cfg.get(k) for k in ('enabled', 'camera_id', 'output_dir', 'width', 'height', 'clip_seconds', 'session_seconds', 'auto_start', 'reserve_mb')}
except Exception:
 _result['recorder_config'] = None
try:
 import uhashlib
 _result['source_hashes'] = {}
 for _file in ('main.py', 'recorder/record_video.py', 'recorder/clean_mp4.py', 'recorder/mp4_metrics.py', 'recorder/wifi_client.py'):
  _hash = uhashlib.sha256()
  with open('/sdcard/' + _file, 'rb') as _f:
   while True:
    _part = _f.read(1024)
    if not _part:
     break
    _hash.update(_part)
  _result['source_hashes'][_file] = ubinascii.hexlify(_hash.digest()).decode()
except Exception:
 _result['source_hashes_error'] = True
print(ujson.dumps(_result))
'''


def load_usb():
    spec = importlib.util.spec_from_file_location("recorder_usb", ROOT / "read-recorder-usb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    args = parser.parse_args()
    print("K230 must be idle; close CanMV IDE first. Inspection pauses the running program.", flush=True)
    camera = load_usb().CameraRepl(args.port)
    try:
        camera.enter()
        report = json.loads(camera.execute(SOURCE))
    finally:
        camera.close()
    path = ROOT / "k230-runtime-report.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report, indent=2))
    print("Report: " + str(path))
    print("No camera files changed. Restart K230 to resume its recorder.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print("Inspection failed: " + str(error), file=sys.stderr)
        sys.exit(1)
