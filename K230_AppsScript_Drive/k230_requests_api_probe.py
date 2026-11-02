"""Inspect the firmware requests API without network access or credentials."""
import time

try:
    import requests
    print("REQUESTS_MODULE_PATH", getattr(requests, "__file__", "<builtin-or-frozen>"))
    for name in ("request", "get", "post", "put", "head"):
        fn = getattr(requests, name, None)
        if fn is None:
            continue
        print("REQUESTS_FUNCTION", name)
        doc = getattr(fn, "__doc__", None)
        if doc:
            print("REQUESTS_DOC", name, doc)
        code = getattr(fn, "__code__", None)
        if code is not None:
            print("REQUESTS_ARGUMENTS", name, getattr(code, "co_varnames", ()))
except Exception as exc:
    print("REQUESTS_API_PROBE_FAILED", repr(exc))
finally:
    time.sleep_ms(1200)
    import machine
    machine.reset()
