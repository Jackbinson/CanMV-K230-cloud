"""Token-free check of raw TLS socket reads on the CanMV board."""
import socket
import sys
import time

try:
    recorder_path = "/sdcard/recorder"
    if recorder_path not in sys.path:
        sys.path.insert(0, recorder_path)
    from wifi_client import ensure_wifi_connected
    if not ensure_wifi_connected(timeout=20):
        raise OSError("Wi-Fi has no active connection")

    try:
        import ssl
    except ImportError:
        import ussl as ssl

    host = "www.google.com"
    address = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0]
    raw = socket.socket(address[0], address[1], address[2])
    try:
        try:
            raw.settimeout(15)
        except Exception:
            pass
        raw.connect(address[-1])
        stream = ssl.wrap_socket(raw, server_hostname=host, cert_reqs=ssl.CERT_NONE)
        stream.write(b"GET /generate_204 HTTP/1.1\r\nHost: www.google.com\r\nConnection: close\r\n\r\n")
        print("RAW_HTTPS_STATUS_LINE", repr(stream.readline()))
        stream.close()
    finally:
        try:
            raw.close()
        except Exception:
            pass
except Exception as exc:
    print("RAW_HTTPS_PROBE_FAILED", repr(exc))
finally:
    time.sleep_ms(1200)
    import machine
    machine.reset()
