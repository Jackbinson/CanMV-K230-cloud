#!/usr/bin/env python3
"""Install/roll back the recorder remote-control update via CanMV USBDBG."""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
BASELINE = ROOT / "recorder-source/20261111-195907-073264"
BUNDLE = ROOT / "recorder-update/recorder"
MODULES = ("google_ca.py", "recorder_https.py", "drive_upload.py", "mqtt_transport.py", "remote_control.py", "record_video.py")
FLAG = "/sdcard/recorder/maintenance.flag"
MANIFEST = "/sdcard/recorder/remote-install.json"
HELPERS = '''
import os, ujson, ubinascii, uhashlib
def _exists(p):
 try:
  os.stat(p)
  return True
 except OSError:
  return False
def _hash(p):
 h = uhashlib.sha256()
 with open(p, 'rb') as f:
  while True:
   b = f.read(1024)
   if not b:
    break
   h.update(b)
 return ubinascii.hexlify(h.digest()).decode()
def _copy(src, dst):
 with open(src, 'rb') as r:
  with open(dst, 'wb') as w:
   while True:
    b = r.read(1024)
    if not b:
     break
    if w.write(b) != len(b):
     raise OSError('short write')
def _save(p, value):
 with open(p, 'w') as f:
  ujson.dump(value, f)
  f.flush()
 if hasattr(os, 'sync'):
  os.sync()
def _allowed(p):
 return p in ('/sdcard/mqtt_config.json', '/sdcard/recorder/drive-webapp.json',
              '/sdcard/recorder/google-pins.json',
              '/sdcard/recorder/drive_upload.py', '/sdcard/recorder/mqtt_transport.py',
              '/sdcard/recorder/google_ca.py', '/sdcard/recorder/recorder_https.py',
              '/sdcard/recorder/remote_control.py', '/sdcard/recorder/record_video.py')
def _validate_restore(manifest):
 backup_dir = manifest['backup_dir']
 if not backup_dir.startswith('/sdcard/recorder/remote-backup-') or '..' in backup_dir:
  raise ValueError('rollback backup directory rejected')
 for item in manifest['files']:
  dest = item['destination']
  if not _allowed(dest):
   raise ValueError('rollback destination rejected')
  if (manifest.get('status') == 'installed' and _exists(dest) and
      _hash(dest) not in (item['sha256'], item.get('before_sha256'))):
   raise ValueError('file changed since install')
  if item['existed']:
   if len(item['backup_name']) != 2 or not item['backup_name'].isdigit():
    raise ValueError('backup name rejected')
   backup = backup_dir + '/' + item['backup_name']
   if _hash(backup) != item['before_sha256']:
    raise ValueError('backup checksum mismatch')
def _restore(manifest):
 _validate_restore(manifest)
 for item in manifest['files']:
  dest = item['destination']
  if item['existed']:
   _copy(manifest['backup_dir'] + '/' + item['backup_name'], dest)
   if _hash(dest) != item['before_sha256']:
    raise ValueError('restore checksum mismatch')
  elif _exists(dest):
   os.remove(dest)
 if hasattr(os, 'sync'):
  os.sync()
'''


def payload():
    files = {}
    for name in MODULES:
        data = (BUNDLE / name).read_bytes()
        compile(data, name, "exec")
        files["/sdcard/recorder/" + name] = data
    drive = json.loads((ROOT / "drive-webapp.json").read_text())
    mqtt = json.loads((ROOT / "mqtt_config.json").read_text())
    if len(drive.get("device_token", "")) < 32 or not mqtt.get("mqtt_access_token"):
        raise ValueError("Private Drive/CoreIoT config is incomplete.")
    if not drive.get("web_app_url", "").startswith("https://script.google.com/macros/s/") or not drive["web_app_url"].endswith("/exec"):
        raise ValueError("Invalid Apps Script URL in private config.")
    files["/sdcard/recorder/drive-webapp.json"] = (json.dumps(drive) + "\n").encode()
    files["/sdcard/mqtt_config.json"] = (json.dumps(mqtt) + "\n").encode()
    pins = json.loads((ROOT / "google-pins.json").read_text())
    if pins.get("version") != 1 or not pins.get("hosts"):
        raise ValueError("Verified Google certificate pins missing; run prepare-google-pins.py first.")
    files["/sdcard/recorder/google-pins.json"] = (json.dumps(pins) + "\n").encode()
    return files


def baseline_hashes():
    return {name: hashlib.sha256((BASELINE / name).read_bytes()).hexdigest() for name in
            ("main.py", "recorder/clean_mp4.py", "recorder/mp4_metrics.py", "recorder/wifi_client.py")}


def execute_json(camera, source):
    return json.loads(camera.execute(HELPERS + source))


def preflight(camera, files):
    old_hash = hashlib.sha256((BASELINE / "recorder/record_video.py").read_bytes()).hexdigest()
    new_hash = hashlib.sha256(files["/sdcard/recorder/record_video.py"]).hexdigest()
    source = '''
import _thread, socket, ssl
_checks = %r
_bad = [p for p in _checks if _hash('/sdcard/' + p) != _checks[p]]
_recorder_ok = _hash('/sdcard/recorder/record_video.py') in %r
_https = hasattr(socket, 'AF_INET') and hasattr(ssl, 'wrap_socket') and hasattr(ssl, 'CERT_REQUIRED')
with open('/sdcard/recorder/recording_config.json') as _f:
 _cfg = ujson.load(_f)
print(ujson.dumps({'protected_changed': _bad, 'recorder_matches': _recorder_ok,
 'https_supported': _https, 'maintenance_flag': _exists(%r),
 'recorder_enabled': _cfg.get('enabled') is True, 'output_dir': _cfg.get('output_dir')}))
''' % (baseline_hashes(), (old_hash, new_hash), FLAG)
    info = execute_json(camera, source)
    if info["protected_changed"] or not info["recorder_matches"]:
        raise RuntimeError("Camera source changed since backup; stopped without writing files.")
    if info["maintenance_flag"]:
        raise RuntimeError("Camera is in maintenance mode. Use --rollback if an earlier install was interrupted.")
    if not info["recorder_enabled"]:
        raise RuntimeError("Recorder is not enabled in existing config; no settings were changed.")
    if not info["https_supported"]:
        raise RuntimeError("Firmware lacks required IPv4/TLS verification support. No camera files changed.")
    print("Preflight OK: recorder baseline, IPv4/TLS capabilities, existing config.", flush=True)


def install(camera, files):
    preflight(camera, files)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stage = "/sdcard/recorder/remote-stage-" + stamp
    backup = "/sdcard/recorder/remote-backup-" + stamp
    execute_json(camera, "os.mkdir(%r)\nprint(ujson.dumps({'ok': True}))\n" % stage)
    entries = []
    for number, (destination, data) in enumerate(files.items()):
        staged = stage + "/%02d" % number
        execute_json(camera, "with open(%r, 'wb') as _f:\n pass\nprint(ujson.dumps({'ok': True}))\n" % staged)
        for offset in range(0, len(data), 4096):
            chunk = data[offset:offset + 4096]
            code = """
with open(%r, 'r+b') as _f:
 _f.seek(%d)
 _b = ubinascii.unhexlify(%r)
 if _f.write(_b) != len(_b):
  raise OSError('short write')
print(ujson.dumps({'ok': True}))
""" % (staged, offset, chunk.hex())
            execute_json(camera, code)
        expected = hashlib.sha256(data).hexdigest()
        code = """
if _hash(%r) != %r:
 raise ValueError('staging checksum mismatch')
if %r.endswith('.py'):
 with open(%r) as _f:
  compile(_f.read(), %r, 'exec')
print(ujson.dumps({'ok': True}))
""" % (staged, expected, destination, staged, destination)
        execute_json(camera, code)
        entries.append({"destination": destination, "staged": staged,
                        "sha256": expected, "backup_name": "%02d" % number})
        print("Staged and verified: " + destination.rsplit("/", 1)[1], flush=True)
    source = '''
_entries = %r
os.mkdir(%r)
for _item in _entries:
 _p = _item['destination']
 if not _allowed(_p):
  raise ValueError('destination rejected')
 _item['existed'] = _exists(_p)
 if _item['existed']:
  _item['before_sha256'] = _hash(_p)
  _dest = %r + '/' + _item['backup_name']
  _copy(_p, _dest)
  if _hash(_dest) != _item['before_sha256']:
   raise ValueError('backup checksum mismatch')
_manifest = {'backup_dir': %r, 'files': _entries, 'status': 'installing'}
_save(%r, _manifest)
with open(%r, 'w') as _f:
 _f.write('remote install in progress')
if hasattr(os, 'sync'):
 os.sync()
try:
 for _item in _entries:
  _copy(_item['staged'], _item['destination'])
  if _hash(_item['destination']) != _item['sha256']:
   raise ValueError('installed checksum mismatch')
 _manifest['status'] = 'installed'
 _save(%r, _manifest)
 os.remove(%r)
 if hasattr(os, 'sync'):
  os.sync()
 print(ujson.dumps({'installed': True, 'backup_dir': %r}))
except Exception:
 try:
  _restore(_manifest)
  _manifest['status'] = 'rolled_back'
  _save(%r, _manifest)
  os.remove(%r)
  if hasattr(os, 'sync'):
   os.sync()
  print(ujson.dumps({'installed': False, 'rollback': True}))
 except Exception:
  print(ujson.dumps({'installed': False, 'rollback': False, 'maintenance': True}))
''' % (entries, backup, backup, backup, MANIFEST, FLAG, MANIFEST, FLAG, backup, MANIFEST, FLAG)
    result = execute_json(camera, source)
    if not result.get("installed"):
        raise RuntimeError("Installation failed; rollback " + ("completed." if result.get("rollback") else "requires --rollback; startup remains in maintenance mode."))
    print("INSTALLED. Original recorder/config backup: " + result["backup_dir"], flush=True)


def rollback(camera):
    source = '''
with open(%r) as _f:
 _manifest = ujson.load(_f)
if _manifest.get('status') not in ('installed', 'installing', 'rolling_back'):
 raise ValueError('no active installation')
_validate_restore(_manifest)
with open(%r, 'w') as _f:
 _f.write('remote rollback in progress')
if hasattr(os, 'sync'):
 os.sync()
_manifest['status'] = 'rolling_back'
_save(%r, _manifest)
_restore(_manifest)
_manifest['status'] = 'rolled_back'
_save(%r, _manifest)
os.remove(%r)
if hasattr(os, 'sync'):
 os.sync()
print(ujson.dumps({'rolled_back': True}))
''' % (MANIFEST, FLAG, MANIFEST, MANIFEST, FLAG)
    result = execute_json(camera, source)
    if not result.get("rolled_back"):
        raise RuntimeError("Rollback could not be verified.")
    print("ROLLED BACK to the recorder/config saved before installation.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--preflight", action="store_true", help="Inspect compatibility only, without writing files")
    args = parser.parse_args()
    files = None if args.rollback else payload()
    spec = importlib.util.spec_from_file_location("recorder_usb", ROOT / "read-recorder-usb.py")
    usb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usb)
    print("K230 must be idle. Quit CanMV IDE; this pauses the running program.", flush=True)
    camera = usb.CameraRepl(args.port)
    try:
        camera.enter()
        if args.rollback:
            rollback(camera)
        elif args.preflight:
            preflight(camera, files)
        else:
            install(camera, files)
    finally:
        camera.close()
    print("Restart K230. Leave KEY released during its 5-second boot selector.", flush=True)
    print("Main boot selector, MP4 hardware modules, recording settings and local videos were preserved.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print("Update stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
