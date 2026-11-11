"""Verify streaming USB test output and filtering without a camera."""
import importlib.util
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usb = load("test_usb_protocol", "read-recorder-usb.py")
runner = load("test_usb_runner", "test-recorder-usb.py")


class StreamingTests(unittest.TestCase):
    def camera(self, make_output):
        camera = usb.CameraRepl.__new__(usb.CameraRepl)
        camera.command = lambda *args: None
        def write(payload):
            marker = re.search(rb"K230_SOURCE_[a-f0-9]+", payload).group()
            camera.chunks = iter(make_output(marker))
        camera.write = write
        camera.poll_output = lambda: next(camera.chunks, b"")
        return camera

    def test_fragmented_progress_lines_are_delivered_once(self):
        def chunks(marker):
            return [b"ignored startup output\n" + marker[:10], marker[10:] + b"_BEGIN\nK230_TEST {\"stage\":\"rec",
                    b"ording\",\"seconds\":5}\nK230_TEST {\"stage\":\"result\",\"ok\":true}\n" + marker + b"_END\n"]
        camera = self.camera(chunks)
        lines = []
        with patch.object(usb.time, "sleep"):
            camera.execute("pass", timeout=120, on_output=lines.append)
        self.assertEqual([runner.event_from_line(line)["stage"] for line in lines], ["recording", "result"])

    def test_long_running_script_uses_requested_timeout(self):
        camera = self.camera(lambda marker: [marker + b"_BEGIN\n{}\n" + marker + b"_END\n"])
        with patch.object(usb.time, "sleep"), patch.object(usb.time, "monotonic", side_effect=[0, 30]):
            result = camera.execute("pass", timeout=120)
        self.assertEqual(result, b"{}")

    def test_device_failure_does_not_expose_private_output(self):
        secret = b"TEST_ONLY_PRIVATE_TOKEN_VALUE"
        camera = self.camera(lambda marker: [marker + b"_BEGIN\n" + secret + b"\n" + marker + b"_ERROR\n" + marker + b"_END\n"])
        with patch.object(usb.time, "sleep"):
            with self.assertRaises(RuntimeError) as error:
                camera.execute("pass")
        self.assertNotIn(secret.decode(), str(error.exception))

    def test_only_structured_test_progress_is_accepted(self):
        self.assertIsNone(runner.event_from_line(b"raw camera log"))
        self.assertIsNone(runner.event_from_line(b"K230_TEST invalid JSON"))
        self.assertIsNone(runner.event_from_line(b'K230_TEST []'))
        result = runner.event_from_line(b'K230_TEST {"stage":"status","upload_state":"uploading","token":"TEST_ONLY_SECRET","upload_url":"PRIVATE"}')
        self.assertEqual(result, {"stage": "status", "upload_state": "uploading"})

    def test_device_script_compiles_and_does_not_rewrite_configs(self):
        source = (ROOT / "recorder-test-device.py").read_text()
        compile("TEST_SECONDS=5\nTEST_WAIT=180\n" + source, "camera_test", "exec")
        self.assertIn("start_services(control, cfg)", source)
        self.assertNotIn("upload_file(", source)
        self.assertNotIn("recording_config.json", source)


if __name__ == "__main__":
    unittest.main()
