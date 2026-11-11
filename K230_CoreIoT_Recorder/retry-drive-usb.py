#!/usr/bin/env python3
"""Diagnose K230 HTTPS and retry an existing finalized test clip without recording."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import runpy

ROOT = Path(__file__).resolve().parent
FIELDS = {"stage", "host", "families", "legacy_wrap", "ssl_context", "dns_hook", "tls_hook", "phase", "error_code",
          "error_type", "errno", "requests_line", "http_status", "attempt", "ok", "local_video",
          "video_file", "drive_url", "upload_http_host", "upload_http_method", "upload_http_phase",
          "upload_http_status", "upload_http_error", "upload_http_error_phase", "upload_file",
          "upload_bytes", "upload_total_bytes", "upload_percent"}
FIELDS.update(("upload_http_wait_seconds", "clock_source", "clock_method", "rtc_year"))
FIELDS.update(("rtc_available", "init_available", "datetime_available", "clock_delta_seconds", "clock_detail"))
FIELDS.update(("verification_year", "upload_verification_year", "upload_tls_verification"))
FIELDS.update(("upload_socket_timeout_seconds", "upload_request_attempt"))


def event_from_line(line):
    if not line.startswith(b"K230_DRIVE "):
        return None
    try:
        data = json.loads(line[len(b"K230_DRIVE "):])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stage"), str):
        return None
    return {key: value for key, value in data.items() if key in FIELDS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--video", help="Optional path to an existing selftest MP4 on the K230")
    parser.add_argument("--install-on-success", action="store_true", help="Install the tested fix only after Drive confirms MP4 + metadata")
    args = parser.parse_args()
    preferred = args.video
    if preferred is None:
        try:
            report = json.loads((ROOT / "recorder-test-result.json").read_text())
            preferred = report["result"].get("local_video")
        except (OSError, ValueError, KeyError, TypeError):
            pass
    print("K230 must be idle; quit CanMV IDE. This pauses normal startup.", flush=True)
    print("Retrying an existing clip using HTTP/1.1 and laptop-verified Google certificate pins.", flush=True)
    print("Uses software UTC; the firmware RTC can remain at 1970. No new recording.", flush=True)
    if args.install_on_success:
        print("After Drive confirms success, install this tested fix with an SD backup.", flush=True)
    print("Progress reports contain no token or temporary upload URL.", flush=True)
    pin_spec = importlib.util.spec_from_file_location("google_pins_prepare", ROOT / "prepare-google-pins.py")
    pin_module = importlib.util.module_from_spec(pin_spec)
    pin_spec.loader.exec_module(pin_module)
    def pin_progress(host, profile, ok, error_type=None):
        print("Certificate %s %s: %s" % (host, profile, "CA_VERIFIED" if ok else "profile_unavailable_" + str(error_type)), flush=True)
    print("Verifying current Google certificates on the laptop (no device token sent)...", flush=True)
    pins = pin_module.collect_pins(pin_progress)
    spec = importlib.util.spec_from_file_location("recorder_usb", ROOT / "read-recorder-usb.py")
    usb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usb)
    bundle = ROOT / "recorder-update/recorder"
    ca = runpy.run_path(str(bundle / "google_ca.py"))["CA_PEM"]
    utc = datetime.now(timezone.utc)
    rtc = (utc.year, utc.month, utc.day, utc.weekday(), utc.hour, utc.minute, utc.second, 0)
    source = ("PREFERRED_VIDEO=%r\nHOST_UTC=%r\nTLS_CA_PEM=%r\nGOOGLE_PINS=%r\n"
              "_https={'__name__':'recorder_https_probe'}\nexec(%r, _https)\n"
              "_upload={'__name__':'drive_upload_probe'}\nexec(%r, _upload)\n") % (
        preferred, rtc, ca, pins, (bundle / "recorder_https.py").read_text(), (bundle / "drive_upload.py").read_text())
    source += (ROOT / "retry-drive-device.py").read_text()
    events = []
    def progress(line):
        event = event_from_line(line)
        if event is not None:
            events.append(event)
            print(json.dumps(event, ensure_ascii=False), flush=True)
    camera = usb.CameraRepl(args.port)
    result = {"ok": False, "error_code": "result_missing"}
    try:
        camera.enter()
        camera.execute(source, timeout=360, on_output=progress)
        results = [event for event in events if event.get("stage") == "result"]
        if results:
            result = results[-1]
        if result.get("ok") and args.install_on_success:
            print("Drive confirmed upload. Installing the verified fix...", flush=True)
            install_spec = importlib.util.spec_from_file_location("recorder_install", ROOT / "install-recorder-usb.py")
            installer = importlib.util.module_from_spec(install_spec)
            install_spec.loader.exec_module(installer)
            try:
                installer.install(camera, installer.payload())
                result["installed_fix"] = True
            except (OSError, ValueError, RuntimeError) as error:
                result["installed_fix"] = False
                print("Drive upload passed but installation stopped: " + str(error), flush=True)
    except (KeyboardInterrupt, TimeoutError):
        camera.command(0x06)
        print("Retry stopped; restart K230 before normal recording.", flush=True)
        return 1
    finally:
        camera.close()
    target = ROOT / "drive-retry-result.json"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"result": result, "events": events}, stream, indent=2)
        stream.write("\n")
    print("PASS: MP4 + metadata confirmed on Drive." if result.get("ok") else "NOT CONFIRMED: send the diagnostic output above.", flush=True)
    print("Report: " + str(target), flush=True)
    print("Restart K230 to resume normal KEY/CoreIoT recording.", flush=True)
    return 0 if result.get("ok") and (not args.install_on_success or result.get("installed_fix")) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print("Cannot retry: " + str(error), file=sys.stderr)
        print("Restart K230 to resume its recorder.", file=sys.stderr)
        sys.exit(1)
