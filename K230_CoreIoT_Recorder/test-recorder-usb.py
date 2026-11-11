#!/usr/bin/env python3
"""Record a short K230 clip and verify automatic background upload to Drive."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "recorder-test-result.json"
FIELDS = {"stage", "message", "seconds", "wait_seconds", "wifi_connected", "local_video",
          "video_file", "video_frames", "wall_record_seconds", "stop_reason", "mqtt_connected",
          "mqtt_error", "upload_state", "upload_error", "upload_file", "upload_percent",
          "upload_bytes", "upload_total_bytes", "ok", "record_ok", "upload_ok", "drive_url",
          "error", "error_type", "shutdown_error"}


def event_from_line(line):
    prefix = b"K230_TEST "
    if not line.startswith(prefix):
        return None
    try:
        data = json.loads(line[len(prefix):])
    except (ValueError, UnicodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("stage"), str):
        return None
    return {key: value for key, value in data.items() if key in FIELDS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--seconds", type=int, choices=range(5, 31), default=5, metavar="5..30")
    parser.add_argument("--wait", type=int, choices=range(30, 301), default=180, metavar="30..300")
    args = parser.parse_args()
    print("Restart K230 first, wait for blue READY, then close CanMV IDE.", flush=True)
    print("This temporarily pauses normal startup, records %ds, and automatically uploads MP4 + JSON to Drive." % args.seconds, flush=True)
    print("Test clips are kept in a new selftest folder; saved recording settings stay unchanged.", flush=True)
    spec = importlib.util.spec_from_file_location("recorder_usb", ROOT / "read-recorder-usb.py")
    usb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(usb)
    events = []
    def progress(line):
        event = event_from_line(line)
        if event is not None:
            events.append(event)
            print(json.dumps(event, ensure_ascii=False), flush=True)
    camera = usb.CameraRepl(args.port)
    try:
        camera.enter()
        source = "TEST_SECONDS=%d\nTEST_WAIT=%d\n" % (args.seconds, args.wait)
        source += (ROOT / "recorder-test-device.py").read_text()
        camera.execute(source, timeout=args.wait + 90, on_output=progress)
    except (KeyboardInterrupt, TimeoutError):
        # This is a user-controlled temporary test, not the normal recorder.
        camera.command(0x06)
        print("Test stopped. Restart K230 before using its normal recorder.", flush=True)
        return 1
    finally:
        camera.close()
    results = [event for event in events if event.get("stage") == "result"]
    result = results[-1] if results else {"ok": False, "error": "test_result_missing"}
    fd = os.open(REPORT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"result": result, "events": events}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if result.get("ok"):
        print("PASS: K230 finalized the MP4; its background uploader confirmed MP4 + JSON on Drive.", flush=True)
        print("Open the drive_url above to check video and audio playback.", flush=True)
    else:
        print("NOT CONFIRMED: send the status/result lines above; local test files are retained.", flush=True)
    print("Report: " + str(REPORT), flush=True)
    print("Restart K230 to return to normal KEY/CoreIoT recording.", flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print("Cannot run test: " + str(error), file=sys.stderr)
        print("Restart K230 to resume its normal recorder.", file=sys.stderr)
        sys.exit(1)
