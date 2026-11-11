"""Boot selector. Original lane program is preserved as main_lane_original.py."""
import os
import sys
import time
import ujson

sys.path.insert(0, "/sdcard/recorder")


def recorder_selected():
    with open("/sdcard/recorder/recording_config.json") as stream:
        config = ujson.load(stream)
    if not config.get("enabled", False):
        return False
    from ybUtils.YbKey import YbKey
    from ybUtils.YbRGB import YbRGB
    key = YbKey()
    rgb = YbRGB()
    start = time.ticks_ms()
    rgb.show_rgb((15, 0, 20))
    while time.ticks_diff(time.ticks_ms(), start) < 5000:
        os.exitpoint()
        if key.is_pressed():
            while key.is_pressed():
                os.exitpoint()
                time.sleep_ms(30)
            rgb.show_rgb((0, 0, 0))
            return False
        time.sleep_ms(30)
    rgb.show_rgb((0, 0, 0))
    return True


os.exitpoint(os.EXITPOINT_ENABLE)
try:
    # A temporary marker lets maintenance scripts run without starting camera media.
    if "maintenance.flag" in os.listdir("/sdcard/recorder"):
        use_recorder = None
    else:
        use_recorder = recorder_selected()
except Exception as exc:
    if "IDE interrupt" in str(exc):
        raise
    print("Recorder selection unavailable; starting preserved lane app:", exc)
    use_recorder = False

if use_recorder is None:
    print("RECORDER MAINTENANCE: startup paused for verification.")
elif use_recorder:
    try:
        import record_video
        record_video.run()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if "IDE interrupt" in str(exc):
            raise
        print("Recorder stopped:", exc)
        with open("/sdcard/recorder/last_error.txt", "w") as stream:
            stream.write(str(exc) + "\n")
        if hasattr(os, "sync"):
            os.sync()
        from ybUtils.YbRGB import YbRGB
        error_led = YbRGB()
        while True:
            os.exitpoint()
            error_led.show_rgb((40, 0, 0))
            time.sleep_ms(150)
            error_led.show_rgb((0, 0, 0))
            time.sleep_ms(150)
else:
    with open("/sdcard/main_lane_original.py") as stream:
        original_source = stream.read()
    exec(original_source, {"__name__": "__main__", "__file__": "/sdcard/main_lane_original.py"})
