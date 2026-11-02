"""Check HTTP/TLS module availability on CanMV K230; performs no network I/O."""
import time

print("CANMV_HTTP_MODULE_CHECK_BEGIN")

module_names = (
    "socket",
    "ssl",
    "ussl",
    "urequests",
    "urequest",
    "requests",
    "ujson",
    "network",
)

for module_name in module_names:
    try:
        module = __import__(module_name)
        print("MODULE_OK", module_name)
        if module_name in ("ssl", "ussl"):
            print(
                "TLS_CAPABILITIES",
                module_name,
                "wrap_socket=", hasattr(module, "wrap_socket"),
                "CERT_REQUIRED=", hasattr(module, "CERT_REQUIRED"),
                "CERT_NONE=", hasattr(module, "CERT_NONE"),
            )
    except Exception as exc:
        print("MODULE_MISSING", module_name, str(exc))

print("CANMV_HTTP_MODULE_CHECK_DONE")
print("Board will reboot in 2 seconds to restart its startup program.")
time.sleep_ms(2000)

try:
    import machine
    machine.reset()
except Exception as exc:
    print("BOARD_RESET_FAILED", str(exc))
