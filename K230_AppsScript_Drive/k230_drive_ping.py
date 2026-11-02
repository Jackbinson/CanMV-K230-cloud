"""Read the saved K230 config and check Apps Script without uploading a file."""
import ujson as json
import urequests

CONFIG_PATH = "/sdcard/recorder/drive_upload_config.json"


def header(headers, name):
    wanted = name.lower()
    for key in headers:
        if str(key).lower() == wanted:
            return str(headers[key])
    return ""


def close(response):
    try:
        response.close()
    except Exception:
        pass


with open(CONFIG_PATH, "r") as stream:
    config = json.load(stream)

payload = json.dumps({"action": "ping", "token": config["device_token"]})
response = urequests.post(
    config["webapp_url"],
    data=payload,
    headers={"Content-Type": "application/json"}
)
redirects = 0
while response.status_code in (301, 302, 303, 307, 308) and redirects < 3:
    location = header(response.headers, "Location")
    close(response)
    if not location:
        raise OSError("Apps Script redirect had no Location header")
    response = urequests.get(location)
    redirects += 1

status = response.status_code
body = response.text
close(response)
try:
    result = json.loads(body)
except Exception:
    raise OSError("Apps Script returned HTTP " + str(status) + " and non-JSON response")

if status < 200 or status >= 300 or not result.get("ok"):
    raise OSError("Apps Script ping failed: " + str(result.get("error", status)))

print("APPS_SCRIPT_PING_OK", result.get("service", ""))
