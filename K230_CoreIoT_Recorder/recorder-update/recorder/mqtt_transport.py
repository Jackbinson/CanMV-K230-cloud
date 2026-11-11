"""MQTT transport adapted from the installed project for Yahboom sockets."""
import socket
import utime

def _mqtt_string(value):
    data = value.encode("utf-8") if isinstance(value, str) else value
    length = len(data)
    if length > 65535:
        raise ValueError("MQTT string is too long")
    return bytes((length >> 8, length & 0xFF)) + data

def _mqtt_remaining_length(length):
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)

class SocketMqttClient:
    """Minimal MQTT 3.1.1 publisher implemented on the firmware socket module."""

    def __init__(
        self,
        client_id,
        host,
        port,
        username,
        password,
        keepalive,
        socket_module=socket,
        timeout=1.5,
    ):
        self.client_id = client_id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.keepalive_seconds = keepalive
        self.socket_module = socket_module
        self.timeout = timeout
        self.sock = None
        self.packet_id = 0
        self.last_keepalive_ms = None
        # Cache the resolved broker address so reconnects skip DNS (getaddrinfo
        # has no timeout of its own and can block indefinitely on a stuck resolver).
        self._resolved_addr = None
        # PUBLISH packets that arrived while waiting for another reply (PINGRESP,
        # SUBACK, PUBACK); check_msg() hands them out before reading the socket.
        self._inbox = []

    def _write_all(self, data):
        offset = 0
        last_progress_ms = utime.ticks_ms()
        while offset < len(data):
            written = self.sock.write(data[offset : offset + 1024])
            if written:
                offset += written
                last_progress_ms = utime.ticks_ms()
                continue
            # None means EAGAIN (send buffer full, or SO_SNDTIMEO expired), not a
            # closed socket: a reset raises OSError. Retry until the stall limit.
            if utime.ticks_diff(utime.ticks_ms(), last_progress_ms) >= self.timeout * 1000:
                raise OSError("MQTT write stalled for %.1fs" % self.timeout)
            utime.sleep_ms(10)

    def _read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                # CanMV recv() returns b'' for a timeout, a reset and a FIN alike.
                raise OSError(
                    "MQTT read got no data in %.1fs (timeout or closed)" % self.timeout
                )
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _parse_publish(body):
        # Subscriptions are QoS 0, so a PUBLISH body has no packet identifier.
        if len(body) < 2:
            raise OSError("Invalid MQTT topic")
        topic_len = (body[0] << 8) | body[1]
        if topic_len == 0 or topic_len + 2 > len(body):
            raise OSError("Invalid MQTT topic")
        return body[2 : 2 + topic_len].decode("utf-8"), body[2 + topic_len :]

    def _read_reply(self):
        """Read the next packet that is not a PUBLISH. CoreIoT can push a
        shared-attribute PUBLISH just before the reply we wait for, so keep it
        for check_msg() instead of treating it as a protocol error."""
        for _ in range(8):
            packet_type, body = self._read_packet()
            if (packet_type & 0xF0) != 0x30:
                return packet_type, body
            self._inbox.append(self._parse_publish(body))
            if len(self._inbox) > 4:
                self._inbox.pop(0)
        raise OSError("MQTT reply missing after 8 PUBLISH packets")

    def _read_packet(self, packet_type=None):
        if packet_type is None:
            packet_type = self._read_exact(1)[0]
        remaining = 0
        multiplier = 1
        for _ in range(4):
            digit = self._read_exact(1)[0]
            remaining += (digit & 0x7F) * multiplier
            if not digit & 0x80:
                if remaining > 4096:
                    raise OSError("MQTT packet too large")
                if (packet_type & 0xF0) == 0x30 and packet_type & 0x06:
                    raise OSError("Unexpected MQTT publish QoS")
                return packet_type, self._read_exact(remaining)
            multiplier *= 128
        raise OSError("Invalid MQTT remaining length")

    def connect(self):
        self.close()
        if self._resolved_addr is None:
            # Unbounded call on this firmware's socket module (no timeout knob).
            # Only paid once per broker-address change, not on every reconnect.
            self._resolved_addr = self.socket_module.getaddrinfo(self.host, self.port)[0][-1]
        address = self._resolved_addr
        self.sock = self.socket_module.socket()
        self.sock.settimeout(self.timeout)
        try:
            self.sock.connect(address)
            variable_header = b"\x00\x04MQTT\x04\xc2" + bytes(
                (
                    self.keepalive_seconds >> 8,
                    self.keepalive_seconds & 0xFF,
                )
            )
            payload = (
                _mqtt_string(self.client_id)
                + _mqtt_string(self.username)
                + _mqtt_string(self.password)
            )
            body = variable_header + payload
            self._write_all(b"\x10" + _mqtt_remaining_length(len(body)) + body)
            packet_type, response = self._read_packet()
            if packet_type != 0x20 or len(response) != 2:
                raise OSError("Invalid MQTT CONNACK")
            if response[1] != 0:
                raise OSError("MQTT CONNACK refused: " + str(response[1]))
            self.last_keepalive_ms = None
        except Exception:
            # Address may be stale (broker moved) — force a fresh DNS lookup next attempt.
            self._resolved_addr = None
            self.close()
            raise

    def publish(self, topic, payload, retain=False, qos=0):
        if qos not in (0, 1):
            raise ValueError("Socket MQTT supports QoS 0 or 1")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        variable_header = _mqtt_string(topic)
        expected_packet_id = None
        if qos == 1:
            self.packet_id = (self.packet_id % 65535) + 1
            expected_packet_id = self.packet_id
            variable_header += bytes((self.packet_id >> 8, self.packet_id & 0xFF))
        header = 0x30 | (qos << 1) | (1 if retain else 0)
        body = variable_header + payload
        self._write_all(bytes((header,)) + _mqtt_remaining_length(len(body)) + body)
        if qos == 1:
            while True:
                packet_type, response = self._read_reply()
                if packet_type == 0xD0:  # Ignore a queued PINGRESP.
                    continue
                if packet_type != 0x40 or len(response) != 2:
                    raise OSError("Invalid MQTT PUBACK")
                received_id = (response[0] << 8) | response[1]
                if received_id != expected_packet_id:
                    raise OSError("MQTT PUBACK packet id mismatch")
                break

    def keepalive(self, now_ms):
        if self.last_keepalive_ms is None:
            self.last_keepalive_ms = now_ms
            return
        elapsed = ((now_ms - self.last_keepalive_ms + (1 << 29)) % (1 << 30)) - (
            1 << 29
        )
        if elapsed < self.keepalive_seconds * 500:
            return
        self._write_all(b"\xc0\x00")
        packet_type, response = self._read_reply()
        if packet_type != 0xD0 or response:
            raise OSError("Invalid MQTT PINGRESP")
        self.last_keepalive_ms = now_ms

    def disconnect(self):
        if self.sock is not None:
            try:
                self._write_all(b"\xe0\x00")
            except Exception:
                pass
        self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def subscribe(self, topic):
        self.packet_id = (self.packet_id % 65535) + 1
        variable_header = bytes((self.packet_id >> 8, self.packet_id & 0xFF))
        body = variable_header + _mqtt_string(topic) + b"\x00"  # QoS 0
        self._write_all(b"\x82" + _mqtt_remaining_length(len(body)) + body)
        packet_type, response = self._read_reply()
        if (packet_type != 0x90 or len(response) != 3 or
                response[:2] != variable_header or response[2] != 0):
            raise OSError("Invalid MQTT SUBACK")

    def check_msg(self, wait=0.02):
        """Return the next incoming (topic, msg) PUBLISH, or None if none is waiting.

        Do not use settimeout(0) here: on mid-2025 CanMV firmware (Yahboom 1.4.1)
        it still waits 50-100 ms, and the socket stays non-blocking even after
        settimeout(self.timeout), so later writes return None. A short positive
        timeout waits about as long and is fully undone afterwards.
        """
        if self._inbox:
            return self._inbox.pop(0)
        if self.sock is None:
            return None
        self.sock.settimeout(wait)
        try:
            first = self.sock.recv(1)
        finally:
            self.sock.settimeout(self.timeout)
        if not first:
            # Nothing waiting (or the broker closed; keepalive will notice).
            return None
        # A packet has started, so read the rest with the normal timeout. If that
        # fails the stream is left mid-packet: raise so the caller reconnects.
        packet_type, body = self._read_packet(first[0])
        if (packet_type & 0xF0) == 0x30:
            return self._parse_publish(body)
        return None
