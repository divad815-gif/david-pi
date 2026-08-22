#!/usr/bin/env python3
"""Narrow Unix-socket bridge from the portal to the SSH-reversed Windows broker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import socket
import socketserver
import time
import urllib.error
import urllib.request


SOCKET_PATH = Path("/run/david-pi-windows/windows-broker.sock")
TARGET = "http://127.0.0.1:17890"
SECRET = os.environ["DAVID_PI_WINDOWS_BROKER_SECRET"].encode()
MAX_REQUEST = 32_000
MAX_RESPONSE = 65_536


def call_windows(payload: dict, timeout: int) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET,
        timestamp.encode() + b"\n" + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        TARGET + ("/health" if payload.get("action") == "health" else "/v1/run"),
        data=None if payload.get("action") == "health" else body,
        method="GET" if payload.get("action") == "health" else "POST",
        headers={
            "Content-Type": "application/json",
            "X-David-Pi-Timestamp": timestamp,
            "X-David-Pi-Signature": signature,
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"ok": False, "error": "windows_offline"}
    if len(data) > MAX_RESPONSE:
        return {"ok": False, "error": "response_too_large"}
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "invalid_response"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_REQUEST + 1)
        if not raw or len(raw) > MAX_REQUEST:
            return
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = {"ok": False, "error": "invalid_request"}
        else:
            action = payload.get("action")
            if action == "health":
                result = call_windows({"action": "health"}, 4)
            elif action == "run" and payload.get("mode") in {"general", "coding_readonly"}:
                messages = payload.get("messages")
                if (
                    not isinstance(messages, list)
                    or len(messages) > 10
                    or any(not isinstance(item, dict) for item in messages)
                ):
                    result = {"ok": False, "error": "invalid_messages"}
                else:
                    result = call_windows(payload, 620)
            else:
                result = {"ok": False, "error": "action_not_allowed"}
        self.wfile.write((json.dumps(result, separators=(",", ":")) + "\n").encode())


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def main():
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOCKET_PATH.unlink(missing_ok=True)
    with Server(str(SOCKET_PATH), Handler) as server:
        SOCKET_PATH.chmod(0o660)
        server.serve_forever()


if __name__ == "__main__":
    main()
