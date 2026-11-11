"""Check RPC packet framing and interleaved MQTT control packets."""
import json
from pathlib import Path
import sys
import time
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "recorder-update/recorder"))
sys.modules.setdefault("utime", types.SimpleNamespace(ticks_ms=lambda: int(time.monotonic() * 1000), ticks_diff=lambda a, b: a-b, sleep_ms=lambda n: None))
import mqtt_transport as mqtt


class Socket:
    def __init__(self, incoming):
        self.incoming = bytearray(incoming)
        self.output = bytearray()
    def write(self, data):
        part = data[:3]
        self.output.extend(part)
        return len(part)
    def recv(self, count):
        size = min(count, 2, len(self.incoming))
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result
    def settimeout(self, value):
        pass


class MqttTests(unittest.TestCase):
    def client(self, incoming):
        client = mqtt.SocketMqttClient("TEST_CLIENT", "test.invalid", 1883, "TEST_ONLY", "", 30)
        client.sock = Socket(incoming)
        return client

    def test_partial_writes_produce_complete_publish(self):
        c = self.client(b"")
        c.publish("test", b"hello")
        self.assertEqual(bytes(c.sock.output), b"\x30\x0b\x00\x04testhello")

    def test_rpc_publish_waiting_before_ping_response_is_kept(self):
        topic = "v1/devices/me/rpc/request/1"
        body = mqtt._mqtt_string(topic) + json.dumps({"method": "stopRecording"}).encode()
        c = self.client(b"\x30" + mqtt._mqtt_remaining_length(len(body)) + body + b"\xd0\x00")
        self.assertEqual(c._read_reply(), (0xd0, b""))
        received = c.check_msg()
        self.assertEqual(received[0], topic)
        self.assertEqual(json.loads(received[1])["method"], "stopRecording")

    def test_oversized_packets_rejected_before_body_allocation(self):
        c = self.client(b"\x30" + mqtt._mqtt_remaining_length(10000000))
        with self.assertRaisesRegex(OSError, "too large"):
            c._read_packet()

    def test_suback_must_match_packet_id_and_qos(self):
        self.client(b"\x90\x03\x00\x01\x00").subscribe("test/+")
        for reply in (b"\x90\x03\x00\x02\x00", b"\x90\x03\x00\x01\x80"):
            with self.assertRaisesRegex(OSError, "SUBACK"):
                self.client(reply).subscribe("test/+")


if __name__ == "__main__":
    unittest.main()
