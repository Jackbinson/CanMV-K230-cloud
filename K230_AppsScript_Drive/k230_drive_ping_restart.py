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
        # Match Kendryte's CanMV HTTPS example: read until the server closes
        # the connection (the request includes Connection: close).
        chunk = stream.read()
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


def _https_request(method, url, body=None, read_body=True,
                   skip_response_read=False):
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

        if skip_response_read:
            print("RAW_HTTPS_RESPONSE_READ_SKIPPED")
            return 0, {}, b""
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


def _append_query(url, query):
    return url + ("&" if "?" in url else "?") + query


def _requests_get_json_follow_redirects(start_url):
    # Prefer CanMV's supported requests package for GET. Its HTTP/TLS stream
    # handling may receive Apps Script's redirect where the raw socket probe
    # sees EOF. Do not pass allow_redirects: this firmware rejects that kwarg.
    import requests
    current_url = start_url
    response = requests.get(current_url)
    redirects = 0
    while response.status_code in (301, 302, 303, 307, 308) and redirects < 3:
        location = _header(response.headers, "Location")
        try:
            response.close()
        except Exception:
            pass
        if not location:
            raise OSError("Apps Script redirect had no Location header")
        current_url = _resolve_location(current_url, location)
        response = requests.get(current_url)
        redirects += 1
        print("REQUESTS_APPS_SCRIPT_REDIRECT_STATUS", response.status_code)
    status = response.status_code
    print("REQUESTS_APPS_SCRIPT_HTTP_STATUS", status)
    try:
        body = response.text
    finally:
        try:
            response.close()
        except Exception:
            pass
    if status in (301, 302, 303, 307, 308):
        raise OSError("Too many Apps Script redirects")
    if status < 200 or status >= 300:
        raise OSError("Apps Script requests GET HTTP " + str(status))
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    return json.loads(body)


def _get_json_follow_redirects(start_url):
    try:
        result = _requests_get_json_follow_redirects(start_url)
        print("CANMV_REQUESTS_GET_OK")
        return result
    except Exception as exc:
        print("CANMV_REQUESTS_GET_FAILED", str(exc))

    # Keep the raw TLS implementation as a fallback so the log distinguishes
    # a requests-package failure from a lower-level socket failure.
    current_url = start_url
    status, headers, body = _https_request("GET", current_url)
    redirects = 0
    while status in (301, 302, 303, 307, 308) and redirects < 3:
        location = _header(headers, "Location")
        if not location:
            raise OSError("Apps Script redirect had no Location header")
        location = _resolve_location(current_url, location)
        status, headers, body = _https_request("GET", location)
        current_url = location
        redirects += 1
        print("APPS_SCRIPT_REDIRECT_STATUS", status)
    if status in (301, 302, 303, 307, 308):
        raise OSError("Too many Apps Script redirects")
    if status < 200 or status >= 300:
        raise OSError("Apps Script GET HTTP " + str(status))
    return json.loads(body.decode("utf-8"))


def _post_rpc_and_get_result(webapp_url, payload):
    # Apps Script's POST response is not reaching this CanMV firmware, even
    # though Executions confirms doPost ran. Use a short-lived request ID and
    # fetch the stored result through GET, which works on this board.
    nonce_result = _get_json_follow_redirects(
        _append_query(webapp_url, "action=nonce")
    )
    request_id = nonce_result.get("request_id", "")
    if not nonce_result.get("ok") or len(request_id) != 32:
        raise OSError("Apps Script did not issue a request ID")
    payload["request_id"] = request_id
    try:
        status, _, _ = _https_request(
            "POST", webapp_url, json.dumps(payload), skip_response_read=True
        )
        print("APPS_SCRIPT_POST_RESPONSE_READ_SKIPPED", status)
    except Exception as exc:
        # The POST may have completed server-side before the response was
        # dropped; polling below determines whether a result was stored.
        print("APPS_SCRIPT_POST_READ_NOTE", str(exc))

    result_url = _append_query(webapp_url, "action=result&id=" + request_id)
    last_result = None
    for attempt in range(12):
        last_result = _get_json_follow_redirects(result_url)
        if not last_result.get("pending"):
            return last_result
        print("APPS_SCRIPT_RESULT_PENDING", attempt + 1)
        time.sleep(1)
    raise OSError("Apps Script result poll timed out: " + str(last_result))


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

    payload = {"action": "ping", "token": config["device_token"]}
    result = _post_rpc_and_get_result(webapp_url, payload)
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
