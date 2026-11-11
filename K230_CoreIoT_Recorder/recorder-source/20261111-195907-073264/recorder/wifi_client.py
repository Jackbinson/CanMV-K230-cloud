"""Connect the K230 WLAN station using the existing SD-card Wi-Fi config."""
import os
import time
import ujson as json


def load_credentials():
    candidates = (
        ("wifi", "/sdcard/wifi_config.json"),
        ("config", "/sdcard/config.json"),
        ("system", "/sdcard/configs/sys_config.json"),
    )
    for kind, path in candidates:
        try:
            with open(path, "r") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                continue
            if kind == "wifi":
                section = data
                ssid = section.get("ssid", section.get("SSID", ""))
                password = section.get("password", section.get("PASSWORD", ""))
            elif kind == "config":
                section = data.get("wifi", {}) or data.get("WLAN", {})
                if not isinstance(section, dict):
                    continue
                ssid = section.get("ssid", section.get("SSID", ""))
                password = section.get("password", section.get("PASSWORD", ""))
            else:
                section = data.get("WLAN", {})
                if not isinstance(section, dict):
                    continue
                ssid = section.get("SSID", "")
                password = section.get("PASSWORD", "")
            if ssid:
                return ssid, password
        except Exception:
            pass
    return None, None


def ensure_wifi_connected(timeout=20):
    """Connect STA from saved credentials; return True only with a valid IP."""
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        sta.active(True)
        if sta.isconnected():
            ip = sta.ifconfig()[0]
            if ip and ip != "0.0.0.0":
                print("WIFI_CONNECTED", ip)
                return True

        ssid, password = load_credentials()
        if not ssid:
            print("WIFI_CREDENTIALS_NOT_FOUND")
            return False

        print("WIFI_CONNECTING using saved configuration")
        try:
            sta.connect(ssid, password)
        except Exception as exc:
            # A connection attempt may already be in progress; poll its state.
            print("WIFI_CONNECT_CALL", str(exc))

        started = time.ticks_ms()
        while not sta.isconnected():
            if time.ticks_diff(time.ticks_ms(), started) >= timeout * 1000:
                break
            try:
                os.exitpoint()
            except Exception:
                pass
            time.sleep_ms(100)

        if sta.isconnected():
            ip = sta.ifconfig()[0]
            if ip and ip != "0.0.0.0":
                print("WIFI_CONNECTED", ip)
                return True
        print("WIFI_CONNECT_TIMEOUT")
    except Exception as exc:
        print("WIFI_INIT_FAILED", str(exc))
    return False
