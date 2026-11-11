"""Resume finalized MP4/JSON uploads, retaining every local recording.

The network thread only reads finalized clips. Two journal slots preserve the
last confirmed session/offset across power loss. Temporary upload URLs never
enter telemetry, logs, or recording metadata.
"""
import os
import time
import gc
import ujson as json
import ubinascii

CHUNK_BYTES = 256 * 1024
MAX_BYTES = 256 * 1024 * 1024
CONFIG_PATH = "/sdcard/recorder/drive-webapp.json"


class UploadError(Exception):
    pass


class UploadPaused(Exception):
    pass


def safe_http_error(error):
    """Keep errno/category only; library exception strings can contain URLs."""
    text = str(error).lower()
    if (type(error).__name__ == "HttpError" and text.startswith("https_") and len(text) <= 64 and
            all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in text)):
        return text
    if "transfer-encoding" in text or "chunked" in text:
        return "http_chunked_response_unsupported"
    if "certificate" in text or "cert_verify" in text:
        return "http_certificate_error"
    if "badstatusline" in text:
        return "http_bad_status_line"
    if isinstance(error, OSError):
        args = getattr(error, "args", ())
        if args and isinstance(args[0], int):
            return "http_oserror_%d" % args[0]
        return "http_oserror"
    if isinstance(error, TypeError):
        if "timeout" in text and ("keyword" in text or "argument" in text):
            return "sdk_http_timeout_unsupported"
        return "http_sdk_type_error"
    if isinstance(error, ValueError):
        return "http_sdk_value_error"
    if isinstance(error, MemoryError):
        return "http_memory_error"
    return "http_sdk_error"


def host_of(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UploadError("https_required")
    authority = url[8:].split("/", 1)[0].split("?", 1)[0]
    if "@" in authority or ":" in authority:
        raise UploadError("invalid_host")
    return authority.lower()


def validate_upload_url(url):
    if host_of(url) not in ("www.googleapis.com", "content.googleapis.com"):
        raise UploadError("invalid_upload_host")
    if "/upload/drive/" not in url or len(url) > 8192:
        raise UploadError("invalid_upload_url")
    return url


def header(headers, key):
    for name in headers:
        if str(name).lower() == key.lower():
            return str(headers[name])
    return ""


class FirmwareHttp:
    def __init__(self, control, exception_observer=None, client=None):
        if client is None:
            from recorder_https import Http11Client
            try:
                with open('/sdcard/recorder/google-pins.json') as stream:
                    pins = json.load(stream)
            except (OSError, ValueError):
                raise UploadError("https_pins_missing")
            client = Http11Client(pins=pins)
        self.client = client
        self.control = control
        self.exception_observer = exception_observer
        self.phase = "request"
        def progress(phase, host, **values):
            self.phase = phase
            public = {"upload_http_phase": phase, "upload_http_host": host}
            if "http_status" in values:
                public["upload_http_status"] = values["http_status"]
            if "wait_seconds" in values:
                public["upload_http_wait_seconds"] = values["wait_seconds"]
            if "verification_year" in values:
                public["upload_verification_year"] = values["verification_year"]
            if "timeout_seconds" in values:
                public["upload_socket_timeout_seconds"] = values["timeout_seconds"]
            if "request_attempt" in values:
                public["upload_request_attempt"] = values["request_attempt"]
            if phase == "certificate_pin_verified":
                public["upload_tls_verification"] = "pinned_certificate"
            control.update(**public)
        self.client.observe = progress

    def __call__(self, method, url, body=None, headers=None):
        host = host_of(url)
        if host not in ("script.google.com", "script.googleusercontent.com",
                        "www.googleapis.com", "content.googleapis.com"):
            raise UploadError("http_host_rejected")
        if not self.control.begin_upload_io():
            raise UploadPaused()
        self.phase = "request"
        self.control.update(upload_http_host=host, upload_http_method=method,
                            upload_http_phase=self.phase)
        try:
            status, response_headers, data = self.client.request(method, url, body, headers)
            if len(data) > 65536:
                raise UploadError("response_too_large")
            return status, response_headers, data
        except UploadError:
            raise
        except Exception as error:
            code = safe_http_error(error)
            self.control.update(upload_http_error=code, upload_http_error_phase=self.phase)
            if self.exception_observer is not None:
                try:
                    self.exception_observer(error, self.phase)
                except Exception:
                    pass
            raise UploadError(code)
        finally:
            self.control.end_upload_io()


class DriveApi:
    def __init__(self, cfg, http):
        self.url = cfg["web_app_url"]
        self.token = cfg["device_token"]
        if host_of(self.url) != "script.google.com" or not self.url.endswith("/exec"):
            raise UploadError("web_app_url_invalid")
        if not isinstance(self.token, str) or len(self.token) < 32:
            raise UploadError("drive_token_invalid")
        self.http = http
        self.prefetched_nonce = None

    def json_request(self, method, url, payload=None):
        url += ("&" if "?" in url else "?") + "_cb=" + ubinascii.hexlify(os.urandom(8)).decode()
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json", "Cache-Control": "no-cache"}
        # Standard frozen requests follows ContentService redirects. Handle a
        # firmware that returns redirects explicitly as well.
        for _ in range(5):
            code, response_headers, data = self.http(method, url, body, headers)
            if code in (301, 302, 303):
                next_url = header(response_headers, "Location")
                if host_of(next_url) not in ("script.google.com", "script.googleusercontent.com"):
                    raise UploadError("script_redirect_rejected")
                url, method, body = next_url, "GET", None
                continue
            if code != 200:
                raise UploadError("script_http_%d" % code)
            try:
                result = json.loads(data)
            except Exception:
                raise UploadError("script_non_json")
            if not isinstance(result, dict):
                raise UploadError("script_invalid_result")
            return result
        raise UploadError("script_redirect_limit")

    def operation(self, action, **values):
        nonce = self.prefetched_nonce
        self.prefetched_nonce = None
        if nonce is None:
            nonce = self.json_request("GET", self.url + "?action=nonce")
        request_id = nonce.get("request_id", "")
        if (not nonce.get("ok") or not isinstance(request_id, str) or len(request_id) != 32 or
                any(c not in "0123456789abcdef" for c in request_id)):
            raise UploadError("script_nonce_invalid")
        payload = {"action": action, "token": self.token, "request_id": request_id}
        payload.update(values)
        posted = {}
        try:
            posted = self.json_request("POST", self.url, payload)
        except UploadError:
            # The server may have processed the POST despite a lost redirect.
            pass
        if "upload_url" in posted or "service" in posted or "error" in posted:
            result = posted
        else:
            result = {"pending": True}
            for _ in range(5):
                result = self.json_request("GET", self.url + "?action=result&id=" + request_id)
                if not result.get("pending"):
                    break
                time.sleep_ms(500)
        if not result.get("ok"):
            code = result.get("error")
            # Report a fixed whitelist, never arbitrary remote content/URLs.
            allowed = ("unauthorized", "drive_session_failed", "invalid_camera_id", "invalid_file_size", "server_error")
            raise UploadError(code if code in allowed else "script_result_unavailable")
        return result


def read_journal(base):
    candidates = []
    for slot in (0, 1):
        try:
            with open(base + ".drive.%d.json" % slot) as stream:
                value = json.load(stream)
            if isinstance(value, dict) and isinstance(value.get("seq"), int):
                candidates.append(value)
        except (OSError, ValueError):
            pass
    return max(candidates, key=lambda item: item["seq"]) if candidates else {"seq": 0, "files": {}}


def write_journal(base, journal):
    journal["seq"] = journal.get("seq", 0) + 1
    path = base + ".drive.%d.json" % (journal["seq"] % 2)
    # Only overwrite the older slot. A power loss leaves the other slot intact.
    with open(path, "w") as stream:
        json.dump(journal, stream)
        stream.flush()
    if hasattr(os, "sync"):
        os.sync()


def apply_progress(code, headers, body, job, maximum):
    total = job["size"]
    if code in (200, 201):
        try:
            result = json.loads(body)
            file_id = result["id"]
            valid = (isinstance(file_id, str) and 1 <= len(file_id) <= 256 and
                     all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in file_id))
            if not valid:
                raise ValueError()
        except Exception:
            raise UploadError("drive_final_response_invalid")
        if maximum < total:
            raise UploadError("drive_premature_completion")
        job.update(file_id=file_id, offset=total, done=True)
        return
    if code in (404, 410):
        job.pop("upload_url", None)
        job.update(offset=0, done=False)
        raise UploadError("drive_session_expired")
    if code != 308:
        raise UploadError("drive_http_%d" % code)
    value = header(headers, "Range")
    if value:
        if not value.startswith("bytes=0-") or not value[8:].isdigit():
            raise UploadError("drive_range_invalid")
        offset = int(value[8:]) + 1
    else:
        offset = 0
    if offset < 0 or offset > maximum or offset >= total:
        raise UploadError("drive_range_out_of_bounds")
    job["offset"] = offset
    job["done"] = False


def upload_file(path, camera_id, api, journal, base, control):
    name = path.rsplit("/", 1)[1]
    size = os.stat(path)[6]
    if not 0 < size <= MAX_BYTES:
        raise UploadError("file_size_out_of_range")
    files = journal.setdefault("files", {})
    job = files.setdefault(name, {"size": size, "offset": 0, "done": False})
    if job.get("size") != size:
        raise UploadError("finalized_file_changed")
    if job.get("done"):
        return job["file_id"]
    if control.snapshot()["session_active"] or not control.alive:
        raise UploadPaused()
    if "upload_url" not in job:
        response = api.operation("init", name=name, size=size, camera_id=camera_id)
        job["upload_url"] = validate_upload_url(response.get("upload_url"))
        job["offset"] = 0
        write_journal(base, journal)
    url = validate_upload_url(job["upload_url"])
    # Probe on every restart/retry; the last successful PUT response may be lost.
    code, headers, body = api.http("PUT", url, b"", {"Content-Length": "0", "Content-Range": "bytes */%d" % size})
    try:
        apply_progress(code, headers, body, job, size)
    finally:
        write_journal(base, journal)
    control.update(upload_file=name, upload_bytes=job["offset"], upload_total_bytes=size,
                   upload_percent=int(job["offset"] * 100 / size))
    while not job.get("done"):
        if control.snapshot()["session_active"] or not control.alive:
            raise UploadPaused()
        offset = job["offset"]
        with open(path, "rb") as stream:
            stream.seek(offset)
            chunk = stream.read(min(CHUNK_BYTES, size - offset))
        if not chunk:
            raise UploadError("local_file_short_read")
        end = offset + len(chunk)
        mime = "video/mp4" if name.endswith(".mp4") else "application/json"
        code, headers, body = api.http("PUT", url, chunk, {
            "Content-Type": mime, "Content-Length": str(len(chunk)),
            "Content-Range": "bytes %d-%d/%d" % (offset, end - 1, size)})
        del chunk
        try:
            apply_progress(code, headers, body, job, end)
        finally:
            write_journal(base, journal)
        control.update(upload_file=name, upload_bytes=job["offset"], upload_total_bytes=size,
                       upload_percent=int(job["offset"] * 100 / size))
        if not job.get("done") and job["offset"] <= offset:
            raise UploadError("drive_no_progress")
        gc.collect()
    return job["file_id"]


def pending_clips(cfg):
    directory = cfg["output_dir"]
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".mp4") or name.endswith(".partial.mp4"):
            continue
        base = directory + "/" + name[:-4]
        try:
            with open(base + ".json") as stream:
                metadata = json.load(stream)
        except (OSError, ValueError):
            continue
        if (metadata.get("status") == "finalized" and metadata.get("video_file") == name and
                metadata.get("camera_id") == cfg["camera_id"]):
            journal = read_journal(base)
            if not journal.get("uploaded"):
                yield base, metadata, journal


def upload_loop(control, cfg):
    delay = 5
    api = None
    while control.alive:
        try:
            if control.snapshot()["session_active"]:
                time.sleep_ms(200)
                continue
            import network
            if not network.WLAN(network.STA_IF).isconnected():
                raise UploadError("wifi_offline")
            if api is None:
                with open(CONFIG_PATH) as stream:
                    upload_cfg = json.load(stream)
                api = DriveApi(upload_cfg, FirmwareHttp(control))
            found = False
            for base, metadata, journal in pending_clips(cfg):
                found = True
                control.update(upload_state="uploading", upload_error="")
                video_id = upload_file(base + ".mp4", cfg["camera_id"], api, journal, base, control)
                control.update(last_drive_file_id=video_id,
                               last_drive_url="https://drive.google.com/file/d/" + video_id + "/view")
                upload_file(base + ".json", cfg["camera_id"], api, journal, base, control)
                journal["uploaded"] = True
                write_journal(base, journal)
                control.update(upload_state="uploaded", last_uploaded_video=metadata["video_file"], upload_error="")
            if not found:
                control.update(upload_state="idle", upload_error="")
            delay = 5
        except UploadPaused:
            control.update(upload_state="paused_for_recording")
        except UploadError as error:
            control.update(upload_state="retry_wait", upload_error=str(error))
            delay = min(delay * 2, 300)
        except Exception:
            control.update(upload_state="retry_wait", upload_error="upload_worker_error")
            delay = min(delay * 2, 300)
        for _ in range(delay * 10):
            if not control.alive or control.take_upload_request():
                break
            time.sleep_ms(100)
