"""Small, read-only heartbeat probes; never import or initialize worker tasks.

Workers publish readiness only after their configuration, storage and first
cycle checks succeed. Their private runtime directories are new tmpfs mounts
on each container start, so an earlier container cannot supply a ready record.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime


PRIVACY = {
    "contains_paths": False,
    "contains_filenames": False,
    "contains_user_content": False,
    "contains_identities": False,
    "contains_secrets": False,
}


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _safe_metadata(metadata, *, directory, owner, device=None):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected(metadata.st_mode)
        and metadata.st_uid == owner
        and not stat.S_IMODE(metadata.st_mode) & 0o022
        and (directory or metadata.st_nlink == 1)
        and (device is None or metadata.st_dev == device)
    )


def _read_heartbeat(root, name, owner, limit):
    """Read a bounded regular file pinned to its trusted runtime directory."""
    directory = descriptor = -1
    try:
        before = os.lstat(root)
        if not _safe_metadata(before, directory=True, owner=owner):
            return None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open(root, flags | os.O_DIRECTORY)
        pinned = os.fstat(directory)
        if _identity(before) != _identity(pinned):
            return None
        file_before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not _safe_metadata(file_before, directory=False, owner=owner, device=pinned.st_dev):
            return None
        # O_NONBLOCK also prevents a raced FIFO/device substitution from hanging.
        descriptor = os.open(name, flags | os.O_NONBLOCK, dir_fd=directory)
        opened = os.fstat(descriptor)
        if (
            _identity(opened) != _identity(file_before)
            or not _safe_metadata(opened, directory=False, owner=owner, device=pinned.st_dev)
            or opened.st_size > limit
        ):
            return None
        raw = os.read(descriptor, limit + 1)
        after = os.stat(name, dir_fd=directory, follow_symlinks=False)
        root_after = os.lstat(root)
        if (
            len(raw) > limit
            or _identity(after) != _identity(opened)
            or not _safe_metadata(after, directory=False, owner=owner, device=pinned.st_dev)
            or _identity(root_after) != _identity(pinned)
            or not _safe_metadata(root_after, directory=True, owner=owner)
        ):
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)


def check_health(worker, runtime_root, owner, *, maximum_age=90, now=None):
    """Fail closed on malformed, replaced, unready, stale or future records."""
    try:
        if owner != os.geteuid() or not os.path.isabs(runtime_root):
            return False
        if worker == "maintenance":
            name, limit, schema, identity = "heartbeat.json", 65536, 2, "david-pi-maintenance"
        elif worker == "device-backup":
            name, limit, schema, identity = "health.json", 4096, 1, "david-pi-device-backup-worker"
        else:
            return False
        value = _read_heartbeat(runtime_root, name, owner, limit)
        if value is None:
            return False
        timestamp = value.get("updated_at")
        if not isinstance(timestamp, str):
            return False
        updated = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if updated.tzinfo is None or updated.utcoffset() is None:
            return False
        age = (time.time() if now is None else now) - updated.timestamp()
        ready = value.get("state") == "healthy" if worker == "maintenance" else value.get("ready") is True
        privacy = value.get("privacy")
        return (
            type(value.get("schema_version")) is int
            and value["schema_version"] == schema
            and value.get("worker") == identity
            and isinstance(privacy, dict)
            and privacy.keys() == PRIVACY.keys()
            and all(flag is False for flag in privacy.values())
            and ready
            and -5 <= age <= maximum_age
        )
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        return False


def main(worker):
    """The exact Docker health command avoids all application/worker imports."""
    try:
        if worker == "maintenance":
            runtime = os.environ.get("DAVID_PI_MAINTENANCE_RUNTIME", "/run/david-pi-maintenance")
            owner = int(os.environ.get("DAVID_PI_MAINTENANCE_UID", "10002"))
        elif worker == "device-backup":
            runtime = os.environ.get("DAVID_PI_DEVICE_BACKUP_RUNTIME", "/run/david-pi-device-backup")
            owner = os.geteuid()
        else:
            return 1
        return 0 if check_health(worker, runtime, owner) else 1
    except (ValueError, TypeError):
        return 1
