"""TLS ping to Apps Script with explicit handling of ContentService redirects."""
import ujson as json
import sys
import time

try:
    import usocket as socket
except ImportError:
    import socket

CONFIG_PATH = "/sdcard/recorder/drive_upload_config.json"


def _header(headers, name):
    wanted = name.lower()
    for key in headers:
        if str(key).lower() == wanted:
            return str(headers[key])
    return ""


def _read_response(stream, limit=8192):
    """Collect one small HTTP response using the CanMV HTTPS read pattern."""
    data = b""
    while True:
        chunk = stream.read(512)
        if not chunk:
            return data
        data += chunk
        if len(data) > limit:
            raise OSError("HTTP response exceeded diagnostic limit")


def _decode_chunked(data):
    chunks = []
    offset = 0
    while True:
        end = data.find(b"\r\n", offset)
        if end < 0:
            raise OSError("HTTP chunk header incomplete")
        size = int(data[offset:end].split(b";", 1)[0].strip(), 16)
        offset = end + 2
        if size == 0:
            return b"".join(chunks)
        if offset + size + 2 > len(data):
            raise OSError("HTTP chunk body incomplete")
        chunks.append(data[offset:offset + size])
        offset += size + 2


def _write_all(stream, data):
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if written is None:
            # Some MicroPython stream wrappers return None after a full write.
            return
        if written <= 0:
            raise OSError("HTTPS request write stopped early")
        offset += written


def _https_request(method, url, body=None, read_body=True):
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS redirects are accepted")
    rest = url[8:]
    slash = rest.find("/")
    if slash < 0:
        host, path = rest, "/"
    else:
        host, path = rest[:slash], rest[slash:]
    if not host or not path.startswith("/"):
        raise ValueError("Invalid HTTPS URL")

    print("RAW_HTTPS_REQUEST", method, host)
    address = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0]
    raw = socket.socket(address[0], address[1], address[2])
    stream = None
    try:
        try:
            raw.settimeout(20)
        except Exception:
            pass
        raw.connect(address[-1])
        try:
            import ussl as ssl
        except ImportError:
            import ssl
        # Match the CanMV requests package's TLS mode; preserve SNI for Google.
        stream = ssl.wrap_socket(raw, server_hostname=host, cert_reqs=ssl.CERT_NONE)

        body_bytes = body or b""
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        lines = [
            method.encode("ascii") + b" " + path.encode("utf-8") + b" HTTP/1.1\r\n",
            b"Host: " + host.encode("utf-8") + b"\r\n",
            b"Connection: close\r\n",
            b"Accept: application/json\r\n",
            b"User-Agent: CanMV-K230\r\n",
        ]
        if body is not None:
            lines.append(b"Content-Type: application/json\r\n")
            lines.append(b"Content-Length: " + str(len(body_bytes)).encode("ascii") + b"\r\n")
        lines.append(b"\r\n")
        request_bytes = b"".join(lines)
        if body is not None:
            request_bytes += body_bytes
        _write_all(stream, request_bytes)
        try:
            stream.flush()
        except Exception:
            pass
        print("RAW_HTTPS_SENT_BYTES", len(request_bytes))

        response = _read_response(stream)
        print("RAW_HTTPS_RESPONSE_BYTES", len(response))
        header_end = response.find(b"\r\n\r\n")
        status_line = response.split(b"\r\n", 1)[0]
        print("RAW_HTTPS_STATUS_LINE_BYTES", len(status_line))
        parts = status_line.split(None, 2)
        if len(parts) < 2:
            raise OSError("HTTP bad status line: " + repr(status_line))
        status = int(parts[1])
        print("RAW_HTTPS_STATUS", status)
        if header_end < 0:
            raise OSError("HTTP response headers incomplete")
        response_headers = {}
        header_lines = response[:header_end].split(b"\r\n")[1:]
        for line in header_lines:
            if b":" in line:
                key, value = line.split(b":", 1)
                response_headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
        if 300 <= status < 400:
            return status, response_headers, b""
        if not read_body:
            return status, response_headers, b""
        body = response[header_end + 4:]
        transfer = _header(response_headers, "Transfer-Encoding").lower()
        if "chunked" in transfer:
            body = _decode_chunked(body)
        else:
            length = _header(response_headers, "Content-Length")
            if length:
                body = body[:int(length)]
        return status, response_headers, body
    finally:
        try:
            (stream or raw).close()
        except Exception:
            pass


def _resolve_location(base_url, location):
    if location.startswith("https://"):
        return location
    if location.startswith("//"):
        return "https:" + location
    if location.startswith("/"):
        rest = base_url[8:]
        slash = rest.find("/")
        host = rest if slash < 0 else rest[:slash]
        return "https://" + host + location
    raise OSError("Unsupported Apps Script redirect format")


def _post_json_follow_redirects(start_url, payload):
    current_url = start_url
    status, headers, body = _https_request("POST", current_url, payload)
    redirects = 0
    while status in (301, 302, 303, 307, 308) and redirects < 3:
        location = _header(headers, "Location")
        if not location:
            raise OSError("Apps Script redirect had no Location header")
        method = "GET" if status in (301, 302, 303) else "POST"
        location = _resolve_location(current_url, location)
        status, headers, body = _https_request(
            method, location, None if method == "GET" else payload
        )
        current_url = location
        redirects += 1
        print("APPS_SCRIPT_REDIRECT_STATUS", status)
    if status in (301, 302, 303, 307, 308):
        raise OSError("Too many Apps Script redirects")
    return status, body


def _ping():
    with open(CONFIG_PATH, "r") as stream:
        config = json.load(stream)
    recorder_path = "/sdcard/recorder"
    if recorder_path not in sys.path:
        sys.path.insert(0, recorder_path)
    from wifi_client import ensure_wifi_connected
    if not ensure_wifi_connected(timeout=20):
        raise OSError("Wi-Fi has no active connection")
    webapp_url = config["webapp_url"]

    # Compare a token-free GET on the same Google host before the POST probe.
    host_status, _, _ = _https_request(
        "GET", "https://script.google.com/", None, read_body=False
    )
    print("SCRIPT_HOST_GET_STATUS", host_status)

    # Verify that Apps Script answers before sending the device token.
    probe_payload = json.dumps({"action": "ping"})
    status, body = _post_json_follow_redirects(webapp_url, probe_payload)
    if status < 200 or status >= 300:
        raise OSError("Apps Script preflight HTTP " + str(status))
    probe_result = json.loads(body.decode("utf-8"))
    if probe_result.get("error") != "unauthorized":
        raise OSError("Apps Script preflight response was unexpected")
    print("APPS_SCRIPT_PREFLIGHT_OK no-token unauthorized response")

    payload = json.dumps({"action": "ping", "token": config["device_token"]})
    status, body = _post_json_follow_redirects(webapp_url, payload)
    print("APPS_SCRIPT_HTTP_STATUS", status)
    if status < 200 or status >= 300:
        raise OSError("Apps Script HTTP " + str(status))
    result = json.loads(body.decode("utf-8"))
    if not result.get("ok"):
        raise OSError("Apps Script rejected ping: " + str(result.get("error", "unknown")))
    print("APPS_SCRIPT_PING_OK", result.get("service", ""))


try:
    _ping()
except Exception as exc:
    print("APPS_SCRIPT_PING_FAILED", str(exc))
finally:
    # Let CanMV flush the message, then restart the board. Its existing
    # launcher/main.py will resume the previously configured camera program.
    time.sleep_ms(1200)
    import machine
    machine.reset()
