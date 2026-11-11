"""Exercise the real installer scripts against a simulated camera filesystem."""
import binascii
import builtins
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("installer", ROOT / "install-recorder-usb.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Camera:
    def __init__(self, mount):
        self.mount = str(mount)
        self.fail_once = False
        self.failure_occurred = False

    def execute(self, source):
        device_os = types.ModuleType("os")
        device_os.__dict__.update(os.__dict__)
        device_os.sync = lambda: None
        def request(method, url, **kwargs):
            raise ValueError("Unsupported protocol")
        modules = {"ujson": json, "uhashlib": hashlib, "ubinascii": binascii,
                   "requests": types.SimpleNamespace(request=request), "os": device_os}
        real_open = builtins.open
        camera = self
        class PartialWriter:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def write(self, data):
                self.stream.write(data[:16])
                camera.failure_occurred = True
                raise OSError("simulated power/storage failure")
        def camera_open(path, mode="r", *args, **kwargs):
            stream = real_open(path, mode, *args, **kwargs)
            if (self.fail_once and not self.failure_occurred and mode == "wb" and
                    str(path) == self.mount + "/recorder/record_video.py"):
                return PartialWriter(stream)
            return stream
        namespace = {"__builtins__": dict(vars(builtins), open=camera_open)}
        output = io.StringIO()
        with patch.dict(sys.modules, modules), contextlib.redirect_stdout(output):
            exec(source.replace("/sdcard", self.mount), namespace)
        return output.getvalue().strip().encode()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mount = Path(self.temp.name) / "sdcard"
        self.mount.mkdir()
        shutil.copytree(installer.BASELINE / "recorder", self.mount / "recorder")
        shutil.copyfile(installer.BASELINE / "main.py", self.mount / "main.py")
        (self.mount / "recorder/recording_config.json").write_text(json.dumps({"enabled": True, "output_dir": "/data/recordings"}))
        (self.mount / "mqtt_config.json").write_text('{"original":"COREIOT_TEST_ONLY"}\n')
        self.original = {str(p.relative_to(self.mount)): p.read_bytes() for p in self.mount.rglob("*") if p.is_file()}
        self.camera = Camera(self.mount)
        self.files = {"/sdcard/recorder/" + name: (installer.BUNDLE / name).read_bytes() for name in installer.MODULES}
        self.files["/sdcard/mqtt_config.json"] = b'{"new":"COREIOT_TEST_ONLY"}\n'
        self.files["/sdcard/recorder/drive-webapp.json"] = b'{"test":"DRIVE_TEST_ONLY"}\n'
        self.files["/sdcard/recorder/google-pins.json"] = b'{"version":1,"hosts":{}}\n'

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.camera, self.files)

    def assert_original_preserved(self):
        for name, data in self.original.items():
            self.assertEqual((self.mount / name).read_bytes(), data, name)

    def test_success_preserves_protected_files_and_can_roll_back(self):
        self.install()
        for path in installer.baseline_hashes():
            self.assertEqual((self.mount / path).read_bytes(), self.original[path])
        self.assertEqual((self.mount / "recorder/recording_config.json").read_bytes(), self.original["recorder/recording_config.json"])
        self.assertFalse((self.mount / "recorder/maintenance.flag").exists())
        self.assertEqual(json.loads((self.mount / "recorder/remote-install.json").read_text())["status"], "installed")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.rollback(self.camera)
        self.assert_original_preserved()
        for name in ("drive_upload.py", "mqtt_transport.py", "remote_control.py", "drive-webapp.json", "google_ca.py", "recorder_https.py", "google-pins.json"):
            self.assertFalse((self.mount / "recorder" / name).exists())

    def test_partial_destination_write_restores_originals(self):
        self.camera.fail_once = True
        with self.assertRaisesRegex(RuntimeError, "rollback completed"):
            self.install()
        self.assertTrue(self.camera.failure_occurred)
        self.assert_original_preserved()
        self.assertFalse((self.mount / "recorder/maintenance.flag").exists())

    def test_unknown_main_source_is_rejected_before_staging(self):
        path = self.mount / "main.py"
        path.write_text("# user change\n")
        before = sorted(str(p) for p in self.mount.rglob("*"))
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.install()
        self.assertEqual(sorted(str(p) for p in self.mount.rglob("*")), before)
        self.assertEqual(path.read_text(), "# user change\n")

    def test_existing_user_edits_are_not_overwritten_by_rollback(self):
        self.install()
        path = self.mount / "recorder/record_video.py"
        path.write_text("# changed after installation\n")
        with self.assertRaisesRegex(ValueError, "changed since install"):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.rollback(self.camera)
        self.assertEqual(path.read_text(), "# changed after installation\n")
        self.assertFalse((self.mount / "recorder/maintenance.flag").exists())

    def test_interrupted_rollback_can_be_retried(self):
        self.install()
        self.camera.fail_once = True
        with self.assertRaisesRegex(OSError, "simulated"):
            with contextlib.redirect_stdout(io.StringIO()):
                installer.rollback(self.camera)
        self.assertTrue((self.mount / "recorder/maintenance.flag").exists())
        self.assertEqual(json.loads((self.mount / "recorder/remote-install.json").read_text())["status"], "rolling_back")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.rollback(self.camera)
        self.assert_original_preserved()
        self.assertFalse((self.mount / "recorder/maintenance.flag").exists())


if __name__ == "__main__":
    unittest.main()
