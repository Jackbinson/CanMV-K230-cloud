#!/usr/bin/env python3
"""Run host simulations in separate processes; never start the USB camera test."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SUITES = (
    "test-check-drive.py",
    "test-drive-network.py",
    "test-google-pins.py",
    "test-recorder-https.py",
    "test-recorder-install.py",
    "test-recorder-mqtt.py",
    "test-recorder-update.py",
    "test-recorder-usb-runner.py",
)


def main():
    for suite in SUITES:
        print(suite, flush=True)
        result = subprocess.run([sys.executable, str(ROOT / suite)], cwd=ROOT)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
