"""Upload finalized K230 MP4/JSON files directly to a Drive resumable session.

CanMV MicroPython prototype. It does not delete or modify source recordings.
Run it only after recording has stopped, because network uploads can delay the
next clip if they are added inside the recorder's capture loop.
"""
import os
import sys
import time
import ujson as json
try:
    import requests
except ImportError:
    import urequests as requests
try:
    import usocket as socket
except ImportError:
    import socket

CONFIG_PATH = "/sdcard/recorder/drive_upload_config.json"
STATE_PATH = "/sdcard/recorder/drive_upload_state.json"
MAX_FILE_BYTES = 256 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 256 * 1024


def _read_json(path, default=None):
    try:
        with open(path, "r") as stream:
            return json.load(stream)
    except Exception:
        return default


def _write_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(value, stream)
        if hasattr(stream, "flush"):
            stream.flush()
    try:
        os.rename(temporary, path)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(temporary, path)


def _file_size(path):
    return os.stat(path)[6]


def _is_directory(path):
    try:
        os.listdir(path)
        return True
    except OSError:
        return False


def _header(headers, name):
    wanted = name.lower()
    for key in headers:
        if str(key).lower() == wanted:
            return str(headers[key])
    return ""


def _close(response):
    try:
        response.close()
    except Exception:
        pass


def _append_query(url, query):
    return url + ("&" if "?" in url else "?") + query


def _response_complete(data):
    """Return True when HTTP framing says the whole response has arrived."""
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


def _read_http_response(stream, limit=16384, wait_ms=30000):
    # CanMV may return None or b'' before the HTTP response arrives. An empty
    # read is not treated as end-of-response when headers declare a body size.
    data = b""
    empty_reads = 0
    started = time.ticks_ms()
    while True:
        chunk = stream.read(512)
        elapsed = time.ticks_diff(time.ticks_ms(), started)
        if chunk:
            if not data:
                print("UPLOAD_HTTP_FIRST_BYTE_MS", elapsed)
            data += chunk
            if len(data) > limit:
                raise OSError("HTTP response exceeded upload control limit")
            if _response_complete(data):
                return data
            continue
        empty_reads += 1
        if empty_reads <= 5:
            print("UPLOAD_HTTP_EMPTY_READ", repr(chunk), elapsed)
        if data and _response_complete(data) is None:
            return data
        if elapsed >= wait_ms:
            print("UPLOAD_HTTP_READ_TIMEOUT_MS", elapsed)
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
    offset = 0
    started = time.ticks_ms()
    while offset < len(data):
        written = stream.write(data[offset:])
        if written:
            offset += written
            continue
        if time.ticks_diff(time.ticks_ms(), started) >= wait_ms:
            raise OSError("HTTPS request write stalled at %d/%d bytes" % (offset, len(data)))
        time.sleep_ms(50)


def _https_request(method, url, body=None):
    if not url.startswith("https://"):
        raise ValueError("Only HTTPS endpoint URLs are accepted")
    rest = url[8:]
    slash = rest.find("/")
    host, path = (rest, "/") if slash < 0 else (rest[:slash], rest[slash:])
    if not host or not path.startswith("/"):
        raise ValueError("Invalid HTTPS URL")

    print("UPLOAD_HTTPS_REQUEST", method, host)
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
        stream = ssl.wrap_socket(raw, server_hostname=host, cert_reqs=ssl.CERT_NONE)

        body_bytes = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        request = (
            method.encode("ascii") + b" " + path.encode("utf-8") + b" HTTP/1.1\r\n" +
            b"Host: " + host.encode("ascii") + b"\r\n" +
            b"Connection: close\r\n" +
            b"Accept: application/json\r\n" +
            b"User-Agent: CanMV-K230-Drive-Upload\r\n"
        )
        if body is not None:
            request += (
                b"Content-Type: application/json\r\n" +
                b"Content-Length: " + str(len(body_bytes)).encode("ascii") + b"\r\n"
            )
        request += b"\r\n"
        if body is not None:
            request += body_bytes
        _write_all(stream, request)
        try:
            stream.flush()
        except Exception:
            pass
        print("UPLOAD_HTTPS_SENT_BYTES", len(request))

        response = _read_http_response(stream)
        header_end = response.find(b"\r\n\r\n")
        status_line = response.split(b"\r\n", 1)[0]
        parts = status_line.split(None, 2)
        if len(parts) < 2:
            raise OSError("HTTP bad status line: " + repr(status_line))
        status = int(parts[1])
        if header_end < 0:
            raise OSError("HTTP response headers incomplete")
        headers = {}
        for line in response[:header_end].split(b"\r\n")[1:]:
            if b":" in line:
                key, value = line.split(b":", 1)
                headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
        response_body = response[header_end + 4:]
        if "chunked" in _header(headers, "Transfer-Encoding").lower():
            response_body = _decode_chunked(response_body)
        else:
            length = _header(headers, "Content-Length")
            if length:
                response_body = response_body[:int(length)]
        print("UPLOAD_HTTPS_STATUS", status)
        return status, headers, response_body
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


def _get_webapp_json(url):
    current_url = url
    for _ in range(4):
        status, headers, body = _https_request("GET", current_url)
        if status in (301, 302, 303, 307, 308):
            location = _header(headers, "Location")
            if not location:
                raise OSError("Apps Script redirect had no Location header")
            current_url = _resolve_location(current_url, location)
            continue
        if status < 200 or status >= 300:
            raise OSError("Apps Script HTTP " + str(status) + ": " + repr(body[:160]))
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            raise OSError("Apps Script returned invalid JSON: " + repr(body[:120]))
    raise OSError("Too many Apps Script redirects")


def _post_webapp(url, payload):
    # Fetching an Apps Script TextOutput after POST fails on this CanMV build:
    # doPost executes, but its HTTP response is empty. Obtain the result using
    # GET with a short-lived ID so no device token appears in the URL.
    nonce_result = _get_webapp_json(_append_query(url, "action=nonce"))
    request_id = nonce_result.get("request_id", "")
    if not nonce_result.get("ok") or len(request_id) != 32:
        raise OSError("Apps Script did not issue a request ID")
    payload["request_id"] = request_id

    try:
        status, _, _ = _https_request("POST", url, json.dumps(payload))
        print("APPS_SCRIPT_POST_STATUS", status)
    except Exception as exc:
        # Continue to poll: Apps Script may have completed the POST before its
        # response was dropped by the board's HTTP/TLS client.
        print("APPS_SCRIPT_POST_READ_NOTE", str(exc))

    result_url = _append_query(url, "action=result&id=" + request_id)
    last_result = None
    for attempt in range(12):
        last_result = _get_webapp_json(result_url)
        if not last_result.get("pending"):
            if not last_result.get("ok"):
                raise OSError("Apps Script error: " + str(last_result.get("error", "unknown")))
            return last_result
        print("APPS_SCRIPT_RESULT_PENDING", attempt + 1)
        time.sleep(1)
    raise OSError("Apps Script result poll timed out: " + str(last_result))


def _read_drive_offset(upload_url, total):
    response = requests.put(upload_url, data=b"", headers={
        "Content-Length": "0",
        "Content-Range": "bytes */" + str(total)
    })
    status = response.status_code
    range_header = _header(response.headers, "Range")
    text = _response_text(response)
    if status in (200, 201):
        try:
            result = json.loads(text)
        except Exception:
            result = {}
        _close(response)
        return total, result.get("id")
    _close(response)
    if status != 308:
        raise OSError("Drive session query HTTP " + str(status))
    if not range_header:
        return 0, None
    try:
        last_byte = int(range_header.rsplit("-", 1)[1])
    except Exception:
        raise OSError("Unrecognized Drive Range header")
    return last_byte + 1, None


def _upload_resumable(path, upload_url, total, chunk_bytes):
    offset = 0
    completed_id = None
    retries = 0
    with open(path, "rb") as stream:
        while offset < total:
            stream.seek(offset)
            data = stream.read(min(chunk_bytes, total - offset))
            if not data:
                raise OSError("Unexpected end of file while uploading")
            end = offset + len(data) - 1
            response = None
            try:
                response = requests.put(upload_url, data=data, headers={
                    "Content-Length": str(len(data)),
                    "Content-Range": "bytes " + str(offset) + "-" + str(end) + "/" + str(total),
                    "Content-Type": "application/octet-stream"
                })
                status = response.status_code
                range_header = _header(response.headers, "Range")
                text = _response_text(response)
                _close(response)
                if status in (200, 201):
                    try:
                        completed_id = json.loads(text).get("id")
                    except Exception:
                        completed_id = None
                    offset = total
                elif status == 308:
                    if range_header:
                        offset = int(range_header.rsplit("-", 1)[1]) + 1
                    else:
                        offset = 0
                else:
                    raise OSError("Drive upload HTTP " + str(status) + ": " + text[:160])
                retries = 0
            except Exception:
                if response is not None:
                    _close(response)
                retries += 1
                if retries > 5:
                    raise
                time.sleep(2 * retries)
                # Ask Drive how much it committed before retrying. This avoids
                # sending duplicate bytes after a dropped 4G connection.
                offset, completed_id = _read_drive_offset(upload_url, total)
                if completed_id:
                    break
    if offset != total:
        raise OSError("Drive upload stopped before all bytes were accepted")
    return completed_id


def upload_file(path, cfg, camera_id):
    size = _file_size(path)
    if size < 1 or size > min(int(cfg.get("max_file_bytes", MAX_FILE_BYTES)), MAX_FILE_BYTES):
        raise ValueError("File size is empty or exceeds configured limit: " + path)
    name = path.rsplit("/", 1)[-1]
    result = _post_webapp(cfg["webapp_url"], {
        "action": "init",
        "token": cfg["device_token"],
        "camera_id": camera_id,
        "name": name,
        "size": size
    })
    chunk = int(cfg.get("chunk_bytes", DEFAULT_CHUNK_BYTES))
    if chunk < DEFAULT_CHUNK_BYTES or chunk % DEFAULT_CHUNK_BYTES:
        raise ValueError("chunk_bytes must be a multiple of 256 KiB")
    file_id = _upload_resumable(path, result["upload_url"], size, chunk)
    return {"name": name, "size": size, "drive_file_id": file_id}


def upload_finalized_recordings():
    cfg = _read_json(CONFIG_PATH)
    if not cfg:
        raise OSError("Missing config: " + CONFIG_PATH)
    if not cfg.get("webapp_url", "").startswith("https://"):
        raise ValueError("Set webapp_url to the HTTPS Apps Script or relay endpoint")
    token = cfg.get("device_token", "")
    if len(token) < 32 or "PASTE_" in token:
        raise ValueError("Set device_token to your own random secret")

    # The recorder and ping script use the saved SD-card credentials, but this
    # uploader runs as a separate IDE script and must ensure STA is active too.
    recorder_path = "/sdcard/recorder"
    if recorder_path not in sys.path:
        sys.path.insert(0, recorder_path)
    try:
        from wifi_client import ensure_wifi_connected
    except Exception as exc:
        raise OSError("Wi-Fi helper unavailable: " + str(exc))
    if not ensure_wifi_connected(timeout=20):
        raise OSError("Wi-Fi has no active connection")

    directory = cfg.get("recordings_dir", "/data/recordings")
    camera_id = cfg.get("camera_id", "CAM01")
    if not _is_directory(directory):
        raise OSError("Recording directory not found: " + directory)

    state = _read_json(STATE_PATH, {}) or {}
    uploaded = state.get("uploaded", {})
    names = sorted(os.listdir(directory))
    name_set = set(names)
    for name in names:
        # Upload only complete, paired clips whose recorder sidecar confirms
        # finalization. Partial clips and unrelated JSON files are excluded.
        if not (name.endswith(".mp4") or name.endswith(".json")):
            continue
        if name.endswith(".partial.mp4") or name == "last_error.txt":
            continue
        stem = name.rsplit(".", 1)[0]
        mp4_name = stem + ".mp4"
        json_name = stem + ".json"
        if mp4_name not in name_set or json_name not in name_set:
            continue
        sidecar = _read_json(directory + "/" + json_name)
        if not sidecar or sidecar.get("status") != "finalized":
            continue
        path = directory + "/" + name
        if name in uploaded:
            continue
        result = upload_file(path, cfg, camera_id)
        uploaded[name] = result
        state["uploaded"] = uploaded
        _write_json(STATE_PATH, state)
        print("DRIVE UPLOADED", result["name"], "bytes", result["size"])


if __name__ == "__main__":
    upload_finalized_recordings()
