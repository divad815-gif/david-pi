"""Client used only after the portal verifies Tailscale identity and admin role."""
import json
import os
import socket


class HostError(RuntimeError):
    """A safe, user-facing validation or management failure."""


def request(operation, payload=None, identity=""):
    message = json.dumps({"operation": operation, "payload": payload or {}, "identity": identity}).encode() + b"\n"
    if len(message) > 65536:
        raise ValueError("Management request is too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(30)
        connection.connect(os.environ.get("DAVID_PI_HELPER_SOCKET", "/run/david-pi-helper/control.sock"))
        connection.sendall(message)
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(1048577))
    if not result.get("ok"):
        raise HostError(result.get("error", "Management operation failed"))
    return result["result"]
