#!/usr/bin/env python3
"""Durably recover exact Docker writers interrupted during a data snapshot."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


JOURNAL = Path(os.environ.get("DAVID_PI_BACKUP_QUIESCENCE_JOURNAL", "/var/lib/david-pi-data-backup/quiescence.json"))
LOCK_DIRECTORY = Path(os.environ.get("DAVID_PI_WRITER_TRANSITION_LOCK_DIRECTORY", "/run/david-pi-writer-transition"))
DOCKER = os.environ.get("DAVID_PI_DOCKER_BIN", "/usr/bin/docker")
READINESS = os.environ.get("DAVID_PI_WRITER_READINESS", "/usr/local/sbin/david-pi-writer-readiness")
ALLOWED = {
    "david-pi-maintenance", "david-pi-chat-notifier", "david-pi-audiobook-preparer",
    "david-pi-mytube-preparer", "david-pi-device-backup-worker",
    "david-pi-slideshow-worker", "family-photo-portal",
}


def _identity(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _machine_id() -> str:
    return _identity(Path("/etc/machine-id"))


def _boot_id() -> str:
    return _identity(Path("/proc/sys/kernel/random/boot_id"))


def _safe_parent() -> None:
    info = JOURNAL.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("quiescence state directory is unsafe")


def _read() -> dict | None:
    _safe_parent()
    try:
        info = JOURNAL.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("quiescence journal is unsafe")
    value = json.loads(JOURNAL.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("machine_id") != _machine_id():
        raise RuntimeError("quiescence journal identity mismatch")
    writers = value.get("writers")
    if not isinstance(writers, list):
        raise RuntimeError("quiescence journal is invalid")
    for item in writers:
        if (
            not isinstance(item, dict) or item.get("name") not in ALLOWED
            or not isinstance(item.get("container_id"), str)
            or len(item["container_id"]) != 64
            or any(character not in "0123456789abcdef" for character in item["container_id"])
        ):
            raise RuntimeError("quiescence writer identity is invalid")
    return value


def _write(value: dict) -> None:
    _safe_parent()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".quiescence-", dir=JOURNAL.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, separators=(",", ":"), sort_keys=True)
            output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, JOURNAL)
        directory = os.open(JOURNAL.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def record(name: str, container_id: str) -> None:
    if name not in ALLOWED or len(container_id) != 64 or any(c not in "0123456789abcdef" for c in container_id):
        raise RuntimeError("refusing unsafe writer identity")
    value = _read() or {
        "schema_version": 1, "machine_id": _machine_id(), "boot_id": _boot_id(), "writers": [],
    }
    existing = next((item for item in value["writers"] if item["name"] == name), None)
    if existing and existing["container_id"] != container_id:
        raise RuntimeError("writer identity changed during quiescence")
    if not existing:
        value["writers"].append({"name": name, "container_id": container_id})
    _write(value)


def record_running() -> None:
    """Snapshot exact identities before the backup process can stop any writer."""
    for name in sorted(ALLOWED):
        result = subprocess.run(
            [DOCKER, "inspect", "--format", "{{.Id}} {{.State.Running}}", name],
            check=False, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            continue
        values = result.stdout.strip().split()
        if len(values) != 2:
            raise RuntimeError(f"writer inspection is invalid: {name}")
        if values[1] == "true":
            record(name, values[0])
        elif values[1] != "false":
            raise RuntimeError(f"writer state is invalid: {name}")


def _remove() -> None:
    value = _read()
    if value is None:
        return
    JOURNAL.unlink()
    directory = os.open(JOURNAL.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _lock():
    info = LOCK_DIRECTORY.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError("writer transition lock is unsafe")
    descriptor = os.open(LOCK_DIRECTORY, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def recover() -> None:
    lock_descriptor = _lock()
    try:
        value = _read()
        if value is None:
            return
        for item in reversed(value["writers"]):
            result = subprocess.run(
                [DOCKER, "inspect", "--format", "{{.Id}} {{.State.Running}}", item["name"]],
                check=True, capture_output=True, text=True, timeout=15,
            ).stdout.strip().split()
            if len(result) != 2 or result[0] != item["container_id"]:
                raise RuntimeError(f"writer identity changed: {item['name']}")
            if result[1] == "false":
                subprocess.run([DOCKER, "start", item["name"]], check=True, timeout=90)
            elif result[1] != "true":
                raise RuntimeError(f"writer state is invalid: {item['name']}")
        subprocess.run(
            [READINESS, "--timeout", "90", "--interval", "2", "--stable-samples", "3"],
            check=True, timeout=120,
        )
        _remove()
    finally:
        os.close(lock_descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    recorder = subparsers.add_parser("record")
    recorder.add_argument("--container", required=True, choices=sorted(ALLOWED))
    recorder.add_argument("--container-id", required=True)
    subparsers.add_parser("recover")
    subparsers.add_parser("record-running")
    subparsers.add_parser("clear")
    options = parser.parse_args()
    if options.operation == "record":
        record(options.container, options.container_id)
    elif options.operation == "recover":
        recover()
    elif options.operation == "record-running":
        record_running()
    else:
        _remove()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
