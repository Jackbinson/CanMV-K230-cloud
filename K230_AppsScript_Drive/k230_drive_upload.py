"""Upload finalized K230 MP4/JSON files directly to a Drive resumable session.

CanMV MicroPython prototype. It does not delete or modify source recordings.
Run it only after recording has stopped, because network uploads can delay the
next clip if they are added inside the recorder's capture loop.
"""
import os
import time
import ujson as json
try:
    import requests
except ImportError:
    import urequests as requests

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


def _response_text(response):
    try:
        return response.text
    except Exception:
        return ""


def _append_query(url, query):
    return url + ("&" if "?" in url else "?") + query


def _follow_get_redirects(response, base_url, max_redirects=3):
    redirects = 0
    current_url = base_url
    while response.status_code in (301, 302, 303, 307, 308) and redirects < max_redirects:
        location = _header(response.headers, "Location")
        _close(response)
        if not location:
            raise OSError("Apps Script redirect had no Location header")
        if location.startswith("/"):
            host_start = current_url.find("//") + 2
            host_end = current_url.find("/", host_start)
            host = current_url[host_start:] if host_end < 0 else current_url[host_start:host_end]
            location = "https://" + host + location
        elif location.startswith("//"):
            location = "https:" + location
        response = requests.get(location)
        current_url = location
        redirects += 1
    if response.status_code in (301, 302, 303, 307, 308):
        _close(response)
        raise OSError("Too many Apps Script redirects")
    return response


def _get_webapp_json(url):
    response = _follow_get_redirects(requests.get(url), url)
    status = response.status_code
    text = _response_text(response)
    _close(response)
    if status < 200 or status >= 300:
        raise OSError("Apps Script HTTP " + str(status) + ": " + text[:160])
    try:
        result = json.loads(text)
    except Exception:
        raise OSError("Apps Script returned invalid JSON")
    return result


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
        response = requests.post(
            url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"}
        )
        _close(response)
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
    if not cfg.get("webapp_url", "").startswith("https://script.google.com/"):
        raise ValueError("Set webapp_url to your deployed Apps Script /exec URL")
    token = cfg.get("device_token", "")
    if len(token) < 32 or "PASTE_" in token:
        raise ValueError("Set device_token to your own random secret")
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
