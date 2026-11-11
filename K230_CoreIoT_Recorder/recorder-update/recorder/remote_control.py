"""Recorder commands and telemetry. Network threads never access media objects."""
import _thread
import time
import ujson as json


class RecorderControl:
    def __init__(self):
        self.lock = _thread.allocate_lock()
        self.alive = True
        self.pending_start = False
        self.stop_requested = False
        self.upload_requested = False
        self.upload_inflight = False
        self.state = {"recorder_state": "idle", "recording": False,
                      "session_active": False, "upload_state": "waiting",
                      "mqtt_connected": False}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def stopped(self):
        with self.lock:
            return self.stop_requested

    def take_start(self):
        with self.lock:
            value = self.pending_start
            self.pending_start = False
            return value

    def begin_upload_io(self):
        with self.lock:
            if not self.alive or self.state["session_active"]:
                return False
            self.upload_inflight = True
            return True

    def end_upload_io(self):
        with self.lock:
            self.upload_inflight = False

    def upload_busy(self):
        with self.lock:
            return self.upload_inflight

    def take_upload_request(self):
        with self.lock:
            value = self.upload_requested
            self.upload_requested = False
            return value

    def begin_session(self):
        with self.lock:
            self.pending_start = False
            self.state.update(recorder_state="starting", session_active=True, recording=False)

    def finish_session(self):
        with self.lock:
            self.stop_requested = False
            self.pending_start = False
            self.state.update(recorder_state="idle", session_active=False, recording=False)

    def handle(self, request):
        if not isinstance(request, dict):
            return {"ok": False, "error": "invalid_request"}
        method = request.get("method")
        if method in ("getRecording", "getRecorderStatus"):
            state = self.snapshot()
            return state["recording"] if method == "getRecording" else state
        if method == "uploadPending":
            with self.lock:
                self.upload_requested = True
            return {"ok": True, "accepted": True}
        if method == "setRecording":
            value = request.get("params")
            if type(value) is not bool:
                return {"ok": False, "error": "params_must_be_boolean"}
        elif method in ("startRecording", "stopRecording"):
            value = method == "startRecording"
        else:
            return {"ok": False, "error": "unsupported_method"}
        with self.lock:
            if value:
                if self.stop_requested:
                    return {"ok": False, "error": "stop_in_progress"}
                if not self.state["session_active"]:
                    self.pending_start = True
                    self.state.update(recorder_state="queued", session_active=True)
            elif self.pending_start:
                self.pending_start = False
                self.state.update(recorder_state="idle", session_active=False)
            elif self.state["session_active"]:
                self.stop_requested = True
            return {"ok": True, "accepted": True, "recording": self.state["recording"],
                    "recorder_state": self.state["recorder_state"]}


def rpc_response(control, replies, request_id, payload):
    cached = next((item[2] for item in replies if item[0] == request_id and item[1] == payload), None)
    if cached is not None:
        return cached
    try:
        result = control.handle(json.loads(payload))
    except (ValueError, TypeError):
        result = {"ok": False, "error": "invalid_json"}
    response = json.dumps(result)
    replies.append((request_id, payload, response))
    if len(replies) > 16:
        replies.pop(0)
    return response


def mqtt_loop(control):
    from mqtt_transport import SocketMqttClient
    from wifi_client import ensure_wifi_connected
    client = None
    delay = 2
    replies = []
    while control.alive:
        try:
            if not ensure_wifi_connected(timeout=10):
                raise OSError("offline")
            with open("/sdcard/mqtt_config.json") as stream:
                cfg = json.load(stream)
            token = cfg["mqtt_access_token"]
            if not token:
                raise ValueError("missing_config")
            client = SocketMqttClient(cfg.get("client_id", "K230_Recorder"),
                                      cfg.get("broker", "app.coreiot.io"),
                                      int(cfg.get("port", 1883)), token, "", 30)
            client.connect()
            client.subscribe("v1/devices/me/rpc/request/+")
            # The transport may restart its RPC counter on a new connection.
            replies = []
            control.update(mqtt_connected=True, mqtt_error="")
            delay = 2
            last_sent = None
            while control.alive:
                incoming = client.check_msg(wait=0.02)
                if incoming:
                    topic, payload = incoming
                    prefix = "v1/devices/me/rpc/request/"
                    request_id = topic[len(prefix):] if topic.startswith(prefix) else ""
                    if request_id and len(request_id) <= 32 and request_id.isdigit():
                        cached = rpc_response(control, replies, request_id, payload)
                        client.publish("v1/devices/me/rpc/response/" + request_id, cached)
                now = time.ticks_ms()
                if last_sent is None or time.ticks_diff(now, last_sent) >= 5000:
                    client.publish("v1/devices/me/telemetry", json.dumps(control.snapshot()))
                    last_sent = now
                client.keepalive(now)
                time.sleep_ms(30)
        except Exception:
            control.update(mqtt_connected=False, mqtt_error="connection_failed")
        finally:
            if client is not None:
                client.close()
                client = None
        for _ in range(delay * 10):
            if not control.alive:
                break
            time.sleep_ms(100)
        delay = min(delay * 2, 60)


def start_services(control, cfg):
    """Failure leaves the existing physical KEY recorder available."""
    try:
        _thread.start_new_thread(mqtt_loop, (control,))
    except Exception:
        control.update(mqtt_error="thread_start_failed")
    try:
        from drive_upload import upload_loop
        _thread.start_new_thread(upload_loop, (control, cfg))
    except Exception:
        control.update(upload_state="unavailable", upload_error="thread_start_failed")
