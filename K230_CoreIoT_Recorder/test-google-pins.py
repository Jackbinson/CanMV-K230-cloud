"""Check verified desktop bootstrap and atomic pinset replacement without network."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import ssl
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pin_bootstrap", ROOT / "prepare-google-pins.py")
pins = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pins)


class PinBootstrapTests(unittest.TestCase):
    def test_leaf_is_captured_only_with_ca_and_hostname_verification(self):
        context = MagicMock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        stream = context.wrap_socket.return_value.__enter__.return_value
        stream.getpeercert.side_effect = [b"TEST_CERT", {"notBefore": "Jan  1 00:00:00 2026 GMT", "notAfter": "Jan  1 00:00:00 2027 GMT"}]
        with patch.object(pins.ssl, "create_default_context", return_value=context), patch.object(pins.socket, "create_connection"), patch.object(pins.time, "time", return_value=1794407400):
            result = pins.capture("script.google.com", "127.0.0.1", "tls12-rsa", b"PUBLIC_TEST_CA")
        self.assertEqual(result["sha256"], hashlib.sha256(b"TEST_CERT").hexdigest())
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "script.google.com")

    def test_disabled_desktop_hostname_validation_is_rejected(self):
        context = MagicMock(check_hostname=False, verify_mode=ssl.CERT_REQUIRED)
        with patch.object(pins.ssl, "create_default_context", return_value=context), patch.object(pins.socket, "create_connection") as connect:
            with self.assertRaisesRegex(ValueError, "verification must remain enabled"):
                pins.capture("script.google.com", "127.0.0.1", "default", b"PUBLIC_TEST_CA")
        connect.assert_not_called()

    def test_failed_host_keeps_previous_pinset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "google-pins.json"
            path.write_text('{"old":"keep"}')
            bundle = root / "recorder-update/recorder"
            bundle.mkdir(parents=True)
            (bundle / "google_ca.py").write_text("CA_PEM=b'PUBLIC_TEST_CA'\n")
            with patch.object(pins, "ROOT", root), patch.object(pins, "HOSTS", ("script.google.com",)), patch.object(pins, "PROFILES", ("default",)), patch.object(pins.socket, "getaddrinfo", return_value=[(2, 1, 0, "", ("127.0.0.1", 443))]), patch.object(pins, "capture", side_effect=ssl.SSLCertVerificationError(1, "test")):
                with self.assertRaisesRegex(ValueError, "Could not verify"):
                    pins.collect_pins()
            self.assertEqual(path.read_text(), '{"old":"keep"}')

    def test_only_verified_addresses_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "recorder-update/recorder"
            bundle.mkdir(parents=True)
            (bundle / "google_ca.py").write_text("CA_PEM=b'PUBLIC_TEST_CA'\n")
            def capture(host, address, profile, ca):
                if address == "127.0.0.2":
                    raise ssl.SSLCertVerificationError(1, "test")
                return {"sha256": "a" * 64, "valid_from": 1700000000, "valid_until": 1900000000, "profile": profile, "address": address}
            with patch.object(pins, "ROOT", root), patch.object(pins, "HOSTS", ("script.google.com",)), patch.object(pins, "PROFILES", ("default",)), patch.object(pins.socket, "getaddrinfo", return_value=[(2, 1, 0, "", ("127.0.0.1", 443)), (2, 1, 0, "", ("127.0.0.2", 443))]), patch.object(pins, "capture", side_effect=capture):
                result = pins.collect_pins()
            self.assertEqual(result["hosts"]["script.google.com"]["addresses"], ["127.0.0.1"])
            self.assertEqual((root / "google-pins.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
