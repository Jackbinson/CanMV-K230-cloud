#!/usr/bin/env python3
"""Capture Google leaf certificates only after desktop CA + hostname verification."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import ssl
import time

ROOT = Path(__file__).resolve().parent
HOSTS = ("script.google.com", "script.googleusercontent.com", "www.googleapis.com",
         "content.googleapis.com", "www.google.com")
PROFILES = ("tls12-rsa", "tls12-ecdsa", "default")


def capture(host, address, profile, ca_pem):
    trust_store = Path('/etc/ssl/certs/ca-certificates.crt')
    context = (ssl.create_default_context(cafile=str(trust_store)) if trust_store.is_file()
               else ssl.create_default_context(cadata=ca_pem.decode("ascii")))
    if profile != "default":
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        cipher = "ECDHE-RSA-AES128-GCM-SHA256" if profile == "tls12-rsa" else "ECDHE-ECDSA-AES128-GCM-SHA256"
        context.set_ciphers(cipher)
    if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
        raise ValueError("Desktop TLS verification must remain enabled")
    with socket.create_connection((address, 443), timeout=8) as raw:
        with context.wrap_socket(raw, server_hostname=host) as stream:
            der = stream.getpeercert(binary_form=True)
            info = stream.getpeercert()
            start = int(ssl.cert_time_to_seconds(info["notBefore"]))
            end = int(ssl.cert_time_to_seconds(info["notAfter"]))
    now = int(time.time())
    if not start <= now < end:
        raise ValueError("Peer certificate is outside its validity interval")
    return {"sha256": hashlib.sha256(der).hexdigest(), "valid_from": start,
            "valid_until": end, "profile": profile, "address": address}


def collect_pins(progress=None):
    ca = runpy.run_path(str(ROOT / "recorder-update/recorder/google_ca.py"))["CA_PEM"]
    result = {"version": 1, "generated_at": int(time.time()), "hosts": {}}
    jobs = []
    for host in HOSTS:
        addresses = list(dict.fromkeys(item[-1][0] for item in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)))[:2]
        result["hosts"][host] = {"addresses": [], "certificates": []}
        for address in addresses:
            for profile in PROFILES:
                jobs.append((host, address, profile))
    with ThreadPoolExecutor(max_workers=5) as pool:
        pending = {pool.submit(capture, host, address, profile, ca): (host, profile) for host, address, profile in jobs}
        for future in as_completed(pending):
            host, profile = pending[future]
            try:
                certificate = future.result()
            except (OSError, ValueError, KeyError) as error:
                if progress:
                    progress(host, profile, False, type(error).__name__)
                continue
            entries = result["hosts"][host]["certificates"]
            addresses = result["hosts"][host]["addresses"]
            if certificate["address"] not in addresses:
                addresses.append(certificate["address"])
            if not any(item["sha256"] == certificate["sha256"] for item in entries):
                entries.append(certificate)
            if progress:
                progress(host, profile, True)
    missing = [host for host, value in result["hosts"].items() if not value["certificates"]]
    if missing:
        raise ValueError("Could not verify Google certificates for: " + ", ".join(missing))
    # Commit only a complete set, preserving the previous usable pinset on errors.
    path = ROOT / "google-pins.json"
    temporary = path.with_suffix(".json.new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return result


if __name__ == "__main__":
    def progress(host, profile, verified, error_type=None):
        print("Google certificate %s %s: %s" % (host, profile, "CA_VERIFIED" if verified else "profile_unavailable_" + str(error_type)), flush=True)
    try:
        print("Capturing Google certificates with CA and hostname verification; no device token is sent.", flush=True)
        pins = collect_pins(progress)
        print("Saved verified certificate pins: " + str(ROOT / "google-pins.json"))
    except (OSError, ValueError) as error:
        print("Cannot prepare pins: " + str(error))
        raise SystemExit(1)
