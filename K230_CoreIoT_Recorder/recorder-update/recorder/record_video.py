"""Recorder for the factory Yahboom K230 MP4 API.

Blue: ready. Red: recording. Yellow: finalizing. Green: saved/stopped.
Blinking red: error; inspect last_error.txt and recording JSON files.
"""
import os
import time
import gc
import ujson as json
from media.mp4format import Mp4CfgStr
from clean_mp4 import CleanMp4Container
from ybUtils.YbKey import YbKey
from ybUtils.YbRGB import YbRGB
from mp4_metrics import read_video_metrics
from remote_control import RecorderControl, start_services
try:
    from wifi_client import ensure_wifi_connected
except ImportError:
    ensure_wifi_connected = None

CONFIG_PATH = "/sdcard/recorder/recording_config.json"
ERROR_PATH = "/sdcard/recorder/last_error.txt"
_engine = None
_control = None


def idle_step():
    if _engine is not None:
        _engine.idle()
    else:
        time.sleep_ms(30)


def shutdown():
    global _engine
    if _control is not None:
        _control.alive = False
    if _engine is not None:
        _engine.shutdown()
        _engine = None


def sync_files():
    if hasattr(os, "sync"):
        os.sync()


def free_bytes(path):
    values = os.statvfs(path)
    return (values[1] or values[0]) * values[4]


def save_json(path, data):
    with open(path, "w") as stream:
        json.dump(data, stream)
        stream.flush()
    sync_files()


def load_config():
    with open(CONFIG_PATH) as stream:
        cfg = json.load(stream)
    if cfg["output_dir"] not in ("/sdcard/recordings", "/data/recordings"):
        raise ValueError("Output must be /sdcard/recordings or /data/recordings")
    for key, minimum, maximum in (("width", 320, 1280), ("height", 240, 720),
                                  ("clip_seconds", 5, 600), ("session_seconds", 5, 21600),
                                  ("reserve_mb", 64, 1024)):
        value = cfg.get(key)
        if not isinstance(value, int) or value < minimum or value > maximum:
            raise ValueError("Invalid configuration: " + key)
    if not isinstance(cfg.get("auto_start", False), bool):
        raise ValueError("Invalid configuration: auto_start")
    if cfg["width"] % 16:
        raise ValueError("Video width must be aligned to 16 pixels")
    name = cfg.get("camera_id", "")
    if not name or len(name) > 32 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for c in name):
        raise ValueError("Invalid camera_id")
    root = cfg["output_dir"].rsplit("/", 1)[0]
    os.stat(root)  # Do not create a missing mount point.
    if root == "/data":
        data_fs = os.statvfs(root)
        app_fs = os.statvfs("/sdcard")
        if data_fs[2] == app_fs[2]:
            raise ValueError("/data does not appear to be a separate data partition")
    if free_bytes(root) < (cfg["reserve_mb"] + 16) * 1024 * 1024:
        raise OSError("Not enough free storage")
    try:
        os.stat(cfg["output_dir"])
    except OSError:
        os.mkdir(cfg["output_dir"])
    return cfg


def next_stem(cfg):
    names = set(os.listdir(cfg["output_dir"]))
    for number in range(1, 100000):
        stem = "%s_%05d" % (cfg["camera_id"], number)
        if not any(stem + suffix in names for suffix in (".mp4", ".partial.mp4", ".json")):
            return cfg["output_dir"] + "/" + stem
    raise OSError("Recording file numbers exhausted")


def wait_release(key):
    while key.is_pressed():
        os.exitpoint()
        idle_step()
    begin = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), begin) < 150:
        os.exitpoint()
        idle_step()


def capture_clip(cfg, key, rgb, seconds):
    global _engine
    _control.update(recorder_state="starting", recording=False)
    stem = next_stem(cfg)
    partial = stem + ".partial.mp4"
    metadata = {"schema_version": 2, "recorder_version": "2026-11-11.remote.1", "camera_id": cfg["camera_id"],
                "lighting": cfg.get("lighting", "unknown"), "status": "recording",
                "video_file": partial.rsplit("/", 1)[1],
                "width": cfg["width"], "height": cfg["height"], "codec": "H264",
                "audio_codec": "G711U", "rtc_at_start": list(time.localtime()),
                "rtc_verified": False, "requested_sensor_fps": None,
                "requested_clip_seconds": seconds}
    save_json(stem + ".json", metadata)
    muxer = None
    started = False
    failure = None
    cleanup_errors = []
    begin = None
    end = None
    reason = "duration"
    try:
        gc.collect()
        if _engine is None:
            _engine = CleanMp4Container()
            muxer = _engine
            setup = Mp4CfgStr(muxer.MP4_CONFIG_TYPE_MUXER)
            setup.SetMuxerCfg(partial, muxer.MP4_CODEC_ID_H264, cfg["width"], cfg["height"], muxer.MP4_CODEC_ID_G711U)
            muxer.Create(setup)
            muxer.Start()
        else:
            muxer = _engine
            muxer.open_next(partial)
        started = True
        begin = time.ticks_ms()
        checked = begin
        rgb.show_rgb((40, 0, 0))
        _control.update(recorder_state="recording", recording=True)
        while True:
            os.exitpoint()
            if muxer.frames_written > 0 and _control.stopped():
                reason = "remote_stop"
                break
            if key.is_pressed():
                reason = "key_stop"
                break
            muxer.Process()
            now = time.ticks_ms()
            if time.ticks_diff(now, begin) >= seconds * 1000:
                break
            if time.ticks_diff(now, checked) >= 1000:
                checked = now
                if free_bytes(cfg["output_dir"]) < cfg["reserve_mb"] * 1024 * 1024:
                    reason = "low_space"
                    break
    except KeyboardInterrupt:
        reason = "ide_stop"
    except Exception as exc:
        if "IDE interrupt" in str(exc):
            reason = "ide_stop"
        else:
            failure = str(exc)
            reason = "error"
    finally:
        # Complete Stop/Destroy + metadata even when an IDE interrupt is pending.
        os.exitpoint(os.EXITPOINT_ENABLE_SLEEP)
        end = time.ticks_ms()
        rgb.show_rgb((40, 25, 0))
        _control.update(recorder_state="finalizing", recording=False)
        if muxer is not None:
            try:
                muxer.close_file()
            except Exception as exc:
                cleanup_errors.append("Close MP4: " + str(exc))
        sync_files()

    elapsed = time.ticks_diff(end, begin) / 1000 if begin is not None else 0
    metadata["wall_record_seconds"] = elapsed
    metadata["stop_reason"] = reason
    metadata["error"] = failure
    metadata["cleanup_errors"] = cleanup_errors
    metadata["status"] = "error" if failure or cleanup_errors else "finalized"
    actual_frames = muxer.frames_written if muxer is not None else 0
    metadata["frames_written"] = actual_frames
    metadata["header_packets_excluded"] = muxer.header_packets if muxer is not None else 0
    if muxer is not None:
        metadata["first_frame_pts_us"] = muxer.first_video_pts
        metadata["last_frame_pts_us"] = muxer.last_video_pts
        metadata["max_frame_gap_us"] = muxer.max_pts_gap_us
        span = (muxer.last_video_pts - muxer.first_video_pts) if actual_frames > 1 else 0
        metadata["fps_encoder_timestamps"] = (actual_frames-1)*1000000/span if span > 0 else None
    try:
        metrics = read_video_metrics(partial, expected_frames=actual_frames)
        metadata.update(metrics)
        metadata["fps_saved_per_wall_second"] = metrics["video_frames"] / elapsed if elapsed > 0 else None
        metadata["fps_measurement"] = "H264 picture packets successfully written, verified against finalized MP4 sample tables; codec-only headers excluded. FPS wall includes startup; fps_encoder_timestamps uses hardware picture PTS."
        if not failure and not cleanup_errors:
            final_path = stem + ".mp4"
            os.rename(partial, final_path)
            metadata["video_file"] = final_path.rsplit("/", 1)[1]
    except Exception as exc:
        metadata["metrics_error"] = str(exc)
        metadata["status"] = "needs_review"
    save_json(stem + ".json", metadata)
    os.exitpoint(os.EXITPOINT_ENABLE)
    if metadata["status"] != "finalized":
        raise RuntimeError("Recording needs review: " + stem + ".json")
    _control.update(last_video=metadata["video_file"], last_stop_reason=reason,
                    recorder_state="between_clips", recording=False)
    print("SAVED", metadata["video_file"], "frames", metadata["video_frames"],
          "wall FPS", metadata["fps_saved_per_wall_second"], "MP4 FPS", metadata["fps_media_timeline"])
    if reason != "ide_stop":
        wait_release(key)
    return reason, elapsed


def _run_loop():
    global _control
    os.exitpoint(os.EXITPOINT_ENABLE)
    rgb = YbRGB()
    key = YbKey()
    rgb.show_rgb((0, 0, 40))
    cfg = load_config()
    _control = RecorderControl()
    _control.update(camera_id=cfg["camera_id"], recorder_version="2026-11-11.remote.1")
    if ensure_wifi_connected is not None:
        rgb.show_rgb((40, 20, 0))
        if not ensure_wifi_connected(timeout=20):
            print("WIFI_OFFLINE: recordings will remain on local storage.")
        rgb.show_rgb((0, 0, 40))
    else:
        print("WIFI_HELPER_MISSING: recordings will remain on local storage.")
    start_services(_control, cfg)
    wait_release(key)
    auto_start = cfg.get("auto_start", False)
    if auto_start:
        print("RECORDER AUTO START: recording will begin after boot and split into clips.")
    else:
        print("RECORDER READY: KEY or CoreIoT setRecording starts/stops a session.")
    while True:
        os.exitpoint()
        if not auto_start:
            requested = _control.take_start()
            if not requested and not key.is_pressed():
                idle_step()
                continue
            if key.is_pressed():
                wait_release(key)
        _control.begin_session()
        try:
            # An in-flight HTTP operation finishes before any new MP4 opens.
            # The queued session prevents the uploader from starting another.
            if _control.upload_busy():
                _control.update(recorder_state="waiting_for_upload")
            while _control.upload_busy() and not _control.stopped():
                os.exitpoint()
                idle_step()
            remaining = cfg["session_seconds"]
            while remaining > 0 and not _control.stopped():
                if free_bytes(cfg["output_dir"]) < (cfg["reserve_mb"] + 16) * 1024 * 1024:
                    raise OSError("Storage reserve reached; copy videos to laptop")
                reason, elapsed = capture_clip(cfg, key, rgb, min(cfg["clip_seconds"], remaining))
                if reason == "ide_stop":
                    return
                remaining -= elapsed
                if reason != "duration" or remaining < 1:
                    break
        finally:
            _control.finish_session()
        rgb.show_rgb((0, 40, 0))
        auto_start = False
        print("SESSION SAVED locally. Ready for KEY/CoreIoT; Drive upload runs while idle.")


def run():
    try:
        _run_loop()
    finally:
        shutdown()


def report_error(exc):
    try:
        with open(ERROR_PATH, "w") as stream:
            stream.write(str(exc) + "\n")
        sync_files()
    finally:
        rgb = YbRGB()
        while True:
            os.exitpoint()
            rgb.show_rgb((40, 0, 0))
            time.sleep_ms(150)
            rgb.show_rgb((0, 0, 0))
            time.sleep_ms(150)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        report_error(exc)
        raise
