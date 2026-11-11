#!/usr/bin/env python3
"""Read the installed K230 recorder over the CanMV USBDBG protocol.

Run from a normal Linux terminal while the recorder is idle and CanMV IDE has
released the serial port. This pauses the running script for USBDBG inspection.
It reads Python files only and never writes to the camera's filesystem.
"""
import argparse
import errno
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import select
import struct
import sys
import termios
import time
import tty
import uuid
from datetime import datetime


class CameraRepl:
    def __init__(self, port):
        try:
            self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno == errno.EBUSY:
                raise RuntimeError(
                    port + " is busy. Save your editor tabs and quit CanMV IDE completely, "
                    "close other serial terminals, then rerun this command. "
                    "To identify the owner, run: fuser -v " + port
                ) from None
            raise
        try:
            self.saved = termios.tcgetattr(self.fd)
            self.buffer = bytearray()
            self.entered = False
            tty.setraw(self.fd)
            settings = termios.tcgetattr(self.fd)
            settings[4] = settings[5] = termios.B115200
            settings[2] |= termios.CLOCAL | termios.CREAD
            termios.tcsetattr(self.fd, termios.TCSANOW, settings)
            # Match the official CanMV backend: USBDBG replies require a
            # deassert/assert DTR edge, even if the port was opened before.
            fcntl.ioctl(self.fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_RTS))
            fcntl.ioctl(self.fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_DTR))
            time.sleep(0.05)
            fcntl.ioctl(self.fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_DTR))
            time.sleep(0.25)
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except BaseException:
            os.close(self.fd)
            raise

    def write(self, payload):
        deadline = time.monotonic() + 10
        while payload:
            if time.monotonic() > deadline:
                raise TimeoutError("USB write timed out.")
            if select.select([], [self.fd], [], 0.2)[1]:
                count = os.write(self.fd, payload)
                payload = payload[count:]

    def read_exact(self, count, timeout=5):
        deadline = time.monotonic() + timeout
        while len(self.buffer) < count:
            if time.monotonic() >= deadline:
                raise TimeoutError("CanMV USBDBG response timed out (%d/%d bytes)." % (len(self.buffer), count))
            if select.select([self.fd], [], [], 0.2)[0]:
                chunk = os.read(self.fd, 4096)
                if not chunk:
                    raise RuntimeError("The camera disconnected from USB.")
                self.buffer.extend(chunk)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def command(self, opcode, size=0):
        self.write(struct.pack("<BBI", 0x30, opcode, size))

    def poll_output(self):
        # Legacy firmware can send terminal bytes directly or buffer them for
        # TX_BUF_LEN/TX_BUF, so support both modes used by the official backend.
        raw = bytearray(self.buffer)
        self.buffer.clear()
        deadline = time.monotonic() + 0.03
        while time.monotonic() < deadline and select.select([self.fd], [], [], 0.005)[0]:
            part = os.read(self.fd, 4096)
            if not part:
                raise RuntimeError("The camera disconnected from USB.")
            raw.extend(part)
            if len(raw) > 65536:
                raise RuntimeError("Unexpectedly large terminal output from the camera.")
        if raw:
            return bytes(raw)
        self.command(0x8E, 4)
        header = self.read_exact(4)
        size = struct.unpack("<I", header)[0]
        if size == 0:
            return b""
        if size > 65536:
            # An unsolicited stdout prefix can arrive before the length reply.
            return header
        self.command(0x8F, size)
        return self.read_exact(size)

    def enter(self):
        self.command(0x80, 12)
        try:
            version = struct.unpack("<III", self.read_exact(12))
        except TimeoutError:
            raise RuntimeError(
                "No CanMV USBDBG firmware reply. Verify this is the same USB port "
                "that CanMV IDE can connect to, close the IDE, and restart the idle camera before retrying."
            ) from None
        if not any(version) or version[0] >= 100 or any(part > 10000 for part in version[1:]):
            raise RuntimeError("The selected USB port did not return a valid CanMV firmware version.")
        print("CanMV USBDBG connected; firmware protocol %d.%d.%d" % version, flush=True)
        self.command(0x06)
        time.sleep(0.1)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not self.poll_output():
                break
        self.entered = True

    def execute(self, source, timeout=20, on_output=None):
        marker = "K230_SOURCE_" + uuid.uuid4().hex
        start = (marker + "_BEGIN").encode()
        end = (marker + "_END").encode()
        failure = (marker + "_ERROR").encode()
        wrapped = "print(%r)\ntry:\n%s\nexcept Exception:\n print(%r)\nprint(%r)\n" % (
            start.decode(), "\n".join(" " + line for line in source.strip().splitlines()),
            failure.decode(), end.decode(),
        )
        payload = wrapped.encode("utf-8")
        # The official backend avoids exact multiples of a 64-byte USB packet.
        if len(payload) % 64 == 0:
            payload += b"\n"
        self.command(0x05, len(payload))
        time.sleep(0.5)
        self.write(payload)
        time.sleep(0.5)
        output = bytearray()
        delivered = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output.extend(self.poll_output())
            if len(output) > 262144:
                raise RuntimeError("Camera output exceeded the source-transfer limit.")
            begin = output.find(start)
            finish = output.find(end, begin + len(start)) if begin >= 0 else -1
            if on_output is not None and begin >= 0:
                if delivered is None:
                    delivered = begin + len(start)
                boundary = finish if finish >= 0 else len(output)
                while delivered < boundary:
                    newline = output.find(b"\n", delivered, boundary)
                    if newline < 0:
                        break
                    line = bytes(output[delivered:newline]).strip()
                    delivered = newline + 1
                    if line and failure not in line:
                        on_output(line)
            if finish >= 0:
                result = bytes(output[begin + len(start):finish]).strip()
                if failure in result:
                    raise RuntimeError("Camera script failed; its private traceback was not printed.")
                return result
            time.sleep(0.03)
        raise TimeoutError("CanMV USBDBG connected, but the camera script did not finish within %d seconds." % timeout)

    def close(self):
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        finally:
            os.close(self.fd)


INVENTORY = """
import os, ujson
_recorder_files = []
def _collect_recorder(path, depth):
    try:
        info = os.stat(path)
    except OSError:
        return
    if info[0] & 0x4000:
        if depth <= 0:
            return
        for name in sorted(os.listdir(path)):
            if name not in ('__pycache__', '.', '..'):
                _collect_recorder(path + '/' + name, depth - 1)
    elif path.endswith('.py') and path.rsplit('/', 1)[-1] != 'secrets.py':
        if info[6] <= 1048576:
            _recorder_files.append([path, info[6]])
for _path in ('/sdcard/main.py', '/sdcard/record_video.py', '/sdcard/recorder'):
    _collect_recorder(_path, 4)
print(ujson.dumps(_recorder_files))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="USB serial port, for example /dev/ttyACM0")
    args = parser.parse_args()
    ports = sorted(set(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")))
    if args.port:
        port = args.port
    elif len(ports) == 1:
        port = ports[0]
    elif not ports:
        raise RuntimeError("No USB serial port found. Connect the K230 with a USB data cable and try again.")
    else:
        raise RuntimeError("Several USB ports found: " + ", ".join(ports) + ". Rerun with --port /dev/ttyACM0 (choose the K230 port).")

    print("Reading installed recorder via CanMV USBDBG on " + port, flush=True)
    print("The recorder must be idle and CanMV IDE disconnected from this port.", flush=True)
    destination = Path(__file__).parent / "recorder-source" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination.mkdir(parents=True, mode=0o700)
    destination.parent.chmod(0o700)
    camera = CameraRepl(port)
    files = []
    try:
        camera.enter()
        inventory = json.loads(camera.execute(INVENTORY).decode("utf-8").strip())
        if not inventory:
            raise RuntimeError("No recorder Python files found in the expected SD-card locations.")
        for source, size in inventory:
            relative = PurePosixPath(source).relative_to("/sdcard")
            if ".." in relative.parts or not source.endswith(".py") or not isinstance(size, int) or not 0 <= size <= 1048576:
                raise RuntimeError("Unexpected source path or file size received from the camera.")
            data = bytearray()
            while len(data) < size:
                script = "import ubinascii\nwith open(%r, 'rb') as _f:\n _f.seek(%d)\n print(ubinascii.hexlify(_f.read(1024)).decode())" % (source, len(data))
                chunk = bytes.fromhex(camera.execute(script).decode("ascii").strip())
                if not chunk:
                    raise RuntimeError("A source file changed or became unreadable during transfer.")
                data.extend(chunk)
                if len(data) % 16384 == 0:
                    print("Reading " + str(relative) + ": " + str(len(data)) + "/" + str(size) + " bytes", flush=True)
            if len(data) != size:
                raise RuntimeError("A source file changed size during transfer; rerun while the camera is idle.")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            files.append({"path": str(relative), "bytes": size, "sha256": hashlib.sha256(data).hexdigest()})
            print("Copied " + str(relative) + " (" + str(size) + " bytes)", flush=True)
    finally:
        camera.close()

    manifest = {"source": "CanMV K230 USBDBG", "files": files}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Source saved privately to: " + str(destination))
    print("Camera files were not modified. Restart the K230 to resume its current startup program.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print("Cannot read recorder: " + str(error), file=sys.stderr)
        sys.exit(1)
