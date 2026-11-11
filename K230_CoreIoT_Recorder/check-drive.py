#!/usr/bin/env python3
"""Check Apps Script authentication without creating a Drive file."""
import argparse
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = Path(__file__).with_name("drive-webapp.json")
TRANSPORT = "urllib"
TIMEOUT = 20
CURL_MODES = {
    "curl": ["--ipv4"],
    "curl-auto": [],
    "curl-http1": ["--http1.1"],
    "curl4-http1": ["--ipv4", "--http1.1"],
}


class DiagnosticError(Exception):
    """Only controlled messages belong here; never include bodies or URLs."""


def network_diagnostic(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return "DNS_ERROR: May khong phan giai duoc ten mien Google. Kiem tra mang/DNS."
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS_CERT_ERROR: Khong xac minh duoc chung chi HTTPS. Kiem tra ngay gio va CA certificates; khong tat xac minh SSL."
    if isinstance(reason, ssl.SSLError):
        return "TLS_ERROR: Ket noi HTTPS that bai. Kiem tra ngay gio, mang hoac proxy."
    if isinstance(reason, TimeoutError):
        return "TIMEOUT: Yeu cau vuot thoi gian cho. Chua ket luan token sai."
    if isinstance(reason, ConnectionRefusedError):
        return "CONNECTION_REFUSED: Ket noi bi tu choi. Kiem tra mang hoac proxy."
    return "NETWORK_ERROR: Khong ket noi duoc Google. Kiem tra mang, VPN hoac proxy."


def http_diagnostic(status):
    if status in (401, 403):
        hint = "Kiem tra quyen truy cap Web App va viec uy quyen tai khoan trien khai."
    elif status in (404, 410):
        hint = "Kiem tra Web App URL /exec; deployment co the da bi xoa hoac URL sai."
    elif status == 429:
        hint = "Google dang gioi han yeu cau; doi roi thu lai."
    elif 500 <= status < 600:
        hint = "Loi may chu hoac proxy; thu lai va kiem tra Apps Script Executions."
    else:
        hint = "Kiem tra deployment Web App va Apps Script Executions."
    return "HTTP_%d: %s" % (status, hint)


def curl_request(target, data):
    # Credentials are sent through stdin, never argv or a temporary file.
    # --disable ignores ~/.curlrc (which might enable verbose/body tracing).
    args = ["curl", "--disable", "--silent", "--show-error", "--location"]
    args.extend(CURL_MODES.get(TRANSPORT, []))
    args.extend(["--proto", "=https", "--proto-redir", "=https",
            "--max-redirs", "5", "--connect-timeout", "10",
            "--max-time", str(TIMEOUT), "--max-filesize", "65536",
            "--header", "Content-Type: application/json",
            "--write-out", "\n%{http_code}\n%{url_effective}"])
    if data is not None:
        args.extend(["--data-binary", "@-"])
    args.extend(["--url", target])
    try:
        response = subprocess.run(args, input=data, capture_output=True, timeout=TIMEOUT + 5)
    except FileNotFoundError:
        raise DiagnosticError("CURL_MISSING: Khong tim thay curl tren laptop.") from None
    except subprocess.TimeoutExpired:
        raise DiagnosticError("TIMEOUT: curl vuot thoi gian cho; chua ket luan token sai.") from None
    except OSError:
        raise DiagnosticError("CURL_START_ERROR: Khong chay duoc curl tren laptop.") from None
    if response.returncode:
        messages = {
            5: "PROXY_DNS_ERROR: Khong phan giai duoc proxy.",
            6: "DNS_ERROR: Khong phan giai duoc ten mien Google.",
            7: "CONNECTION_ERROR: Khong ket noi duoc Google hoac proxy.",
            28: "TIMEOUT: curl vuot thoi gian cho; chua ket luan token sai.",
            35: "TLS_ERROR: Ket noi HTTPS that bai.",
            47: "REDIRECT_ERROR: Qua nhieu chuyen huong; kiem tra quyen truy cap Web App.",
            60: "TLS_CERT_ERROR: Khong xac minh duoc chung chi HTTPS; khong tat SSL verification.",
            63: "RESPONSE_TOO_LARGE: Phan hoi vuot gioi han JSON cua Code.gs.",
        }
        raise DiagnosticError(messages.get(response.returncode, "CURL_ERROR_%d: Ket noi that bai." % response.returncode))
    try:
        raw, status, effective_url = response.stdout.rsplit(b"\n", 2)
        status = int(status)
        final_host = urllib.parse.urlsplit(effective_url.decode("utf-8")).hostname
    except (ValueError, UnicodeError):
        raise DiagnosticError("CURL_RESPONSE_ERROR: Khong doc duoc trang thai HTTP.") from None
    if not 200 <= status < 300:
        raise DiagnosticError(http_diagnostic(status))
    return raw, final_host


def urllib_request(target, data):
    req = urllib.request.Request(target, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            final_host = urllib.parse.urlsplit(response.geturl()).hostname
            raw = response.read(65537)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise DiagnosticError(http_diagnostic(status)) from None
    except (urllib.error.URLError, OSError) as error:
        raise DiagnosticError(network_diagnostic(error)) from None
    return raw, final_host


def request_json(target, body=None):
    # Each ContentService request gets a fresh redirect URL. Avoid reusing a
    # cached redirect to a one-time script.googleusercontent.com response.
    target += ("&" if "?" in target else "?") + "_cb=" + secrets.token_hex(8)
    data = json.dumps(body).encode() if body is not None else None
    raw, final_host = curl_request(target, data) if TRANSPORT in CURL_MODES else urllib_request(target, data)
    if final_host == "accounts.google.com":
        raise DiagnosticError("LOGIN_REQUIRED: Web App yeu cau dang nhap Google; camera khong the dang nhap tuong tac.")
    if len(raw) > 65536:
        raise DiagnosticError("RESPONSE_TOO_LARGE: Google khong tra ve phan hoi JSON ngan nhu Code.gs yeu cau.")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        if b"servicelogin" in raw.lower() or b"accounts.google.com/signin" in raw.lower():
            raise DiagnosticError("LOGIN_REQUIRED: Nhan trang dang nhap thay vi JSON; kiem tra quyen truy cap Web App.") from None
        if b"script function not found" in raw.lower():
            raise DiagnosticError("SCRIPT_FUNCTION_MISSING: Ban /exec chua co doGet/doPost can thiet. Kiem tra Code.gs va phien ban deployment.") from None
        raise DiagnosticError("NON_JSON_RESPONSE: Nhan HTML/van ban thay vi JSON. Kiem tra Web App URL, quyen truy cap va phien ban Code.gs da deploy.") from None
    if not isinstance(result, dict):
        raise DiagnosticError("INVALID_JSON_RESPONSE: Endpoint tra JSON khong dung giao thuc cua Code.gs.")
    return result


def get_nonce(url):
    for attempt in range(1, 4):
        print("[2/3] GET nonce lan %d/3..." % attempt, flush=True)
        try:
            nonce = request_json(url + "?action=nonce")
        except DiagnosticError as error:
            if attempt == 3 or not str(error).startswith(("TIMEOUT:", "NETWORK_ERROR:", "CONNECTION_")):
                raise
            print("GET nonce: %s Se thu lai voi yeu cau moi." % error, flush=True)
            time.sleep(1)
            continue
        request_id = nonce.get("request_id", "")
        if (not nonce.get("ok") or not isinstance(request_id, str) or
                not re.fullmatch(r"[a-f0-9]{32}", request_id)):
            raise DiagnosticError("NONCE_MISSING: Endpoint tra JSON nhung khong co nonce hop le. Kiem tra Code.gs va phien ban da deploy.")
        return request_id


def check(request_id=None):
    print("Checking Apps Script; no video or Drive file will be created.", flush=True)
    print("Transport: %s; timeout moi yeu cau: %ds." % (TRANSPORT, TIMEOUT), flush=True)
    print("[1/3] Doc cau hinh upload tren laptop...", flush=True)
    cfg = json.loads(CONFIG.read_text())
    url = cfg["web_app_url"]
    token = cfg["device_token"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "script.google.com" or not parsed.path.endswith("/exec"):
        raise DiagnosticError("CONFIG_URL_ERROR: web_app_url phai la dia chi HTTPS script.google.com ket thuc /exec.")
    if not isinstance(token, str) or len(token) < 32:
        raise DiagnosticError("CONFIG_TOKEN_ERROR: Token upload thieu hoac ngan hon 32 ky tu.")
    print("[1/3] OK: Cau hinh hop le; chua xac nhan token khop Apps Script.", flush=True)
    if request_id is None:
        request_id = get_nonce(url)
        print("[2/3] OK: Nhan nonce dung giao thuc.", flush=True)
    elif isinstance(request_id, str) and re.fullmatch(r"[a-f0-9]{32}", request_id):
        print("[2/3] OK: Dung ngay nonce thanh cong tu phep do mang; khong goi lai GET nonce.", flush=True)
    else:
        raise DiagnosticError("NONCE_INVALID: Nonce truyen vao khong hop le.")
    print("[3/3] POST ping (khong tao file Drive)...", flush=True)
    # A POST response/redirect may be lost after the server cached the result.
    post_result = {}
    try:
        post_result = request_json(url, {"action": "ping", "token": token, "request_id": request_id})
    except DiagnosticError as error:
        print("POST chua xac nhan; se kiem tra ket qua da luu. " + str(error), flush=True)
    if post_result.get("queued") is True:
        print("[3/3] POST OK: Apps Script da ghi nhan yeu cau; dang doc ket qua.", flush=True)
    # Some compatible deployments return the actual authenticated ping result.
    # Do not discard it and wait for a cache entry that may not exist.
    result = post_result if "service" in post_result or "error" in post_result else {}
    if not result:
        for attempt in range(1, 4):
            print("[3/3] GET result lan %d/3..." % attempt, flush=True)
            try:
                result = request_json(url + "?action=result&id=" + request_id)
            except DiagnosticError as error:
                print("GET result lan %d/3: %s" % (attempt, error), flush=True)
                # Retry only transient failures; permission/protocol errors need fixing.
                transient = str(error).startswith(("TIMEOUT:", "NETWORK_ERROR:", "CONNECTION_", "HTTP_429:", "HTTP_500:", "HTTP_502:", "HTTP_503:", "HTTP_504:"))
                if not transient:
                    raise
                if attempt == 3:
                    raise DiagnosticError("RESULT_UNAVAILABLE: Chua doc duoc ket qua sau 3 lan. Chua ket luan token sai. Mo Apps Script → Executions, kiem tra doPost lan vua chay la Completed hay Failed.") from None
            else:
                if not result.get("pending"):
                    break
                print("[3/3] Ket qua chua san sang; se thu lai.", flush=True)
            if attempt < 3:
                time.sleep(1)
    if result.get("ok") and result.get("service") == "k230-drive-upload":
        print("OK: Apps Script DEVICE_TOKEN matches this laptop's upload config.")
        print("Authentication only verified; Drive permissions and camera upload still need testing.")
        return 0
    if result.get("error") == "unauthorized":
        print("FAILED: DEVICE_TOKEN differs. Copy apps-script-device-token.txt into Script Properties → DEVICE_TOKEN.")
    elif result.get("pending"):
        print("FAILED: No cached POST result. Verify the deployed /exec version and access permissions.")
    elif result.get("error") == "server_error":
        print("FAILED: SCRIPT_SERVER_ERROR. Kiem tra Apps Script → Executions de xem loi doPost.")
    else:
        print("FAILED: PROTOCOL_ERROR. Ket qua khong dung Code.gs; kiem tra phien ban da deploy.")
    return 1


def main(request_id=None):
    try:
        return check(request_id=request_id)
    except DiagnosticError as error:
        print("FAILED: " + str(error), file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("FAILED: CONFIG_ERROR. Khong doc duoc drive-webapp.json hoac dinh dang cau hinh sai. Token khong duoc in ra.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["urllib"] + list(CURL_MODES), default="urllib",
                        help="curl=IPv4; curl-auto=IP tu dong; curl-http1=HTTP/1.1; curl4-http1=IPv4 + HTTP/1.1")
    parser.add_argument("--timeout", type=int, choices=range(5, 61), default=20, metavar="5..60",
                        help="gioi han giay cho moi yeu cau (mac dinh 20)")
    args = parser.parse_args()
    TRANSPORT, TIMEOUT = args.transport, args.timeout
    sys.exit(main())
