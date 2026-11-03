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


def _response_complete(data):
    """True once headers and body have arrived, False if more is expected,
    None if the body only ends when the server closes the connection."""
    header_end = data.find(b"\r\n\r\n")
    if header_end < 0:
        return False
    head = data[:header_end].lower()
    body = data[header_end + 4:]
    if b"transfer-encoding: chunked" in head:
        return body.endswith(b"0\r\n\r\n")
    marker = b"\r\ncontent-length:"
    start = head.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = head.find(b"\r\n", start)
    if end < 0:
        end = len(head)
    return len(body) >= int(head[start:end].strip())


def _read_response(stream, limit=8192, wait_ms=30000):
    """Collect one small HTTP response.

    On CanMV an empty read does not mean the server is done: read() returns
    b'' when the socket timeout passes, on a reset and on a close alike, and
    None when the TLS layer has nothing to hand over yet. Apps Script answers
    only after doGet/doPost has run, so keep reading until the headers say the
    response is complete, for up to wait_ms.
    """
    data = b""
    empty_reads = 0
    start = time.ticks_ms()
    while True:
        chunk = stream.read(512)
        elapsed = time.ticks_diff(time.ticks_ms(), start)
        if chunk:
            if not data:
                print("RAW_HTTPS_FIRST_BYTE_MS", elapsed)
            data += chunk
            if len(data) > limit:
                raise OSError("HTTP response exceeded diagnostic limit")
            if _response_complete(data):
                return data
            continue
        empty_reads += 1
        if empty_reads <= 5:
            print("RAW_HTTPS_EMPTY_READ", repr(chunk), elapsed)
        if data and _response_complete(data) is None:
            return data
        if elapsed > wait_ms:
            print("RAW_HTTPS_READ_GAVE_UP", empty_reads, elapsed)
            return data
        time.sleep_ms(200)


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


def _write_all(stream, data, wait_ms=20000):
    # write() returns None when nothing could be written yet (EAGAIN), not
    # after a full write, so wait and send the rest instead of stopping.
    offset = 0
    start = time.ticks_ms()
    while offset < len(data):
        written = stream.write(data[offset:])
        if written:
            offset += written
            continue
        if time.ticks_diff(time.ticks_ms(), start) > wait_ms:
            raise OSError(
                "HTTPS request write stalled at %d/%d bytes" % (offset, len(data))
            )
        time.sleep_ms(50)


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
        # With the patient _read_response this should now show 302 (Apps
        # Script's redirect). The GET polling below still fetches the result.
        status, _, _ = _https_request("POST", webapp_url, json.dumps(payload))
        print("APPS_SCRIPT_POST_STATUS", status)
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
