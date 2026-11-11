#!/usr/bin/env python3
"""Probe Google routes without secrets; authenticate if a working route is found."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "drive-network-result.json"
PROBE_TIMEOUT = 25
KNOWN_HOSTS = {"www.google.com", "script.google.com", "script.googleusercontent.com", "accounts.google.com"}


def probe(label, url, mode=None, follow=False, expect_nonce=False):
    args = ["curl", "--disable", "--silent", "--show-error",
            "--proto", "=https", "--proto-redir", "=https",
            "--max-redirs", "5", "--connect-timeout", "8",
            "--max-time", str(PROBE_TIMEOUT), "--max-filesize", "131072",
            "--write-out", "\n%{http_code}\n%{time_namelookup}\n%{time_connect}\n%{time_appconnect}\n%{time_starttransfer}\n%{time_total}\n%{num_redirects}\n%{url_effective}"]
    modes = {"curl": ["--ipv4"], "curl-auto": [], "curl-http1": ["--http1.1"],
             "curl4-http1": ["--ipv4", "--http1.1"]}
    args.extend(modes.get(mode, []))
    if follow:
        args.append("--location")
    if expect_nonce:
        url += ("&" if "?" in url else "?") + "_cb=" + secrets.token_hex(8)
    args.extend(["--url", url])
    result = {"label": label, "mode": mode, "curl_exit": None, "http": 0,
              "host": "unknown", "nonce_valid": False}
    try:
        response = subprocess.run(args, capture_output=True, timeout=PROBE_TIMEOUT + 3)
    except FileNotFoundError:
        result["fault"] = "curl_missing"
        return result
    except subprocess.TimeoutExpired:
        result["fault"] = "process_timeout"
        return result
    except OSError:
        result["fault"] = "process_failed"
        return result
    result["curl_exit"] = response.returncode
    # Ignore stderr and raw bodies in reports; they can contain private URLs.
    try:
        fields = response.stdout.rsplit(b"\n", 8)
        if len(fields) != 9:
            raise ValueError()
        raw = fields[0]
        result["http"] = int(fields[1])
        for name, value in zip(("dns_s", "tcp_s", "tls_s", "first_byte_s", "total_s"), fields[2:7]):
            result[name] = round(float(value), 3)
        result["redirects"] = int(fields[7])
        # rsplit allows newlines in the original response body.
        host = urllib.parse.urlsplit(fields[8].decode("utf-8")).hostname
        result["host"] = host if host in KNOWN_HOSTS else "other"
    except (ValueError, UnicodeError):
        result["fault"] = "unparsed_curl_output"
        return result
    if expect_nonce and response.returncode == 0 and result["http"] == 200:
        try:
            body = json.loads(raw)
            result["nonce_valid"] = (isinstance(body, dict) and body.get("ok") is True and
                                     isinstance(body.get("request_id"), str) and
                                     bool(re.fullmatch(r"[a-f0-9]{32}", body["request_id"])))
            if result["nonce_valid"]:
                # Ephemeral only. Never print/persist this capability in reports.
                result["_nonce"] = body["request_id"]
        except (ValueError, UnicodeError):
            pass
    return result


def outcome(result):
    if result.get("nonce_valid"):
        return "NONCE_OK"
    errors = {5: "PROXY_DNS", 6: "DNS", 7: "CONNECT", 28: "TIMEOUT", 35: "TLS", 60: "CERT", 63: "TOO_LARGE"}
    if result.get("curl_exit") == 0:
        return "HTTP_%d" % result["http"]
    return errors.get(result.get("curl_exit"), result.get("fault", "CURL_ERROR"))


def explain(results):
    usable = [r for r in results if r["nonce_valid"]]
    if usable:
        return "Da tim thay duong ket noi tra nonce dung giao thuc."
    responses = [r for r in results if r.get("curl_exit") == 0 and r.get("http", 0) > 0]
    if not responses:
        return "Chua nhan duoc HTTP tu cac dia chi Google da thu. Can kiem tra ket noi mang/VPN/proxy tren laptop; chua the kiem tra token."
    web_responses = [r for r in responses if r.get("mode")]
    if any(r["host"] == "accounts.google.com" or r["http"] in (401, 403) for r in web_responses):
        return "Web App co dau hieu yeu cau dang nhap/tu choi truy cap. Kiem tra deployment va quyen truy cap."
    if any(r["http"] in (404, 410) for r in web_responses):
        return "Web App tra HTTP 404/410. Can kiem tra lai URL /exec va deployment."
    return "Mot so may chu Google co phan hoi, nhung chua lay duoc nonce tu Web App. Can doi chieu cac dong ben tren va Apps Script → Executions (doGet)."


def save_report(report):
    fd = os.open(REPORT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    REPORT.chmod(0o600)


def main():
    try:
        cfg = json.loads((ROOT / "drive-webapp.json").read_text())
        url = cfg["web_app_url"]
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "script.google.com" or not parsed.path.endswith("/exec"):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("CONFIG_ERROR: Khong doc duoc Web App URL trong drive-webapp.json.")
        return 1
    print("Do mang Google (khoang 25-30 giay). Cac phep do nay KHONG gui token.", flush=True)
    print("Neu tim duoc duong ket noi, se tu kiem tra token; KHONG tao file Drive.", flush=True)
    proxies = set(urllib.request.getproxies()) & {"http", "https", "all"}
    print("Proxy HTTP/HTTPS trong moi truong: " + ("co" if proxies else "khong"), flush=True)
    tests = [("Google Internet", "https://www.google.com/generate_204", None, False, False),
             ("Apps Script host", "https://script.google.com/", None, False, False),
             ("Content host", "https://script.googleusercontent.com/", None, False, False)]
    for mode in ("curl-auto", "curl", "curl-http1", "curl4-http1"):
        tests.append(("Web App " + mode, url + "?action=nonce", mode, True, True))
    results = []
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(probe, *test) for test in tests]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print("%-24s %-12s HTTP=%s host=%s DNS=%ss TCP=%ss TLS=%ss FirstByte=%ss Total=%ss" % (
                result["label"], outcome(result), result["http"], result["host"],
                result.get("dns_s", "?"), result.get("tcp_s", "?"), result.get("tls_s", "?"),
                result.get("first_byte_s", "?"), result.get("total_s", "?")), flush=True)
    report = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "proxies_configured": bool(proxies),
              "probes": [{k: v for k, v in r.items() if k != "_nonce"} for r in results],
              "auth_checked": False}
    print(explain(results), flush=True)
    working = sorted((r for r in results if r["nonce_valid"]), key=lambda r: r.get("total_s", 999))
    if working:
        mode = working[0]["mode"]
        print("Tu kiem tra xac thuc bang %s; toi da 30s/yeu cau, co log tung buoc." % mode, flush=True)
        spec = importlib.util.spec_from_file_location("check_drive", ROOT / "check-drive.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        checker.TRANSPORT, checker.TIMEOUT = mode, 30
        status = checker.main(request_id=working[0]["_nonce"])
        report.update(auth_checked=True, auth_ok=status == 0, selected_transport=mode)
    else:
        print("Thu lai lenh nay khi laptop dung hotspot 4G/5G de doi chieu voi mang hien tai.", flush=True)
        status = 1
    try:
        save_report(report)
        print("Bao cao chan doan (khong co token): " + str(REPORT), flush=True)
    except OSError:
        print("Khong luu duoc bao cao; ket qua o cac dong ben tren.", flush=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
