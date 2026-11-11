"""Exercise diagnostic branches without networking or real credentials."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("check_drive", Path(__file__).with_name("check-drive.py"))
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
TOKEN = "TEST_ONLY_secret_never_print_this_1234567890"
URL = "https://script.google.com/macros/s/TEST_DEPLOYMENT/exec"
NONCE = "a" * 32


class Response(io.BytesIO):
    def __init__(self, body, url=URL):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "drive-webapp.json"
        self.config.write_text(json.dumps({"web_app_url": URL, "device_token": TOKEN}))
        self.patch_config = patch.object(checker, "CONFIG", self.config)
        self.patch_config.start()
        self.addCleanup(self.patch_config.stop)

    def run_check(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = checker.main()
        text = output.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(URL, text)
        return status, text

    def test_dns_failure_identifies_failed_step(self):
        with patch.object(checker.urllib.request, "urlopen", side_effect=urllib.error.URLError(socket.gaierror(-3, TOKEN))):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("[2/3] GET", text)
        self.assertIn("DNS_ERROR", text)
        self.assertNotIn("[3/3]", text)

    def test_http_permission_error_hides_response(self):
        error = urllib.error.HTTPError(URL, 403, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        with patch.object(checker.urllib.request, "urlopen", side_effect=error):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("HTTP_403", text)

    def test_login_html_is_identified_without_body(self):
        with patch.object(checker.urllib.request, "urlopen", return_value=Response(("<html>ServiceLogin " + TOKEN).encode())):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("LOGIN_REQUIRED", text)

    def test_missing_do_get_is_identified(self):
        with patch.object(checker.urllib.request, "urlopen", return_value=Response(b"<html>Script function not found: doGet</html>")):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("SCRIPT_FUNCTION_MISSING", text)

    def test_unexpected_html_is_identified(self):
        with patch.object(checker.urllib.request, "urlopen", return_value=Response(("<html>" + TOKEN).encode())):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("NON_JSON_RESPONSE", text)

    def test_certificate_failure_is_identified(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError(1, TOKEN))
        with patch.object(checker.urllib.request, "urlopen", side_effect=error):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("TLS_CERT_ERROR", text)

    def test_nonce_missing_is_protocol_error(self):
        with patch.object(checker, "request_json", return_value={"ok": True}):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("NONCE_MISSING", text)

    def test_nonce_wrong_type_is_protocol_error(self):
        with patch.object(checker, "request_json", return_value={"ok": True, "request_id": 123}):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("NONCE_MISSING", text)

    def test_success_only_sends_ping(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, {"ok": True, "queued": True},
            {"ok": True, "service": "k230-drive-upload"},
        ]) as request:
            status, text = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("DEVICE_TOKEN matches", text)
        body = request.call_args_list[1].args[1]
        self.assertEqual(body, {"action": "ping", "token": TOKEN, "request_id": NONCE})

    def test_mismatch_is_reported_without_token(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, {"ok": True, "queued": True},
            {"ok": False, "error": "unauthorized", "secret": TOKEN},
        ]):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("DEVICE_TOKEN differs", text)

    def test_lost_post_response_can_still_succeed(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, checker.DiagnosticError("TIMEOUT: test"),
            {"ok": True, "service": "k230-drive-upload"},
        ]):
            status, text = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("DEVICE_TOKEN matches", text)

    def test_direct_post_success_does_not_poll(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE},
            {"ok": True, "service": "k230-drive-upload"},
        ]) as request:
            status, text = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("GET result lan", text)

    def test_verified_nonce_is_used_without_fetching_another(self):
        output = io.StringIO()
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "queued": True}, {"ok": True, "service": "k230-drive-upload"},
        ]) as request, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            status = checker.main(request_id=NONCE)
        self.assertEqual(status, 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1]["request_id"], NONCE)
        self.assertNotIn("GET nonce lan", output.getvalue())
        self.assertNotIn(NONCE, output.getvalue())
        self.assertNotIn(TOKEN, output.getvalue())

    def test_initial_nonce_timeout_is_retried(self):
        with patch.object(checker, "request_json", side_effect=[
            checker.DiagnosticError("TIMEOUT: test"), {"ok": True, "request_id": NONCE},
            {"ok": True, "queued": True}, {"ok": True, "service": "k230-drive-upload"},
        ]) as request, patch.object(checker.time, "sleep"):
            status, text = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(request.call_count, 4)
        self.assertIn("GET nonce lan 2/3", text)

    def test_direct_post_rejection_does_not_poll(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE},
            {"ok": False, "error": "unauthorized"},
        ]) as request:
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(request.call_count, 2)
        self.assertIn("DEVICE_TOKEN differs", text)

    def test_result_timeout_retries_without_resending_post(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, {"ok": True, "queued": True},
            checker.DiagnosticError("TIMEOUT: test"),
            {"ok": True, "service": "k230-drive-upload"},
        ]) as request, patch.object(checker.time, "sleep"):
            status, text = self.run_check()
        self.assertEqual(status, 0)
        self.assertIn("POST OK", text)
        self.assertIn("GET result lan 2/3", text)
        self.assertEqual(sum(len(call.args) == 2 for call in request.call_args_list), 1)

    def test_result_timeouts_are_bounded(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, {"ok": True, "queued": True},
            checker.DiagnosticError("TIMEOUT: test"),
            checker.DiagnosticError("TIMEOUT: test"),
            checker.DiagnosticError("TIMEOUT: test"),
        ]) as request, patch.object(checker.time, "sleep"):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(request.call_count, 5)
        self.assertIn("RESULT_UNAVAILABLE", text)

    def test_permission_failure_is_not_retried(self):
        with patch.object(checker, "request_json", side_effect=[
            {"ok": True, "request_id": NONCE}, {"ok": True, "queued": True},
            checker.DiagnosticError("HTTP_403: denied"),
        ]) as request:
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(request.call_count, 3)
        self.assertIn("HTTP_403", text)

    def test_curl_post_uses_stdin_and_allows_get_redirect(self):
        response = subprocess.CompletedProcess([], 0, b'{"ok":true}\n200\nhttps://script.googleusercontent.com/macros/echo?private=TEST', b'')
        with patch.object(checker, "TRANSPORT", "curl"), patch.object(checker.subprocess, "run", return_value=response) as run:
            result = checker.request_json(URL, {"action": "ping", "token": TOKEN})
        self.assertTrue(result["ok"])
        args = run.call_args.args[0]
        self.assertNotIn(TOKEN, " ".join(args))
        self.assertEqual(args[:2], ["curl", "--disable"])
        self.assertIn("--ipv4", args)
        self.assertIn("--location", args)
        self.assertNotIn("--insecure", args)
        self.assertNotIn("--request", args)
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["token"], TOKEN)

    def test_curl_failure_does_not_print_stderr(self):
        response = subprocess.CompletedProcess([], 28, b'', TOKEN.encode())
        with patch.object(checker, "TRANSPORT", "curl"), patch.object(checker.subprocess, "run", return_value=response):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("TIMEOUT", text)

    def test_curl_permission_error_ignores_body(self):
        response = subprocess.CompletedProcess([], 0, TOKEN.encode() + b'\n403\n' + URL.encode(), b'')
        with patch.object(checker, "TRANSPORT", "curl"), patch.object(checker.subprocess, "run", return_value=response):
            status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("HTTP_403", text)

    def test_bad_config_does_not_echo_values(self):
        self.config.write_text(TOKEN)
        status, text = self.run_check()
        self.assertEqual(status, 1)
        self.assertIn("CONFIG_ERROR", text)


if __name__ == "__main__":
    unittest.main()
