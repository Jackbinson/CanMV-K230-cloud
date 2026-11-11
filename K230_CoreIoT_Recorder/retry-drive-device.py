"""Temporary upload-only diagnostic. The host supplies updated uploader source in RAM."""
import os
import sys
import time
import ujson as json
import socket
import ssl

sys.path.insert(0, "/sdcard/recorder")
from remote_control import RecorderControl
from wifi_client import ensure_wifi_connected


def _event(stage, **values):
    values["stage"] = stage
    print("K230_DRIVE " + json.dumps(values))


class ProbeControl(RecorderControl):
    def update(self, **values):
        super().update(**values)
        allowed = ("upload_http_host", "upload_http_method", "upload_http_phase", "upload_http_status",
                   "upload_http_error", "upload_http_error_phase", "upload_file", "upload_bytes",
                   "upload_total_bytes", "upload_percent", "upload_http_wait_seconds",
                   "upload_verification_year", "upload_tls_verification",
                   "upload_socket_timeout_seconds", "upload_request_attempt")
        visible = {key: values[key] for key in allowed if key in values}
        if visible:
            _event("http_progress", **visible)


def _sdk_error(error, phase):
    values = {"phase": phase, "error_code": _upload["safe_http_error"](error),
              "error_type": type(error).__name__}
    args = getattr(error, "args", ())
    if args and isinstance(args[0], int):
        values["errno"] = args[0]
    # Extract only a numeric library line from traceback; never print its text.
    try:
        import io
        stream = io.StringIO()
        sys.print_exception(error, stream)
        for line in stream.getvalue().splitlines():
            if 'File "requests/__init__.py"' in line and ", line " in line:
                value = line.split(", line ", 1)[1].split(",", 1)[0].strip()
                if value.isdigit():
                    values["requests_line"] = int(value)
    except Exception:
        pass
    _event("sdk_error", **values)


def _valid_video(path, root):
    prefix = root + "/selftest-"
    if not isinstance(path, str) or not path.startswith(prefix) or ".." in path:
        return None
    rest = path[len(prefix):].split("/")
    if len(rest) != 2 or not rest[0] or any(c not in "0123456789abcdef" for c in rest[0]):
        return None
    if not rest[1].endswith(".mp4") or rest[1].endswith(".partial.mp4"):
        return None
    try:
        with open(path[:-4] + ".json") as stream:
            meta = json.load(stream)
        if meta.get("status") == "finalized" and meta.get("video_file") == rest[1]:
            os.stat(path)
            return meta
    except Exception:
        pass
    return None


def _select_video(preferred, root):
    meta = _valid_video(preferred, root)
    if meta is not None:
        return preferred, meta
    candidates = []
    for directory in os.listdir(root):
        if not directory.startswith("selftest-"):
            continue
        path = root + "/" + directory
        try:
            for name in os.listdir(path):
                video = path + "/" + name
                meta = _valid_video(video, root)
                if meta is not None:
                    candidates.append((os.stat(video)[8], video, meta))
        except OSError:
            pass
    if not candidates:
        raise ValueError("no_finalized_test_clip")
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][1], candidates[0][2]


def _run_retry():
    result = {"ok": False}
    control = ProbeControl()
    try:
        with open('/sdcard/recorder/recording_config.json') as stream:
            recorder_cfg = json.load(stream)
        video, meta = _select_video(PREFERRED_VIDEO, recorder_cfg["output_dir"])
        base = video[:-4]
        _event("selected", local_video=video, video_file=meta["video_file"])
        if not ensure_wifi_connected(timeout=20):
            raise _upload["UploadError"]("wifi_offline")
        with open('/sdcard/recorder/drive-webapp.json') as stream:
            cfg = json.load(stream)
        utc = (HOST_UTC[0], HOST_UTC[1], HOST_UTC[2], HOST_UTC[4], HOST_UTC[5], HOST_UTC[6])
        _https["seed_software_clock"](utc)
        epoch = 946684800 if time.gmtime(0)[0] == 2000 else 0
        _event("clock", clock_source="laptop_software_utc", rtc_year=time.gmtime()[0],
               verification_year=time.gmtime(_https["software_time"]() - epoch)[0])
        client = _https["Http11Client"](ca_pem=TLS_CA_PEM, pins=GOOGLE_PINS)
        http = _upload["FirmwareHttp"](control, exception_observer=_sdk_error, client=client)
        api = _upload["DriveApi"](cfg, http)
        # Isolate basic HTTPS from ContentService/redirect parsing failures.
        try:
            _event("basic_https_start", host="www.google.com")
            status, _, _ = client.request("GET", "https://www.google.com/generate_204")
            _event("basic_https_result", http_status=status)
        except Exception as error:
            _sdk_error(error, "basic_https")
        for attempt in range(1, 3):
            try:
                _event("nonce_probe", attempt=attempt)
                nonce = api.json_request("GET", api.url + "?action=nonce")
                # Reuse the successful nonce immediately for init.
                api.prefetched_nonce = nonce
                _event("retry_existing_video", attempt=attempt)
                journal = _upload["read_journal"](base)
                file_id = _upload["upload_file"](video, meta["camera_id"], api, journal, base, control)
                _upload["upload_file"](base + ".json", meta["camera_id"], api, journal, base, control)
                journal["uploaded"] = True
                _upload["write_journal"](base, journal)
                result.update(ok=True, local_video=video,
                              drive_url="https://drive.google.com/file/d/" + file_id + "/view")
                result.pop("error_code", None)
                break
            except _upload["UploadError"] as error:
                result["error_code"] = str(error)
                _event("attempt_failed", attempt=attempt, error_code=str(error))
                time.sleep_ms(500)
    except _upload["UploadError"] as error:
        result["error_code"] = str(error)
    except Exception as error:
        result.update(error_code="diagnostic_failed", error_type=type(error).__name__)
    finally:
        control.alive = False
        _event("result", **result)


_run_retry()
