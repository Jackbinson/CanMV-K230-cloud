"""HTTP/1.1 framing, redirects, partial I/O and TLS policy checks."""
from pathlib import Path
import socket
import ssl
import struct
import hashlib
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "recorder-update/recorder"))
import recorder_https as https


class Clock:
    def __init__(self):
        self.now = 0
    def ticks_ms(self):
        self.now += 1
        return self.now
    def ticks_diff(self, a, b):
        return a - b
    def sleep_ms(self, value):
        self.now += value
    def gmtime(self, value=None):
        return time.gmtime(value) if value is not None else (1970, 1, 1, 0, 0, 0, 3, 1, 0)


class Stream:
    def __init__(self, response, transient_pending=0):
        self.response = bytearray(response)
        self.output = bytearray()
        self.transient_pending = transient_pending
        self.closed = False
        self.close_count = 0
    def read(self, count):
        if self.transient_pending:
            self.transient_pending -= 1
            return None
        size = min(count, len(self.response), 3)
        data = bytes(self.response[:size])
        del self.response[:size]
        return data
    def write(self, data):
        size = min(len(data), 7)
        self.output.extend(data[:size])
        return size
    def close(self):
        self.closed = True
        self.close_count += 1
    def settimeout(self, value):
        self.timeout = value
    def connect(self, address):
        self.address = address


class HttpsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.patch = patch.object(https, "time", self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        https._software_epoch = None
        https._software_ticks = None

    def read(self, response, **kwargs):
        return https.read_response(https.Reader(Stream(response), 1, lambda *a, **k: None), **kwargs)

    def client(self, responses, wrap=None, pins=None):
        streams = [Stream(response) for response in responses]
        pending = iter(streams)
        network = types.SimpleNamespace(AF_INET=socket.AF_INET, SOCK_STREAM=socket.SOCK_STREAM,
            getaddrinfo=lambda *args: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
            socket=lambda *args: next(pending))
        client = https.Http11Client(ca_pem=b"TEST_PUBLIC_CA", socket_module=network,
            wrap=wrap or (lambda stream, host, ca: stream), clock_sync=lambda: None, timeout=1, pins=pins)
        return client, streams

    def pins(self, certificate=b"TEST_TRUSTED_GOOGLE_CERT"):
        stamp = https._unix_seconds((2026, 11, 11, 14, 30, 0))
        return {"version": 1, "hosts": {"script.google.com": {"addresses": [], "certificates": [
            {"sha256": hashlib.sha256(certificate).hexdigest(), "valid_from": stamp - 100,
             "valid_until": stamp + 3600}]}}}

    def test_fragmented_status_and_pending_reads_are_retained(self):
        stream = Stream(b"\r\nHTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello", transient_pending=2)
        result = https.read_response(https.Reader(stream, 1, lambda *a, **k: None))
        self.assertEqual(result[0], 200)
        self.assertEqual(result[2], b"hello")

    def test_chunked_body_with_extensions_and_trailers(self):
        result = self.read(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2;extension=x\r\nhe\r\n3\r\nllo\r\n0\r\nX-Trailer: yes\r\n\r\n")
        self.assertEqual(result[2], b"hello")

    def test_informational_response_is_skipped(self):
        result = self.read(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 204 No Content\r\n\r\n")
        self.assertEqual(result[0], 204)

    def test_conflicting_lengths_are_rejected(self):
        with self.assertRaisesRegex(https.HttpError, "length_conflict"):
            self.read(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello")

    def test_truncated_response_requires_fresh_connection(self):
        with self.assertRaisesRegex(https.HttpError, "peer_closed_or_timed_out"):
            self.read(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhe")

    def test_native_timeout_is_not_polled_again_on_same_ssl_stream(self):
        stream = types.SimpleNamespace(read=lambda count: (_ for _ in ()).throw(OSError(110)))
        with self.assertRaisesRegex(https.HttpError, "https_socket_timeout"):
            https.Reader(stream, 60, lambda *a, **k: None).read_some(1)

    def test_tls_owner_closes_raw_once_on_success_and_pin_failure(self):
        class TlsOwner:
            def __init__(self, raw):
                self.raw = raw
            def __getattr__(self, name):
                return getattr(self.raw, name)
            def close(self):
                self.raw.close()
        client, streams = self.client([b"HTTP/1.1 204 No Content\r\n\r\n"],
                                     wrap=lambda raw, *args: TlsOwner(raw))
        client.timeout = 60
        client.request("GET", "https://www.google.com/generate_204")
        self.assertEqual(streams[0].timeout, 60)
        self.assertEqual(streams[0].close_count, 1)
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        client, streams = self.client([b""], pins=self.pins(),
                                     wrap=lambda raw, *args: TlsOwner(raw))
        streams[0].getpeercert = lambda binary: b"UNKNOWN_CERT"
        with self.assertRaisesRegex(https.HttpError, "certificate_changed"):
            client.request("POST", "https://script.google.com/test", b"TEST_SECRET")
        self.assertEqual(streams[0].close_count, 1)
        self.assertFalse(streams[0].output)

    def test_get_timeout_restarts_original_url_for_new_redirect(self):
        redirect = lambda suffix: (b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/echo?" +
                                   suffix + b"\r\nContent-Length: 0\r\n\r\n")
        client, streams = self.client([redirect(b"FIRST"), b"", redirect(b"SECOND"),
                                      b"HTTP/1.1 204 No Content\r\n\r\n"])
        self.assertEqual(client.request("GET", "https://script.google.com/exec?action=nonce")[0], 204)
        self.assertIn(b"GET /exec?action=nonce&_transport_retry=", streams[2].output)
        self.assertIn(b"GET /echo?SECOND HTTP/1.1", streams[3].output)
        self.assertTrue(all(stream.close_count == 1 for stream in streams))

    def test_aborted_get_connect_retries_with_new_socket(self):
        client, streams = self.client([b"", b"HTTP/1.1 204 No Content\r\n\r\n"])
        streams[0].connect = lambda address: (_ for _ in ()).throw(OSError(103))
        self.assertEqual(client.request("GET", "https://script.google.com/exec?action=nonce")[0], 204)
        self.assertEqual(streams[0].close_count, 1)
        self.assertEqual(streams[1].close_count, 1)

    def test_ambiguous_post_or_put_is_not_automatically_replayed(self):
        for method in ("POST", "PUT"):
            with self.subTest(method=method):
                client, streams = self.client([b"", b"HTTP/1.1 204 No Content\r\n\r\n"])
                with self.assertRaisesRegex(https.HttpError, "peer_closed_or_timed_out"):
                    client.request(method, "https://script.google.com/exec", b"TEST_SECRET")
                self.assertTrue(streams[0].closed)
                self.assertFalse(streams[1].output)

    def test_oversized_response_is_bounded_before_read(self):
        with self.assertRaisesRegex(https.HttpError, "content_length_invalid"):
            self.read(b"HTTP/1.1 200 OK\r\nContent-Length: 9000000\r\n\r\n")

    def test_write_all_handles_partial_writes(self):
        stream = Stream(b"")
        https.write_all(stream, b"x" * 262144, 1)
        self.assertEqual(len(stream.output), 262144)

    def test_google_redirect_changes_post_to_get_without_token(self):
        responses = [b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/echo?PRIVATE\r\nContent-Length: 0\r\n\r\n",
                     b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}']
        client, streams = self.client(responses)
        self.assertEqual(client.request("POST", "https://script.google.com/macros/s/TEST/exec", b"TEST_PRIVATE_TOKEN")[0], 200)
        self.assertIn(b"POST /macros/s/TEST/exec HTTP/1.1", streams[0].output)
        self.assertIn(b"GET /echo?PRIVATE HTTP/1.1", streams[1].output)
        self.assertNotIn(b"TEST_PRIVATE_TOKEN", streams[1].output)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_drive_308_is_returned_as_resume_incomplete(self):
        client, streams = self.client([b"HTTP/1.1 308 Resume Incomplete\r\nRange: bytes=0-10\r\nContent-Length: 0\r\n\r\n"])
        result = client.request("PUT", "https://www.googleapis.com/upload/drive/v3/files?PRIVATE", b"hello")
        self.assertEqual(result[0], 308)
        self.assertEqual(result[1]["range"], "bytes=0-10")
        self.assertEqual(len(streams), 1)

    def test_unknown_redirect_host_is_rejected(self):
        client, _ = self.client([b"HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/token\r\nContent-Length: 0\r\n\r\n"])
        with self.assertRaisesRegex(https.HttpError, "host_rejected"):
            client.request("POST", "https://script.google.com/test", b"TEST_SECRET")

    def test_tls_always_requires_google_ca_and_sni(self):
        client, streams = self.client([b"HTTP/1.1 204 No Content\r\n\r\n"])
        client.wrap = None
        with patch.object(https.ssl, "wrap_socket", return_value=streams[0]) as wrap:
            client.request("GET", "https://www.google.com/generate_204")
        self.assertEqual(wrap.call_args.kwargs["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertEqual(wrap.call_args.kwargs["cadata"], b"TEST_PUBLIC_CA")
        self.assertEqual(wrap.call_args.kwargs["server_hostname"], "www.google.com")

    def test_tls_verification_failure_never_falls_back_unverified(self):
        client, streams = self.client([b""])
        client.wrap = None
        with patch.object(https.ssl, "wrap_socket", side_effect=OSError(-9984)) as wrap:
            with self.assertRaises(OSError):
                client.request("GET", "https://script.google.com/test")
        self.assertEqual(wrap.call_count, 1)
        self.assertEqual(wrap.call_args.kwargs["cert_reqs"], ssl.CERT_REQUIRED)
        self.assertTrue(streams[0].closed)

    def test_ntp_sync_sets_utc_rtc_and_checks_request_nonce(self):
        recorded = []
        state = [(1970, 1, 1, 0, 0, 0, 3, 1, 0)]
        class Udp:
            def settimeout(self, value):
                pass
            def sendto(self, data, address):
                self.sent = data
            def recv(self, count):
                data = bytearray(48)
                data[0], data[1] = 0x24, 1
                data[24:32] = self.sent[40:48]
                data[40:44] = struct.pack("!I", 2208988800 + 1794407400)
                return data
            def close(self):
                pass
        network = types.SimpleNamespace(AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
            getaddrinfo=lambda *args: [(None, None, None, None, ("127.0.0.1", 123))], socket=lambda *a: Udp())
        def init(value):
            recorded.append(value)
            state[0] = tuple(value[:6]) + (0, 1, 0)
        machine = types.SimpleNamespace(RTC=lambda: types.SimpleNamespace(init=init))
        def gmtime(value=None):
            return time.gmtime(value) if value is not None else state[0]
        with patch.object(https, "time", types.SimpleNamespace(gmtime=gmtime)), patch.object(https, "socket", network), patch.dict(sys.modules, {"machine": machine}):
            https.sync_clock()
        expected = time.gmtime(1794407400)
        self.assertEqual(recorded, [tuple(expected[:6]) + (0, 0)])

    def test_ntp_failure_rejects_unset_clock_but_keeps_plausible_rtc(self):
        network = types.SimpleNamespace(AF_INET=socket.AF_INET, SOCK_DGRAM=socket.SOCK_DGRAM,
            getaddrinfo=lambda *a: (_ for _ in ()).throw(OSError(110)))
        with patch.object(https, "socket", network):
            with patch.object(https, "time", types.SimpleNamespace(gmtime=lambda: (1970,))):
                with self.assertRaisesRegex(https.HttpError, "clock_not_set"):
                    https.sync_clock()
            with patch.object(https, "time", types.SimpleNamespace(gmtime=lambda: (2026,))):
                https.sync_clock()

    def test_k230_legacy_init_is_used_when_datetime_is_getter_only(self):
        state = [(1970, 1, 1, 0, 0, 0)]
        calls = []
        class Rtc:
            def datetime(self, *args):
                return state[0]
            def init(self, value):
                calls.append(value)
                state[0] = value[:6]
        with patch.object(https, "time", types.SimpleNamespace(gmtime=lambda: state[0])), patch.dict(sys.modules, {"machine": types.SimpleNamespace(RTC=Rtc)}):
            method = https.set_utc_clock((2026, 11, 11, 14, 30, 45))
        self.assertEqual(method, "k230_init")
        self.assertEqual(calls, [(2026, 11, 11, 14, 30, 45, 0, 0)])

    def test_modern_rtc_standard_setter_is_supported(self):
        state = [(1970, 1, 1, 0, 0, 0)]
        class Rtc:
            def init(self, value):
                if len(value) != 8:
                    raise ValueError()
                state[0] = value[:3] + value[4:7]
            def datetime(self, value):
                state[0] = value[:3] + value[4:7]
        with patch.object(https, "time", types.SimpleNamespace(gmtime=lambda: state[0])), patch.dict(sys.modules, {"machine": types.SimpleNamespace(RTC=Rtc)}):
            method = https.set_utc_clock((2026, 11, 11, 14, 30, 45))
        self.assertEqual(method, "rtc_datetime")
        self.assertEqual(state[0], (2026, 11, 11, 14, 30, 45))

    def test_silent_rtc_failure_is_rejected_before_tls(self):
        rtc = types.SimpleNamespace(init=lambda *args: None, datetime=lambda *args: None)
        with patch.object(https, "time", types.SimpleNamespace(gmtime=lambda: (1970, 1, 1, 0, 0, 0))), patch.dict(sys.modules, {"machine": types.SimpleNamespace(RTC=lambda: rtc)}):
            with self.assertRaisesRegex(https.HttpError, "clock_not_applied"):
                https.set_utc_clock((2026, 11, 11, 14, 30, 45))

    def test_software_clock_works_while_firmware_calendar_stays_1970(self):
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        self.clock.sleep_ms(5000)
        self.assertEqual(https.software_time(), https._unix_seconds((2026, 11, 11, 14, 30, 5)))
        self.assertEqual(https.time.gmtime()[0], 1970)

    def test_pinned_google_certificate_is_checked_before_request_write(self):
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        client, streams = self.client([b"HTTP/1.1 204 No Content\r\n\r\n"], pins=self.pins())
        streams[0].getpeercert = lambda binary: b"TEST_TRUSTED_GOOGLE_CERT"
        status, _, _ = client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertEqual(status, 204)
        self.assertIn(b"TEST_PRIVATE_TOKEN", streams[0].output)

    def test_unknown_peer_pin_sends_no_http_headers_or_token(self):
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        client, streams = self.client([b""], pins=self.pins())
        streams[0].getpeercert = lambda binary: b"UNTRUSTED_PEER_CERT"
        with self.assertRaisesRegex(https.HttpError, "certificate_changed"):
            client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertEqual(bytes(streams[0].output), b"")
        self.assertTrue(streams[0].closed)

    def test_expired_pin_sends_no_credentials(self):
        https.seed_software_clock((2026, 11, 12, 14, 30, 0))
        client, streams = self.client([b""], pins=self.pins())
        streams[0].getpeercert = lambda binary: b"TEST_TRUSTED_GOOGLE_CERT"
        with self.assertRaisesRegex(https.HttpError, "expired_or_future"):
            client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertFalse(streams[0].output)

    def test_pin_requires_peer_certificate_api(self):
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        client, streams = self.client([b""], pins=self.pins())
        with self.assertRaisesRegex(https.HttpError, "certificate_api_missing"):
            client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertFalse(streams[0].output)

    def test_no_software_time_never_opens_authenticated_request(self):
        client, streams = self.client([b""], pins=self.pins())
        with patch.object(https, "query_ntp_unix", side_effect=OSError(110)):
            with self.assertRaisesRegex(https.HttpError, "software_time_unavailable"):
                client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertFalse(streams[0].output)

    def test_native_pin_mode_never_sends_data_before_manual_peer_check(self):
        https.seed_software_clock((2026, 11, 11, 14, 30, 0))
        client, streams = self.client([b""], pins=self.pins())
        client.wrap = None
        streams[0].getpeercert = lambda binary: b"UNTRUSTED_PEER_CERT"
        with patch.object(https.ssl, "wrap_socket", return_value=streams[0]):
            with self.assertRaisesRegex(https.HttpError, "certificate_changed"):
                client.request("POST", "https://script.google.com/test", b"TEST_PRIVATE_TOKEN")
        self.assertFalse(streams[0].output)

    def test_cold_boot_uses_software_ntp_without_touching_rtc(self):
        stamp = https._unix_seconds((2026, 11, 11, 14, 30, 0))
        machine = types.SimpleNamespace(RTC=lambda: (_ for _ in ()).throw(AssertionError("RTC must not be used")))
        with patch.object(https, "query_ntp_unix", return_value=stamp) as query, patch.dict(sys.modules, {"machine": machine}):
            self.assertEqual(https.software_time(), stamp)
            self.clock.sleep_ms(5000)
            self.assertEqual(https.software_time(), stamp + 5)
            self.assertEqual(query.call_count, 1)
        self.assertEqual(https.time.gmtime()[0], 1970)


if __name__ == "__main__":
    unittest.main()
