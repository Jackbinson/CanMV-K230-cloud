"""Executed temporarily over USB; record a short clip and use the installed uploader."""
import os
import sys
import time
import ujson as json
import ubinascii


def _test_event(stage, **values):
    values["stage"] = stage
    print("K230_TEST " + json.dumps(values))


def _run_recorder_test(seconds, wait_seconds):
    sys.path.insert(0, "/sdcard/recorder")
    import record_video as recorder
    from remote_control import RecorderControl, start_services
    from drive_upload import read_journal
    from wifi_client import ensure_wifi_connected
    from ybUtils.YbKey import YbKey
    from ybUtils.YbRGB import YbRGB

    class TestControl(RecorderControl):
        def handle(self, request):
            # The one-clip test owns media; reject unrelated recording commands.
            if isinstance(request, dict) and request.get("method") in (
                    "setRecording", "startRecording", "stopRecording"):
                return {"ok": False, "error": "usb_test_in_progress"}
            return super().handle(request)

    control = None
    record_ok = False
    upload_ok = False
    final = {"ok": False, "record_ok": False, "upload_ok": False}
    try:
        cfg = dict(recorder.load_config())
        run_id = ubinascii.hexlify(os.urandom(6)).decode()
        cfg["camera_id"] = "TEST_" + cfg["camera_id"][:8] + "_" + run_id
        cfg["output_dir"] += "/selftest-" + run_id
        os.mkdir(cfg["output_dir"])
        _test_event("wifi", message="Connecting K230 using saved Wi-Fi settings")
        wifi = ensure_wifi_connected(timeout=20)
        _test_event("wifi_result", wifi_connected=bool(wifi))

        control = TestControl()
        control.update(camera_id=cfg["camera_id"], recorder_test=True)
        recorder._control = control
        control.begin_session()
        start_services(control, cfg)
        key, rgb = YbKey(), YbRGB()
        recorder.wait_release(key)
        _test_event("recording", seconds=seconds)
        reason, elapsed = recorder.capture_clip(cfg, key, rgb, seconds)
        control.finish_session()
        rgb.show_rgb((0, 40, 0))
        name = control.snapshot()["last_video"]
        base = cfg["output_dir"] + "/" + name[:-4]
        with open(base + ".json") as stream:
            metadata = json.load(stream)
        record_ok = metadata.get("status") == "finalized"
        final.update(record_ok=record_ok, local_video=base + ".mp4", video_file=name,
                     video_frames=metadata.get("video_frames"),
                     wall_record_seconds=elapsed, stop_reason=reason)
        _test_event("saved", local_video=base + ".mp4", video_file=name,
                    video_frames=metadata.get("video_frames"), wall_record_seconds=elapsed)
        _test_event("auto_upload", message="Waiting for installed background uploader", wait_seconds=wait_seconds)
        started = time.ticks_ms()
        last_report = None
        while time.ticks_diff(time.ticks_ms(), started) < wait_seconds * 1000:
            os.exitpoint()
            state = control.snapshot()
            summary = {key: state.get(key) for key in (
                "mqtt_connected", "mqtt_error", "upload_state", "upload_error",
                "upload_file", "upload_percent", "upload_bytes", "upload_total_bytes")}
            if summary != last_report:
                _test_event("status", **summary)
                last_report = summary
            if state.get("last_uploaded_video") == name and read_journal(base).get("uploaded"):
                upload_ok = True
                final.update(drive_url=state.get("last_drive_url"),
                             mqtt_connected=state.get("mqtt_connected", False))
                break
            recorder.idle_step()
        if not upload_ok:
            state = control.snapshot()
            final.update(error="upload_not_confirmed_within_wait", upload_state=state.get("upload_state"),
                         upload_error=state.get("upload_error"), mqtt_connected=state.get("mqtt_connected", False))
    except KeyboardInterrupt:
        final["error"] = "test_interrupted"
    except Exception as error:
        # Never print arbitrary exceptions, HTTP bodies, credentials or URLs.
        final.update(error="recorder_test_failed", error_type=type(error).__name__)
    finally:
        if control is not None:
            control.alive = False
        try:
            recorder.shutdown()
        except Exception:
            final["shutdown_error"] = True
        final.update(ok=record_ok and upload_ok, record_ok=record_ok, upload_ok=upload_ok)
        _test_event("result", **final)


_run_recorder_test(TEST_SECONDS, TEST_WAIT)
