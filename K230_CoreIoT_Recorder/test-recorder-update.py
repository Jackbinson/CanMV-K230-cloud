"""Host simulations: RPC lifecycle, MP4 shutdown ordering, resumable Drive uploads."""
import binascii
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "recorder-update/recorder"
sys.path.insert(0, str(BUNDLE))
sys.modules.setdefault("ujson", json)
sys.modules.setdefault("ubinascii", binascii)
import remote_control as remote
import drive_upload as drive


class ControlTests(unittest.TestCase):
    def test_stop_finalizes_session_before_idle(self):
        c = remote.RecorderControl()
        self.assertTrue(c.handle({"method": "setRecording", "params": True})["accepted"])
        self.assertFalse(c.snapshot()["recording"])
        self.assertTrue(c.take_start())
        c.begin_session()
        c.update(recording=True, recorder_state="recording")
        c.handle({"method": "stopRecording"})
        self.assertTrue(c.stopped())
        self.assertTrue(c.snapshot()["session_active"])
        c.update(recording=False, recorder_state="finalizing")
        self.assertTrue(c.snapshot()["session_active"])
        c.finish_session()
        self.assertFalse(c.snapshot()["session_active"])
        self.assertFalse(c.stopped())

    def test_queued_start_can_be_cancelled(self):
        c = remote.RecorderControl()
        c.handle({"method": "startRecording"})
        c.handle({"method": "stopRecording"})
        self.assertFalse(c.take_start())
        self.assertFalse(c.snapshot()["session_active"])

    def test_boolean_validation_and_unknown_methods(self):
        c = remote.RecorderControl()
        for value in (1, 0, "true", {}, None):
            self.assertFalse(c.handle({"method": "setRecording", "params": value})["ok"])
        self.assertEqual(c.handle({"method": "setAlarm", "params": True})["error"], "unsupported_method")
        self.assertFalse(c.take_start())

    def test_upload_io_cannot_start_during_session(self):
        c = remote.RecorderControl()
        self.assertTrue(c.begin_upload_io())
        c.handle({"method": "startRecording"})
        self.assertTrue(c.upload_busy())
        c.end_upload_io()
        self.assertFalse(c.begin_upload_io())
        c.finish_session()
        self.assertTrue(c.begin_upload_io())

    def test_rpc_retry_does_not_start_a_second_completed_session(self):
        c = remote.RecorderControl()
        cache = []
        payload = b'{"method":"startRecording"}'
        first = remote.rpc_response(c, cache, "1", payload)
        c.take_start()
        c.begin_session()
        c.finish_session()
        self.assertEqual(remote.rpc_response(c, cache, "1", payload), first)
        self.assertFalse(c.take_start())

    def test_reused_rpc_id_with_a_different_command_is_handled(self):
        c = remote.RecorderControl()
        cache = []
        remote.rpc_response(c, cache, "1", b'{"method":"startRecording"}')
        remote.rpc_response(c, cache, "1", b'{"method":"stopRecording"}')
        self.assertFalse(c.take_start())


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = str(Path(self.directory.name) / "CAM01_00001")
        self.path = self.base + ".mp4"
        Path(self.path).write_bytes(b"x" * 300000)
        self.control = remote.RecorderControl()
        self.journal = {"seq": 0, "files": {}}
        self.calls = []
        self.inits = []
        self.sync = patch.object(drive.os, "sync", lambda: None)
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def api(self, responses):
        iterator = iter(responses)
        def http(method, url, body, headers):
            self.calls.append((method, len(body or b""), dict(headers)))
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return value
        def operation(action, **values):
            self.inits.append((action, values))
            return {"ok": True, "upload_url": "https://www.googleapis.com/upload/drive/v3/files?upload_id=PRIVATE_TEST"}
        return types.SimpleNamespace(http=http, operation=operation)

    def upload(self, api):
        return drive.upload_file(self.path, "CAM01", api, self.journal, self.base, self.control)

    def test_upload_chunks_and_final_confirmation(self):
        api = self.api([(308, {}, b""), (308, {"Range": "bytes=0-262143"}, b""),
                        (200, {}, b'{"id":"DRIVE_FILE_123"}')])
        self.assertEqual(self.upload(api), "DRIVE_FILE_123")
        self.assertEqual([call[1] for call in self.calls], [0, 262144, 37856])
        self.assertEqual(self.calls[2][2]["Content-Range"], "bytes 262144-299999/300000")
        self.assertTrue(drive.read_journal(self.base)["files"]["CAM01_00001.mp4"]["done"])
        self.assertEqual(Path(self.path).stat().st_size, 300000)

    def test_lost_final_response_is_probed_without_duplicate_init(self):
        api = self.api([(308, {}, b""), (308, {"Range": "bytes=0-262143"}, b""),
                        drive.UploadError("http_connection_failed")])
        with self.assertRaises(drive.UploadError):
            self.upload(api)
        self.assertFalse(self.journal["files"]["CAM01_00001.mp4"]["done"])
        self.journal = drive.read_journal(self.base)
        self.assertEqual(self.upload(self.api([(200, {}, b'{"id":"DRIVE_FILE_123"}') ])), "DRIVE_FILE_123")
        self.assertEqual(len(self.inits), 1)
        self.assertEqual(self.calls[-1][1], 0)

    def test_expired_session_is_persisted_for_retry(self):
        with self.assertRaisesRegex(drive.UploadError, "session_expired"):
            self.upload(self.api([(404, {}, b"" )]))
        job = drive.read_journal(self.base)["files"]["CAM01_00001.mp4"]
        self.assertNotIn("upload_url", job)
        self.assertFalse(job["done"])

    def test_out_of_bounds_range_never_marks_uploaded(self):
        with self.assertRaisesRegex(drive.UploadError, "out_of_bounds"):
            self.upload(self.api([(308, {}, b""), (308, {"Range": "bytes=0-299999"}, b"")]))
        self.assertFalse(self.journal["files"]["CAM01_00001.mp4"]["done"])

    def test_premature_completion_is_rejected(self):
        job = {"size": 300000, "offset": 0}
        with self.assertRaisesRegex(drive.UploadError, "premature"):
            drive.apply_progress(200, {}, b'{"id":"DRIVE_FILE_123"}', job, 262144)
        self.assertNotIn("done", job)

    def test_recording_pauses_before_initialization(self):
        self.control.begin_session()
        with self.assertRaises(drive.UploadPaused):
            self.upload(self.api([]))
        self.assertFalse(self.inits)

    def test_partial_or_failed_recordings_are_excluded(self):
        cfg = {"output_dir": self.directory.name, "camera_id": "CAM01"}
        metadata = {"status": "needs_review", "video_file": "CAM01_00001.mp4", "camera_id": "CAM01"}
        Path(self.base + ".json").write_text(json.dumps(metadata))
        self.assertEqual(list(drive.pending_clips(cfg)), [])
        metadata["status"] = "finalized"
        Path(self.base + ".json").write_text(json.dumps(metadata))
        self.assertEqual(len(list(drive.pending_clips(cfg))), 1)
        Path(self.base + ".partial.mp4").write_bytes(b"partial")
        self.assertEqual(len(list(drive.pending_clips(cfg))), 1)

    def test_corrupted_newest_journal_keeps_previous_confirmed_slot(self):
        drive.write_journal(self.base, self.journal)
        self.journal["files"]["test"] = {"offset": 123}
        drive.write_journal(self.base, self.journal)
        Path(self.base + ".drive.0.json").write_text('{"seq":')
        previous = drive.read_journal(self.base)
        self.assertEqual(previous["seq"], 1)
        self.assertNotIn("test", previous["files"])

    def test_upload_urls_must_be_google_https(self):
        for url in ("http://www.googleapis.com/upload/drive/x", "https://evil.test/upload/drive/x",
                    "https://www.googleapis.com@evil.test/upload/drive/x"):
            with self.assertRaises(drive.UploadError):
                drive.validate_upload_url(url)

    def test_script_nonce_post_result_protocol(self):
        calls = []
        responses = iter([(200, {}, json.dumps({"ok": True, "request_id": "a" * 32}).encode()),
                          (200, {}, b'{"ok":true,"queued":true}'),
                          (200, {}, b'{"ok":true,"upload_url":"https://www.googleapis.com/upload/drive/v3/files?upload_id=TEST"}')])
        def http(method, url, body, headers):
            calls.append((method, url, body))
            return next(responses)
        api = drive.DriveApi({"web_app_url": "https://script.google.com/macros/s/TEST/exec", "device_token": "t" * 43}, http)
        result = api.operation("init", name="CAM01_00001.mp4", camera_id="CAM01", size=300000)
        self.assertTrue(result["ok"])
        self.assertEqual([item[0] for item in calls], ["GET", "POST", "GET"])
        self.assertEqual(json.loads(calls[1][2])["token"], "t" * 43)
        self.assertTrue(all("_cb=" in item[1] for item in calls))

    def test_successful_probe_nonce_is_reused_for_init(self):
        calls = []
        responses = iter([(200, {}, b'{"ok":true,"queued":true}'),
                          (200, {}, b'{"ok":true,"upload_url":"https://www.googleapis.com/upload/drive/v3/files?upload_id=TEST"}')])
        def http(method, url, body, headers):
            calls.append((method, url, body))
            return next(responses)
        api = drive.DriveApi({"web_app_url": "https://script.google.com/macros/s/TEST/exec", "device_token": "t" * 43}, http)
        api.prefetched_nonce = {"ok": True, "request_id": "a" * 32}
        result = api.operation("init", name="CAM01_00001.mp4", camera_id="CAM01", size=300000)
        self.assertTrue(result["ok"])
        self.assertEqual([item[0] for item in calls], ["POST", "GET"])
        self.assertIsNone(api.prefetched_nonce)

    def test_http_errors_keep_errno_but_never_arbitrary_messages(self):
        secret = "TEST_ONLY_PRIVATE_TOKEN"
        cases = [(OSError(110, secret), "http_oserror_110"),
                 (OSError(-0x7100, secret), "http_oserror_-28928"),
                 (ValueError("Unsupported Transfer-Encoding: chunked " + secret), "http_chunked_response_unsupported"),
                 (ValueError(secret), "http_sdk_value_error"),
                 (TypeError("unexpected keyword argument timeout " + secret), "sdk_http_timeout_unsupported"),
                 (TypeError(secret), "http_sdk_type_error")]
        for error, expected in cases:
            self.assertEqual(drive.safe_http_error(error), expected)
            self.assertNotIn(secret, drive.safe_http_error(error))

    def test_sdk_oserror_reports_errno_and_releases_upload_lease(self):
        def fail(*args, **kwargs):
            raise OSError(-28928, "TEST_ONLY_PRIVATE_URL")
        observed = []
        client = types.SimpleNamespace(request=fail, observe=None)
        http = drive.FirmwareHttp(self.control, lambda error, phase: observed.append(phase), client=client)
        with self.assertRaisesRegex(drive.UploadError, "http_oserror_-28928"):
            http("GET", "https://script.google.com/macros/s/TEST/exec")
        self.assertFalse(self.control.upload_busy())
        self.assertEqual(observed, ["request"])
        self.assertEqual(self.control.snapshot()["upload_http_error_phase"], "request")

    def test_sdk_body_error_is_distinguished_from_request_failure(self):
        client = types.SimpleNamespace(observe=None)
        def fail(*args, **kwargs):
            client.observe("body", "script.google.com")
            raise ValueError("Unsupported Transfer-Encoding: chunked")
        client.request = fail
        http = drive.FirmwareHttp(self.control, client=client)
        with self.assertRaisesRegex(drive.UploadError, "http_chunked_response_unsupported"):
            http("GET", "https://script.google.com/macros/s/TEST/exec")
        self.assertFalse(self.control.upload_busy())
        self.assertEqual(self.control.snapshot()["upload_http_error_phase"], "body")


class CaptureTests(unittest.TestCase):
    def test_remote_stop_closes_mp4_before_validation_and_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            events = []
            control = remote.RecorderControl()
            control.begin_session()
            class Engine:
                MP4_CONFIG_TYPE_MUXER = 0
                MP4_CODEC_ID_H264 = 1
                MP4_CODEC_ID_G711U = 2
                frames_written = 0
                header_packets = 1
                first_video_pts = 0
                last_video_pts = 0
                max_pts_gap_us = 0
                def Create(self, cfg):
                    self.path = cfg.path
                    Path(self.path).write_bytes(b"fake encoder data")
                def Start(self):
                    events.append("start")
                def Process(self):
                    self.frames_written += 1
                    control.handle({"method": "stopRecording"})
                def close_file(self):
                    events.append("close")
            class Config:
                def __init__(self, kind):
                    pass
                def SetMuxerCfg(self, path, *args):
                    self.path = path
            def metrics(path, expected_frames):
                self.assertEqual(events[-1], "close")
                self.assertEqual(expected_frames, 1)
                events.append("metrics")
                return {"video_frames": 1, "fps_media_timeline": 1}
            stubs = {
                "media": types.ModuleType("media"),
                "media.mp4format": types.SimpleNamespace(Mp4CfgStr=Config),
                "clean_mp4": types.SimpleNamespace(CleanMp4Container=Engine),
                "ybUtils": types.ModuleType("ybUtils"),
                "ybUtils.YbKey": types.SimpleNamespace(YbKey=object),
                "ybUtils.YbRGB": types.SimpleNamespace(YbRGB=object),
                "mp4_metrics": types.SimpleNamespace(read_video_metrics=metrics),
                "wifi_client": types.SimpleNamespace(ensure_wifi_connected=lambda **k: False),
            }
            spec = importlib.util.spec_from_file_location("record_video_test", BUNDLE / "record_video.py")
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, stubs):
                spec.loader.exec_module(module)
            module._control = control
            module.wait_release = lambda key: None
            clock = iter([0, 100, 200])
            fake_time = types.SimpleNamespace(ticks_ms=lambda: next(clock), ticks_diff=lambda a, b: a-b, localtime=lambda: (2026, 11, 11, 0, 0, 0, 0, 0))
            fake_os = types.SimpleNamespace(exitpoint=lambda *a: None, EXITPOINT_ENABLE=1, EXITPOINT_ENABLE_SLEEP=2, rename=os.rename, listdir=os.listdir)
            cfg = {"output_dir": directory, "camera_id": "CAM01", "width": 640, "height": 480}
            with patch.object(module, "time", fake_time), patch.object(module, "os", fake_os), context_stdout():
                reason, elapsed = module.capture_clip(cfg, types.SimpleNamespace(is_pressed=lambda: False), types.SimpleNamespace(show_rgb=lambda rgb: None), 30)
            self.assertEqual(reason, "remote_stop")
            self.assertEqual(events, ["start", "close", "metrics"])
            saved = json.loads((Path(directory) / "CAM01_00001.json").read_text())
            self.assertEqual(saved["status"], "finalized")
            self.assertTrue((Path(directory) / "CAM01_00001.mp4").exists())
            self.assertFalse((Path(directory) / "CAM01_00001.partial.mp4").exists())


def context_stdout():
    import contextlib
    return contextlib.redirect_stdout(io.StringIO())


if __name__ == "__main__":
    unittest.main()
