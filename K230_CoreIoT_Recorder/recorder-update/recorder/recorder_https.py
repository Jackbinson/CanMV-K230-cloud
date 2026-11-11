"""Small bounded HTTP/1.1 client for CanMV stream semantics and Google APIs."""
import socket
import ssl
import time
try:
    import uhashlib as hashlib
    import ubinascii as binascii
except ImportError:
    import hashlib
    import binascii

HOSTS = ("script.google.com", "script.googleusercontent.com", "www.googleapis.com",
         "content.googleapis.com", "www.google.com")
_software_epoch = None
_software_ticks = None


class HttpError(Exception):
    pass


def parse_url(url):
    if not isinstance(url, str) or not url.startswith("https://") or any(c in url for c in ("\r", "\n", " ", "#")):
        raise HttpError("https_url_invalid")
    pieces = url[8:].split("/", 1)
    host = pieces[0].lower()
    if host.endswith(":443"):
        host = host[:-4]
    if host not in HOSTS:
        raise HttpError("https_host_rejected")
    return host, "/" + pieces[1] if len(pieces) == 2 else "/"


def _unix_seconds(value):
    year, month, day, hour, minute, second = value[:6]
    if not (1970 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31 and
            0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 60):
        raise HttpError("https_clock_value_invalid")
    days = 0
    for item in range(1970, year):
        days += 366 if item % 4 == 0 and (item % 100 != 0 or item % 400 == 0) else 365
    lengths = (31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
               31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    days += sum(lengths[:month - 1]) + day - 1
    return ((days * 24 + hour) * 60 + minute) * 60 + second


def set_utc_clock(value, observe=None):
    """K230 legacy uses RTC.init(date tuple); verify libc/system time after it."""
    import machine
    target = _unix_seconds(value)
    if not 2024 <= value[0] <= 2040:
        raise HttpError("https_clock_value_invalid")
    rtc_factory = getattr(machine, "RTC", None)
    if rtc_factory is None:
        if observe is not None:
            observe("rtc_capabilities", rtc_available=False)
        raise HttpError("https_rtc_unavailable")
    rtc = rtc_factory()
    if observe is not None:
        observe("rtc_capabilities", rtc_available=True, init_available=hasattr(rtc, "init"),
                datetime_available=hasattr(rtc, "datetime"))
    # In old K230 firmware datetime(...) silently acts as a getter. Conversely,
    # newer upstream RTC.init uses the standard tuple including weekday.
    weekday = ((target // 86400) + 3) % 7
    standard = (value[0], value[1], value[2], weekday, value[3], value[4], value[5], 0)
    date = tuple(value[:6])
    candidates = (("k230_init", "init", date + (0, 0), "tuple"),
                  ("k230_init_7", "init", date + (0,), "tuple"),
                  ("rtc_datetime", "datetime", standard, "tuple"),
                  ("rtc_init", "init", standard, "tuple"),
                  ("k230_init_args8", "init", date + (0, 0), "args"),
                  ("k230_init_args7", "init", date + (0,), "args"),
                  ("rtc_init_args", "init", standard, "args"),
                  ("k230_init_keyword", "init", date + (0, 0), "keyword"))
    import os
    errno_fn = getattr(os, "errno", None)
    for label, name, data, mode in candidates:
        setter = getattr(rtc, name, None)
        if setter is None:
            continue
        try:
            if errno_fn is not None:
                errno_fn(0)
            if mode == "args":
                setter(*data)
            elif mode == "keyword":
                setter(datetime=data)
            else:
                setter(data)
            error_number = errno_fn() if errno_fn is not None else None
            actual = _unix_seconds(time.gmtime())
            if observe is not None:
                values = {"clock_method": label, "rtc_year": time.gmtime()[0],
                          "clock_delta_seconds": actual - target, "errno": error_number}
                observe("rtc_set_result", **values)
            if abs(actual - target) <= 5:
                return label
        except Exception as error:
            if observe is not None:
                # These calls receive UTC integers only; no tokens or URLs.
                observe("rtc_set_error", clock_method=label, error_type=type(error).__name__,
                        clock_detail=str(error).replace("\n", " ")[:160],
                        errno=errno_fn() if errno_fn is not None else None)
    raise HttpError("https_clock_not_applied")


def query_ntp_unix():
    """Return UTC Unix seconds without writing the firmware RTC."""
    import os
    import struct
    udp = None
    try:
        address = socket.getaddrinfo("time.google.com", 123, socket.AF_INET, socket.SOCK_DGRAM)[0][-1]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(3)
        packet = bytearray(48)
        packet[0] = 0x23
        packet[40:48] = os.urandom(8)
        udp.sendto(packet, address)
        reply = udp.recv(512)
        if (len(reply) < 48 or reply[0] & 7 != 4 or reply[0] >> 6 == 3 or
                not 1 <= reply[1] <= 15 or reply[24:32] != packet[40:48]):
            raise HttpError("https_ntp_response_invalid")
        stamp = struct.unpack("!I", reply[40:44])[0] - 2208988800
        if not 1704067200 <= stamp < 2240611200:
            raise HttpError("https_ntp_date_invalid")
        return stamp
    finally:
        if udp is not None:
            udp.close()


def seed_software_clock(value):
    """Seed UTC from NTP or the trusted local USB host, independent of RTC."""
    global _software_epoch, _software_ticks
    stamp = _unix_seconds(value) if isinstance(value, (tuple, list)) else int(value)
    if not 1704067200 <= stamp < 2240611200:
        raise HttpError("https_clock_value_invalid")
    _software_epoch, _software_ticks = stamp, time.ticks_ms()


def software_time():
    global _software_epoch, _software_ticks
    age = time.ticks_diff(time.ticks_ms(), _software_ticks) if _software_ticks is not None else -1
    if _software_epoch is None or age < 0 or age >= 3600000:
        try:
            seed_software_clock(query_ntp_unix())
        except Exception:
            raise HttpError("https_software_time_unavailable")
        age = time.ticks_diff(time.ticks_ms(), _software_ticks)
    return _software_epoch + age // 1000


def verify_pinned_peer(stream, host, pins, stamp):
    """Exact certificates were CA/hostname verified by the laptop beforehand."""
    host_pins = pins.get("hosts", {}).get(host, {}).get("certificates", [])
    if not host_pins:
        raise HttpError("https_pins_missing_for_host")
    get_peer = getattr(stream, "getpeercert", None)
    if get_peer is None:
        raise HttpError("https_peer_certificate_api_missing")
    certificate = get_peer(True)
    if not isinstance(certificate, bytes) or not certificate:
        raise HttpError("https_peer_certificate_missing")
    digest = hashlib.sha256()
    digest.update(certificate)
    fingerprint = binascii.hexlify(digest.digest()).decode()
    for item in host_pins:
        if item.get("sha256") == fingerprint:
            start, end = item.get("valid_from"), item.get("valid_until")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                raise HttpError("https_pin_metadata_invalid")
            if not start <= stamp < end:
                raise HttpError("https_pin_expired_or_future")
            return
    raise HttpError("https_certificate_changed_refresh_pins")


def sync_clock():
    """NTP is attempted once by each client; a plausible RTC permits offline NTP."""
    import os
    import struct
    current_year = time.gmtime()[0]
    udp = None
    attempted_set = False
    try:
        address = socket.getaddrinfo("time.google.com", 123, socket.AF_INET, socket.SOCK_DGRAM)[0][-1]
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(3)
        packet = bytearray(48)
        packet[0] = 0x23
        packet[40:48] = os.urandom(8)
        udp.sendto(packet, address)
        reply = udp.recv(512)
        if (len(reply) < 48 or reply[0] & 7 != 4 or reply[0] >> 6 == 3 or
                not 1 <= reply[1] <= 15 or reply[24:32] != packet[40:48]):
            raise HttpError("https_ntp_response_invalid")
        offset = 2208988800 if time.gmtime(0)[0] == 1970 else 3155673600
        stamp = struct.unpack("!I", reply[40:44])[0] - offset
        value = time.gmtime(stamp)
        if not 2024 <= value[0] <= 2040:
            raise HttpError("https_ntp_date_invalid")
        attempted_set = True
        set_utc_clock(value)
    except Exception:
        if attempted_set:
            raise HttpError("https_clock_not_applied")
        if not 2024 <= current_year <= 2040:
            raise HttpError("https_clock_not_set")
    finally:
        if udp is not None:
            udp.close()


class Reader:
    def __init__(self, stream, timeout, observe):
        self.stream = stream
        self.timeout = timeout
        self.observe = observe
        self.last = time.ticks_ms()
        self.reported = 0

    def read_some(self, count, eof=False):
        while True:
            try:
                value = self.stream.read(count)
            except OSError as error:
                args = getattr(error, "args", ())
                if args and args[0] in (110, 116):
                    raise HttpError("https_socket_timeout")
                if not args or args[0] != 11:
                    raise
                value = None
            if value:
                self.last = time.ticks_ms()
                self.reported = 0
                return value
            if value == b"":
                if eof:
                    return b""
                # On this port a receive timeout may surface as EOF. Reusing
                # that SSL state cannot recover; the caller must reconnect.
                raise HttpError("https_peer_closed_or_timed_out")
            waited = time.ticks_diff(time.ticks_ms(), self.last)
            if waited >= self.timeout * 1000:
                raise HttpError("https_response_timeout")
            if waited // 10000 > self.reported:
                self.reported = waited // 10000
                self.observe("waiting", wait_seconds=waited // 1000)
            time.sleep_ms(10)

    def exact(self, count):
        data = bytearray()
        while len(data) < count:
            data.extend(self.read_some(min(count - len(data), 1024)))
        return bytes(data)

    def line(self, limit=8192):
        # CanMV readline() may return an empty/fragmented line on a read timeout.
        # Read one byte at a time here; retain fragments until CRLF or a deadline.
        data = bytearray()
        while len(data) < limit:
            value = self.read_some(1)
            data.extend(value)
            if data[-1] == 10:
                return bytes(data)
        raise HttpError("https_line_too_large")


def read_response(reader, method="GET", max_body=65536):
    for _ in range(5):
        line = reader.line(2048).strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if (len(parts) < 2 or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1") or
                len(parts[1]) != 3 or not parts[1].isdigit()):
            raise HttpError("https_status_invalid")
        status = int(parts[1])
        if not 100 <= status <= 599:
            raise HttpError("https_status_invalid")
        headers = {}
        total = 0
        for _ in range(128):
            raw = reader.line()
            total += len(raw)
            if total > 32768:
                raise HttpError("https_headers_too_large")
            if raw in (b"\r\n", b"\n"):
                break
            if b":" not in raw:
                raise HttpError("https_header_invalid")
            name, value = raw.split(b":", 1)
            name, value = name.decode().strip().lower(), value.decode().strip()
            if name == "content-length" and name in headers and headers[name] != value:
                raise HttpError("https_length_conflict")
            headers[name] = value
        else:
            raise HttpError("https_headers_too_many")
        if status < 200:
            continue
        break
    else:
        raise HttpError("https_status_missing")
    reader.observe("response", http_status=status)
    if method == "HEAD" or status in (204, 304):
        return status, headers, b""
    reader.observe("body", http_status=status)
    if headers.get("content-encoding", "identity").lower() not in ("identity", ""):
        raise HttpError("https_content_encoding_unsupported")
    transfer = headers.get("transfer-encoding", "").lower()
    if transfer:
        if transfer != "chunked":
            raise HttpError("https_transfer_encoding_unsupported")
        result = bytearray()
        while True:
            size_text = reader.line(128).strip().split(b";", 1)[0]
            try:
                size = int(size_text, 16)
            except ValueError:
                raise HttpError("https_chunk_size_invalid")
            if size < 0 or size + len(result) > max_body:
                raise HttpError("https_body_too_large")
            if size == 0:
                for _ in range(64):
                    if reader.line() in (b"\r\n", b"\n"):
                        return status, headers, bytes(result)
                raise HttpError("https_trailers_too_many")
            result.extend(reader.exact(size))
            if reader.exact(2) != b"\r\n":
                raise HttpError("https_chunk_terminator_invalid")
    if "content-length" in headers:
        value = headers["content-length"]
        if not value.isdigit() or int(value) > max_body:
            raise HttpError("https_content_length_invalid")
        return status, headers, reader.exact(int(value))
    result = bytearray()
    while True:
        data = reader.read_some(1024, eof=True)
        if not data:
            return status, headers, bytes(result)
        result.extend(data)
        if len(result) > max_body:
            raise HttpError("https_body_too_large")


def write_all(stream, data, timeout):
    view = memoryview(data)
    offset = 0
    progress = time.ticks_ms()
    while offset < len(view):
        try:
            count = stream.write(view[offset:offset + 1024])
        except OSError as error:
            args = getattr(error, "args", ())
            if not args or args[0] not in (11, 110, 116):
                raise
            count = None
        if count:
            offset += count
            progress = time.ticks_ms()
        else:
            if time.ticks_diff(time.ticks_ms(), progress) >= timeout * 1000:
                raise HttpError("https_write_timeout")
            time.sleep_ms(10)


def close_connection(stream, raw):
    # The SSL stream owns/closes raw. K230 raw.close() does not invalidate fd;
    # closing both can close a reused descriptor and abort a later connection.
    owner = stream if stream is not None else raw
    if owner is not None:
        try:
            owner.close()
        except Exception:
            pass


class Http11Client:
    def __init__(self, ca_pem=None, timeout=60, observe=None, socket_module=None,
                 wrap=None, clock_sync=None, pins=None):
        if ca_pem is None:
            from google_ca import CA_PEM
            ca_pem = CA_PEM
        self.ca_pem = ca_pem
        self.timeout = timeout
        self.observe = observe
        self.socket_module = socket_module or socket
        self.wrap = wrap
        self.pins = pins
        if pins is not None and (not isinstance(pins, dict) or pins.get("version") != 1 or not isinstance(pins.get("hosts"), dict)):
            raise HttpError("https_pin_config_invalid")
        self.clock_sync = clock_sync or (software_time if pins is not None else sync_clock)
        self.clock_checked = False
        self.addresses = {}
        self.saved_addresses_tried = set()

    def notify(self, phase, host, **values):
        if self.observe is not None:
            self.observe(phase, host, **values)

    def once(self, method, url, body=None, headers=None):
        host, path = parse_url(url)
        if method not in ("GET", "POST", "PUT", "HEAD"):
            raise HttpError("https_method_rejected")
        if not self.clock_checked:
            self.notify("clock", host)
            self.clock_sync()
            if self.pins is not None:
                epoch = 946684800 if time.gmtime(0)[0] == 2000 else 0
                self.notify("software_clock", host, verification_year=time.gmtime(software_time() - epoch)[0])
            self.clock_checked = True
        if host not in self.addresses:
            self.notify("dns", host)
            known = self.pins.get("hosts", {}).get(host, {}) if self.pins is not None else {}
            saved = []
            for address in known.get("addresses", []) if host not in self.saved_addresses_tried else []:
                parts = address.split(".")
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    saved.append((self.socket_module.AF_INET, self.socket_module.SOCK_STREAM, 0, "", (address, 443)))
            self.saved_addresses_tried.add(host)
            self.addresses[host] = saved or self.socket_module.getaddrinfo(host, 443,
                self.socket_module.AF_INET, self.socket_module.SOCK_STREAM)
        raw, stream = None, None
        try:
            last_error = None
            for address in self.addresses[host][:3]:
                try:
                    raw = self.socket_module.socket(address[0], address[1], address[2])
                    raw.settimeout(15)
                    self.notify("connect", host)
                    raw.connect(address[-1])
                    self.notify("tls", host)
                    if self.wrap is not None:
                        stream = self.wrap(raw, host, self.ca_pem)
                    elif self.pins is not None:
                        # Native date checks use the broken system clock on this
                        # firmware. Authenticate the exact CA-verified certificate
                        # and its validity with software UTC BEFORE any HTTP write.
                        stream = ssl.wrap_socket(raw, server_hostname=host, cert_reqs=ssl.CERT_NONE)
                    else:
                        stream = ssl.wrap_socket(raw, server_hostname=host,
                            cert_reqs=ssl.CERT_REQUIRED, cadata=self.ca_pem)
                    if self.pins is not None:
                        verify_pinned_peer(stream, host, self.pins, software_time())
                        self.notify("certificate_pin_verified", host)
                    # Keep a successful address first for subsequent requests.
                    self.addresses[host] = [address] + [a for a in self.addresses[host] if a != address]
                    # A 2s native timeout was shorter than Apps Script response
                    # latency and could permanently end the SSL input stream.
                    if hasattr(stream, "settimeout"):
                        stream.settimeout(self.timeout)
                    else:
                        raw.settimeout(self.timeout)
                    last_error = None
                    break
                except Exception as error:
                    last_error = error
                    close_connection(stream, raw)
                    raw, stream = None, None
            if last_error is not None:
                self.addresses.pop(host, None)
                raise last_error
            if stream is None:
                raise HttpError("https_no_address")
            if isinstance(body, str):
                body = body.encode()
            body = body or b""
            lines = [method + " " + path + " HTTP/1.1", "Host: " + host,
                     "User-Agent: K230Recorder/2026.11", "Accept: application/json",
                     "Accept-Encoding: identity", "Connection: close"]
            for name, value in (headers or {}).items():
                if name.lower() in ("host", "connection", "content-length", "transfer-encoding", "accept-encoding"):
                    continue
                if any(c in name + str(value) for c in ("\r", "\n")):
                    raise HttpError("https_request_header_invalid")
                lines.append(name + ": " + str(value))
            if body or method in ("POST", "PUT"):
                lines.append("Content-Length: " + str(len(body)))
            self.notify("write", host)
            write_all(stream, ("\r\n".join(lines) + "\r\n\r\n").encode(), self.timeout)
            if body:
                write_all(stream, body, self.timeout)
            self.notify("status", host, timeout_seconds=self.timeout)
            reader = Reader(stream, self.timeout, lambda phase, **data: self.notify(phase, host, **data))
            return read_response(reader, method)
        except Exception:
            self.addresses.pop(host, None)
            raise
        finally:
            close_connection(stream, raw)

    def _request_redirects(self, method, url, body=None, headers=None):
        for _ in range(6):
            host, _ = parse_url(url)
            status, response_headers, data = self.once(method, url, body, headers)
            location = response_headers.get("location")
            if location and status in (301, 302, 303, 307, 308) and not (status == 308 and method == "PUT"):
                if location.startswith("//"):
                    location = "https:" + location
                elif location.startswith("/"):
                    location = "https://" + host + location
                next_host, _ = parse_url(location)
                if status in (301, 302, 303) and method != "HEAD":
                    method, body = "GET", None
                elif method in ("POST", "PUT") and next_host != host:
                    raise HttpError("https_body_redirect_rejected")
                self.notify("redirect", next_host, http_status=status)
                url = location
                continue
            return status, response_headers, data
        raise HttpError("https_redirect_limit")

    def request(self, method, url, body=None, headers=None):
        # Retry the original GET so Apps Script creates a fresh one-time
        # ContentService URL. Never replay an ambiguous POST or upload PUT.
        attempts = 2 if method in ("GET", "HEAD") else 1
        for attempt in range(attempts):
            try:
                return self._request_redirects(method, url, body, headers)
            except Exception as error:
                retry = (isinstance(error, HttpError) and str(error) in (
                    "https_peer_closed_or_timed_out", "https_socket_timeout", "https_response_timeout"))
                args = getattr(error, "args", ())
                if isinstance(error, OSError) and args and args[0] in (32, 101, 103, 104, 110, 111, 113, 116):
                    retry = True
                if not retry or attempt + 1 >= attempts:
                    raise
                host, _ = parse_url(url)
                self.notify("retry_original_get", host, request_attempt=attempt + 2)
                time.sleep_ms(1500)
                url += ("&" if "?" in url else "?") + "_transport_retry=" + str(time.ticks_ms())
