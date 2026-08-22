#!/usr/bin/env python3
"""Validate the portal's fixed power-off request and ask systemd to shut down."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path


MOUNTPOINT = Path("/srv/data")
SENTINEL = Path("/srv/data/family-photos/.david-pi-storage")
EXPECTED_DATA_ID = "david-pi-family-storage-v1"
REQUEST_PATH = Path("/srv/data/family-photos/platform/control/shutdown.request")
EXPECTED_UID = 10001
MAX_AGE_SECONDS = 120


def validate_request(now=None):
    if not os.path.ismount(MOUNTPOINT):
        raise RuntimeError("external storage is not mounted")
    if SENTINEL.read_text(encoding="utf-8").strip() != EXPECTED_DATA_ID:
        raise RuntimeError("external storage identity does not match")
    info = REQUEST_PATH.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("shutdown request is not a regular file")
    if info.st_uid != EXPECTED_UID:
        raise RuntimeError("shutdown request owner is not the portal user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("shutdown request permissions are too broad")
    if info.st_size <= 0 or info.st_size > 1024:
        raise RuntimeError("shutdown request size is invalid")
    payload = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    if set(payload) != {"action", "request_id", "requested_at", "version"}:
        raise RuntimeError("shutdown request fields are invalid")
    if payload["action"] != "poweroff" or payload["version"] != 1:
        raise RuntimeError("shutdown action is invalid")
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or len(request_id) != 32 or any(
        character not in "0123456789abcdef" for character in request_id
    ):
        raise RuntimeError("shutdown request identifier is invalid")
    requested_at = payload["requested_at"]
    if not isinstance(requested_at, int):
        raise RuntimeError("shutdown request time is invalid")
    age = int(now or time.time()) - requested_at
    if age < -10 or age > MAX_AGE_SECONDS:
        raise RuntimeError("shutdown request is stale")
    return payload


def main():
    validate_request()
    REQUEST_PATH.unlink()
    subprocess.run(["/usr/bin/systemctl", "--no-block", "poweroff"], check=True, timeout=10)


if __name__ == "__main__":
    main()
