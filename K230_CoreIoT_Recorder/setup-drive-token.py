#!/usr/bin/env python3
"""Save the existing Apps Script DEVICE_TOKEN in the private upload config."""
import argparse
import getpass
import json
import os
import secrets
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true", help="Create a new private token to set in Apps Script DEVICE_TOKEN.")
    args = parser.parse_args()
    path = Path(__file__).with_name("drive-webapp.json")
    settings = json.loads(path.read_text())
    if not settings.get("web_app_url", "").startswith("https://script.google.com/macros/s/"):
        raise ValueError("The supplied Apps Script Web App URL is missing.")
    if args.generate:
        existing = settings.get("device_token", "")
        token = existing if isinstance(existing, str) and len(existing) >= 32 else secrets.token_urlsafe(32)
    else:
        print("Apps Script -> Project Settings -> Script Properties -> DEVICE_TOKEN")
        print("Enter that existing upload token below; input is hidden.")
        token = getpass.getpass("Apps Script DEVICE_TOKEN: ")
    if len(token) < 32:
        raise ValueError("The Apps Script code requires DEVICE_TOKEN to contain at least 32 characters.")
    coreiot = json.loads(Path(__file__).with_name("mqtt_config.json").read_text())
    if token == coreiot.get("mqtt_access_token"):
        raise ValueError("Use the separate Apps Script upload token.")
    settings["device_token"] = token
    settings["chunk_bytes"] = 262144
    with path.open("w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(settings, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("Saved private Drive upload configuration. Token: ****" + token[-4:])
    if args.generate:
        token_path = path.with_name("apps-script-device-token.txt")
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            os.chmod(token_path, 0o600)
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print("Open the private token file in your editor and copy its value into Apps Script DEVICE_TOKEN:")
        print(str(token_path))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        print("Configuration not saved: " + str(error), file=sys.stderr)
        sys.exit(1)
