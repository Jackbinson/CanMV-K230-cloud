"""Validate timing diagnostics and automatic authentication without networking."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("drive_network", Path(__file__).with_name("diagnose-drive-network.py"))
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)
URL = "https://script.google.com/macros/s/TEST/exec"
SECRET = "TEST_ONLY_PRIVATE_VALUE_DO_NOT_LOG_123456"


def result_bytes(body, status=200, effective="https://script.googleusercontent.com/echo?private=" + SECRET):
    return body + ("\n%d\n0.010\n0.100\n0.200\n0.300\n0.400\n1\n%s" % (status, effective)).encode()


class NetworkTests(unittest.TestCase):
    def test_successful_nonce_parses_timings_without_private_url(self):
        data = result_bytes(json.dumps({"ok": True, "request_id": "a" * 32}).encode())
        response = subprocess.CompletedProcess([], 0, data, b'')
        with patch.object(doctor.subprocess, "run", return_value=response):
            result = doctor.probe("Web App curl-auto", URL, "curl-auto", True, True)
        self.assertTrue(result["nonce_valid"])
        self.assertEqual(result["host"], "script.googleusercontent.com")
        self.assertEqual(result["tls_s"], 0.2)
        self.assertEqual(result["redirects"], 1)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["_nonce"], "a" * 32)

    def test_failure_retains_timings_and_hides_stderr(self):
        data = result_bytes(b'', status=0, effective=URL)
        response = subprocess.CompletedProcess([], 28, data, SECRET.encode())
        with patch.object(doctor.subprocess, "run", return_value=response):
            result = doctor.probe("Web App curl", URL, "curl", True, True)
        self.assertFalse(result["nonce_valid"])
        self.assertEqual(doctor.outcome(result), "TIMEOUT")
        self.assertEqual(result["tcp_s"], 0.1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_login_page_does_not_count_as_working_nonce(self):
        response = subprocess.CompletedProcess([], 0, result_bytes(b'<html>' + SECRET.encode(), effective="https://accounts.google.com/login"), b'')
        with patch.object(doctor.subprocess, "run", return_value=response):
            result = doctor.probe("Web App curl-auto", URL, "curl-auto", True, True)
        self.assertFalse(result["nonce_valid"])
        self.assertIn("dang nhap", doctor.explain([result]))
        self.assertNotIn(SECRET, json.dumps(result))

    def test_multiline_response_body_does_not_shift_timing_fields(self):
        data = result_bytes(b'{\n"ok":true,\n"request_id":"' + b'b' * 32 + b'"\n}')
        with patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, data, b'')):
            result = doctor.probe("Web App curl-auto", URL, "curl-auto", True, True)
        self.assertTrue(result["nonce_valid"])

    def test_probe_never_sends_authentication_data(self):
        data = result_bytes(b'{}')
        with patch.object(doctor.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, data, b'')) as run:
            doctor.probe("Web App curl-http1", URL, "curl-http1", True, True)
        args = run.call_args.args[0]
        self.assertIn("--http1.1", args)
        self.assertNotIn("--ipv4", args)
        self.assertNotIn("--data-binary", args)
        self.assertNotIn("input", run.call_args.kwargs)
        self.assertNotIn("--insecure", args)

    def test_all_failed_never_invokes_authentication_and_report_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            (root / "drive-webapp.json").write_text(json.dumps({"web_app_url": URL, "device_token": SECRET}))
            failure = {"label": "Google", "mode": None, "curl_exit": 28, "http": 0, "host": "unknown", "nonce_valid": False}
            output = io.StringIO()
            with patch.object(doctor, "ROOT", root), patch.object(doctor, "REPORT", report), patch.object(doctor, "probe", return_value=failure), patch.object(doctor.importlib.util, "spec_from_file_location") as load, contextlib.redirect_stdout(output):
                status = doctor.main()
            self.assertEqual(status, 1)
            load.assert_not_called()
            saved = report.read_text()
            self.assertFalse(json.loads(saved)["auth_checked"])
            self.assertNotIn(SECRET, saved + output.getvalue())
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_only_verified_nonce_route_is_used_for_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / "report.json"
            (root / "drive-webapp.json").write_text(json.dumps({"web_app_url": URL, "device_token": SECRET}))
            def probe(label, url, mode, follow, expect_nonce):
                return {"label": label, "mode": mode, "curl_exit": 0, "http": 200, "host": "script.googleusercontent.com", "nonce_valid": mode == "curl-http1", "_nonce": "c" * 32, "total_s": 0.1}
            received = []
            def check(request_id=None):
                received.append(request_id)
                return 0
            fake_check = type("Checker", (), {"main": staticmethod(check)})()
            output = io.StringIO()
            with patch.object(doctor, "ROOT", root), patch.object(doctor, "REPORT", report), patch.object(doctor, "probe", side_effect=probe), patch.object(doctor.importlib.util, "spec_from_file_location") as spec_mock, patch.object(doctor.importlib.util, "module_from_spec", return_value=fake_check), contextlib.redirect_stdout(output):
                status = doctor.main()
            self.assertEqual(status, 0)
            self.assertEqual(fake_check.TRANSPORT, "curl-http1")
            self.assertEqual(received, ["c" * 32])
            spec_mock.return_value.loader.exec_module.assert_called_once_with(fake_check)
            saved = report.read_text()
            self.assertTrue(json.loads(saved)["auth_ok"])
            self.assertNotIn(SECRET, saved + output.getvalue())
            self.assertNotIn("c" * 32, saved + output.getvalue())


if __name__ == "__main__":
    unittest.main()
