"""Check basic HTTPS with the board's requests module; sends no credentials."""
import time
import sys

try:
    recorder_path = "/sdcard/recorder"
    if recorder_path not in sys.path:
        sys.path.insert(0, recorder_path)
    from wifi_client import ensure_wifi_connected
    if not ensure_wifi_connected(timeout=20):
        raise OSError("Wi-Fi has no active connection")

    import requests
    response = requests.get("https://www.google.com/generate_204")
    print("HTTP_PROBE_STATUS", response.status_code)
    try:
        response.close()
    except Exception:
        pass
except Exception as exc:
    print("HTTP_PROBE_FAILED", repr(exc))
finally:
    # Return the board to its configured startup program after printing output.
    time.sleep_ms(1200)
    import machine
    machine.reset()
