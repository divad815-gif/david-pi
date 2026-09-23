"""Singleton lifecycle owner for device-upload cleanup and secondary copies."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .device_backup import (
    cleanup_stale_upload_sessions,
    initialize_device_backup,
    secondary_verify_once,
)


LOGGER = logging.getLogger(__name__)
LOCK_NAME = ".device-backup-housekeeping.lock"
HEALTH_NAME = "health.json"
HEALTH_MAX_AGE_SECONDS = 90
MAX_PROGRESS_SILENCE_SECONDS = 180


class ActivationError(RuntimeError):
    """The worker cannot prove that its storage or singleton is safe."""


def _bounded_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ActivationError(f"{name} is invalid") from error
    if value < minimum or value > maximum:
        raise ActivationError(f"{name} is outside its approved range")
    return value


@dataclass(frozen=True)
class Config:
    data_root: Path
    primary_sentinel: Path
    runtime_root: Path
    expected_data_id: str
    interval_seconds: int
    heartbeat_seconds: int
    secondary_batch_size: int
    secondary_root: Path | None
    secondary_sentinel_id: str

    @property
    def database(self) -> Path:
        return self.data_root / "photos.db"

    @property
    def incoming_root(self) -> Path:
        return self.data_root / "incoming" / "device-backup"

    @property
    def originals(self) -> Path:
        return self.data_root / "originals"

    @classmethod
    def from_environment(cls) -> "Config":
        raw_secondary = os.environ.get("DAVID_PI_SECONDARY_BACKUP_ROOT", "").strip()
        secondary_id = os.environ.get("DAVID_PI_SECONDARY_DATA_ID", "").strip()
        if bool(raw_secondary) != bool(secondary_id):
            raise ActivationError("secondary storage configuration is incomplete")
        return cls(
            data_root=Path(os.environ.get("PHOTO_DATA", "/data")),
            primary_sentinel=Path(
                os.environ.get("DAVID_PI_DATA_SENTINEL", "/data/.david-pi-storage")
            ),
            runtime_root=Path(
                os.environ.get(
                    "DAVID_PI_DEVICE_BACKUP_RUNTIME", "/run/david-pi-device-backup"
                )
            ),
            expected_data_id=os.environ.get(
                "DAVID_PI_DATA_ID", "david-pi-family-storage-v1"
            ),
            interval_seconds=_bounded_integer(
                "DAVID_PI_DEVICE_BACKUP_INTERVAL", 60, 10, 3600
            ),
            heartbeat_seconds=_bounded_integer(
                "DAVID_PI_DEVICE_BACKUP_HEARTBEAT", 15, 5, 30
            ),
            secondary_batch_size=_bounded_integer(
                "DAVID_PI_DEVICE_BACKUP_SECONDARY_BATCH", 8, 1, 64
            ),
            secondary_root=Path(raw_secondary) if raw_secondary else None,
            secondary_sentinel_id=secondary_id,
        )


def _identity(path: Path, *, directory: bool) -> tuple[int, int]:
    try:
        if path.is_symlink():
            raise OSError
        details = path.stat()
    except OSError as error:
        raise ActivationError("required worker storage is unavailable") from error
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(details.st_mode):
        raise ActivationError("required worker storage has an unsafe type")
    if not directory and details.st_nlink != 1:
        raise ActivationError("required worker file has an unsafe link count")
    return details.st_dev, details.st_ino


def _read_sentinel(config: Config) -> None:
    identity = _identity(config.primary_sentinel, directory=False)
    try:
        root = config.data_root.resolve(strict=True)
        sentinel = config.primary_sentinel.resolve(strict=True)
        value = config.primary_sentinel.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ActivationError("primary storage identity is unavailable") from error
    if (
        value != config.expected_data_id
        or not sentinel.is_relative_to(root)
        or _identity(config.primary_sentinel, directory=False) != identity
    ):
        raise ActivationError("primary storage identity does not match")


def _secondary_identities(config: Config) -> dict[str, tuple[int, int]]:
    if config.secondary_root is None:
        return {}
    root = config.secondary_root
    sentinel = root / ".david-pi-secondary-storage"
    originals = root / "originals"
    identities = {
        "secondary": _identity(root, directory=True),
        "secondary_sentinel": _identity(sentinel, directory=False),
        "secondary_originals": _identity(originals, directory=True),
    }
    try:
        primary_root = config.data_root.resolve(strict=True)
        secondary_root = root.resolve(strict=True)
        sentinel_value = sentinel.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ActivationError("secondary storage identity is unavailable") from error
    if (
        sentinel_value != config.secondary_sentinel_id
        or config.secondary_sentinel_id == config.expected_data_id
        or secondary_root == primary_root
        or secondary_root.is_relative_to(primary_root)
        or primary_root.is_relative_to(secondary_root)
        or identities["secondary"][0] == _identity(config.data_root, directory=True)[0]
        or not os.access(originals, os.R_OK | os.W_OK)
    ):
        raise ActivationError("secondary storage is not independent and writable")
    if _identity(sentinel, directory=False) != identities["secondary_sentinel"]:
        raise ActivationError("secondary storage identity changed during validation")
    return identities


class Lease:
    def __init__(self, config: Config):
        self.config = config
        self.identities = {
            "data": _identity(config.data_root, directory=True),
            "sentinel": _identity(config.primary_sentinel, directory=False),
            "database": _identity(config.database, directory=False),
            "incoming": _identity(config.incoming_root, directory=True),
            "originals": _identity(config.originals, directory=True),
            "runtime": _identity(config.runtime_root, directory=True),
        }
        self.identities.update(_secondary_identities(config))
        _read_sentinel(config)
        runtime = config.runtime_root.stat()
        if runtime.st_uid != os.geteuid() or stat.S_IMODE(runtime.st_mode) & 0o077:
            raise ActivationError("worker runtime permissions are unsafe")
        if not os.access(config.database, os.R_OK | os.W_OK):
            raise ActivationError("device backup database is not writable")
        if not os.access(config.incoming_root, os.R_OK | os.W_OK):
            raise ActivationError("device backup staging is not writable")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            self.descriptor = os.open(config.incoming_root / LOCK_NAME, flags, 0o600)
            details = os.fstat(self.descriptor)
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_nlink != 1
                or details.st_uid != os.geteuid()
            ):
                raise OSError
            os.fchmod(self.descriptor, 0o600)
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            incoming_descriptor = os.open(
                config.incoming_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                lock_path = os.stat(
                    LOCK_NAME,
                    dir_fd=incoming_descriptor,
                    follow_symlinks=False,
                )
            finally:
                os.close(incoming_descriptor)
            if (lock_path.st_dev, lock_path.st_ino) != (details.st_dev, details.st_ino):
                raise OSError
        except BlockingIOError:
            if hasattr(self, "descriptor"):
                os.close(self.descriptor)
            raise ActivationError("device backup housekeeping singleton is busy")
        except OSError as error:
            if hasattr(self, "descriptor"):
                os.close(self.descriptor)
            raise ActivationError("device backup housekeeping lock is unsafe") from error

    def validate(self) -> None:
        _read_sentinel(self.config)
        observed = {
            "data": _identity(self.config.data_root, directory=True),
            "sentinel": _identity(self.config.primary_sentinel, directory=False),
            "database": _identity(self.config.database, directory=False),
            "incoming": _identity(self.config.incoming_root, directory=True),
            "originals": _identity(self.config.originals, directory=True),
            "runtime": _identity(self.config.runtime_root, directory=True),
        }
        observed.update(_secondary_identities(self.config))
        if observed != self.identities:
            raise ActivationError("worker storage identity changed")
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ActivationError("device backup housekeeping lease was lost") from error

    def close(self) -> None:
        descriptor = getattr(self, "descriptor", -1)
        if descriptor >= 0:
            self.descriptor = -1
            os.close(descriptor)


@contextmanager
def database(config: Config):
    connection = sqlite3.connect(config.database, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _write_health(config: Config, value: dict) -> None:
    root = config.runtime_root
    _identity(root, directory=True)
    temporary = root / f".health-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, root / HEALTH_NAME)
    directory = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class HealthPublisher:
    def __init__(self, config: Config):
        self.config = config
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.last_progress = time.monotonic()
        self.reason = "starting"
        self.ready = False
        self.started = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.started = True

    def progress(self, reason: str = "working") -> None:
        with self.lock:
            self.last_progress = time.monotonic()
            self.reason = reason

    def succeeded(self) -> None:
        with self.lock:
            self.last_progress = time.monotonic()
            self.reason = "idle"
            self.ready = True

    def failed(self) -> None:
        with self.lock:
            self.last_progress = time.monotonic()
            self.reason = "task_failed"
            self.ready = False

    def _value(self) -> dict:
        with self.lock:
            silent = time.monotonic() - self.last_progress
            ready = self.ready and silent <= MAX_PROGRESS_SILENCE_SECONDS
            reason = self.reason if ready or not self.ready else "worker_stalled"
        return {
            "schema_version": 1,
            "worker": "david-pi-device-backup-worker",
            "ready": ready,
            "reason": reason,
            "updated_at": _utcnow(),
            "privacy": {
                "contains_paths": False,
                "contains_filenames": False,
                "contains_user_content": False,
                "contains_identities": False,
                "contains_secrets": False,
            },
        }

    def publish(self) -> None:
        _write_health(self.config, self._value())

    def _run(self) -> None:
        while not self.stop.wait(self.config.heartbeat_seconds):
            try:
                self.publish()
            except OSError:
                LOGGER.error("Device backup worker could not publish health")

    def close(self) -> None:
        self.failed()
        try:
            self.publish()
        finally:
            self.stop.set()
            if self.started:
                self.thread.join(timeout=self.config.heartbeat_seconds + 1)


def check_health(config: Config) -> bool:
    path = config.runtime_root / HEALTH_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        updated = _parse_time(value.get("updated_at"))
        return (
            value.get("schema_version") == 1
            and value.get("worker") == "david-pi-device-backup-worker"
            and value.get("ready") is True
            and value.get("privacy", {}).get("contains_user_content") is False
            and updated is not None
            and datetime.now(timezone.utc) - updated
            <= timedelta(seconds=HEALTH_MAX_AGE_SECONDS)
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def run_cycle(config: Config, lease: Lease, progress) -> dict:
    lease.validate()
    progress("cleaning_uploads")
    retired = cleanup_stale_upload_sessions(
        lambda: database(config), config.incoming_root
    )
    copied = 0
    if config.secondary_root is not None:
        for _ in range(config.secondary_batch_size):
            progress("copying_secondary")
            succeeded = secondary_verify_once(
                lambda: database(config),
                config.data_root,
                config.secondary_root,
                config.secondary_sentinel_id,
                progress=lambda: progress("copying_secondary"),
            )
            if not succeeded:
                break
            copied += 1
    lease.validate()
    progress("idle")
    return {"retired_uploads": retired, "secondary_copies": copied}


def run_forever(config: Config, stop: threading.Event | None = None) -> int:
    lease = Lease(config)
    stop = stop or threading.Event()
    publisher = HealthPublisher(config)

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        with database(config) as connection:
            initialize_device_backup(connection)
        publisher.start()
        while not stop.is_set():
            try:
                run_cycle(config, lease, publisher.progress)
                publisher.succeeded()
                publisher.publish()
            except ActivationError:
                publisher.failed()
                raise
            except Exception:
                publisher.failed()
                LOGGER.exception("Device backup housekeeping cycle failed")
            stop.wait(config.interval_seconds)
        return 0
    finally:
        publisher.close()
        lease.close()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--check-health", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config = Config.from_environment()
        if arguments.check_health:
            return 0 if check_health(config) else 1
        return run_forever(config)
    except ActivationError as error:
        LOGGER.error("Device backup worker activation blocked: %s", error)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
