"""Fail-closed, singleton David-Pi operational maintenance worker.

The protected family-data mount is read-only. Every worker mutation is made
through pinned directory descriptors in one dedicated operations mount. Saved
content retention supports only ``off`` and read-only ``preview`` modes.
"""

from __future__ import annotations

# Docker polls this command frequently under the worker's CPU limit. A probe
# reads the last validated heartbeat without constructing the worker again.
if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--check-health"]:
        from .worker_health import main as health_main

        raise SystemExit(health_main("maintenance"))

import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from .maintenance_observations import (
    ObservationError,
    history_values,
    load_server_status,
    metric_sample,
)
from .worker_health import PRIVACY as STATUS_PRIVACY, check_health as heartbeat_health


ALLOWED_ERROR_CODES = {
    "none",
    "configuration_invalid",
    "database_unavailable",
    "observation_incomplete",
    "observation_invalid",
    "observation_stale",
    "observation_unavailable",
    "sentinel_invalid",
    "singleton_busy",
    "storage_identity_changed",
    "task_failed",
}
PREVIEW_MODES = {"off", "preview"}
LOCK_NAME = "worker.lock"
STATUS_NAME = "status.json"
METRICS_NAME = "metrics.db"
METRICS_AUXILIARY = ("metrics.db-journal", "metrics.db-wal", "metrics.db-shm")
HEARTBEAT_NAME = "heartbeat.json"
OPERATIONS_COMPONENTS = (".david-pi-operations", "maintenance")


class MaintenanceError(RuntimeError):
    def __init__(self, code: str):
        if code not in ALLOWED_ERROR_CODES:
            code = "task_failed"
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Config:
    data_root: Path
    operations_root: Path
    operations_anchor: Path
    runtime_root: Path
    server_status_path: Path
    expected_data_id: str
    operations_uid: int
    metrics_enabled: bool = True
    metrics_interval: int = 300
    photo_retention_mode: str = "off"
    note_retention_mode: str = "off"
    upload_parts_mode: str = "off"
    preview_interval: int = 3600
    heartbeat_interval: int = 15
    status_interval: int = 900

    @property
    def sentinel(self) -> Path:
        return self.data_root / ".david-pi-storage"

    @property
    def lock_path(self) -> Path:
        return self.operations_root / LOCK_NAME

    @property
    def status_path(self) -> Path:
        return self.operations_root / STATUS_NAME

    @property
    def metrics_db(self) -> Path:
        return self.operations_root / METRICS_NAME

    @property
    def heartbeat_path(self) -> Path:
        return self.runtime_root / HEARTBEAT_NAME

    @classmethod
    def from_environment(cls) -> "Config":
        data_root = Path(os.environ.get("PHOTO_DATA", "/data"))
        return cls(
            data_root=data_root,
            operations_root=Path(
                os.environ.get("DAVID_PI_MAINTENANCE_STATE", "/maintenance-state")
            ),
            operations_anchor=Path(
                os.environ.get(
                    "DAVID_PI_MAINTENANCE_ANCHOR",
                    data_root.joinpath(*OPERATIONS_COMPONENTS),
                )
            ),
            runtime_root=Path(
                os.environ.get(
                    "DAVID_PI_MAINTENANCE_RUNTIME", "/run/david-pi-maintenance"
                )
            ),
            server_status_path=Path(
                os.environ.get(
                    "DAVID_PI_SERVER_STATUS", "/run/david-pi/server-status.json"
                )
            ),
            expected_data_id=os.environ.get(
                "DAVID_PI_DATA_ID", "david-pi-family-storage-v1"
            ),
            operations_uid=_integer(
                "DAVID_PI_MAINTENANCE_UID", 10002, 1, 2_147_483_647
            ),
            metrics_enabled=_boolean(
                "DAVID_PI_MAINTENANCE_METRICS_ENABLED", True
            ),
            metrics_interval=_integer(
                "DAVID_PI_MAINTENANCE_METRICS_INTERVAL", 300, 30, 86400
            ),
            photo_retention_mode=os.environ.get(
                "DAVID_PI_MAINTENANCE_PHOTO_RETENTION_MODE", "off"
            ).strip().lower(),
            note_retention_mode=os.environ.get(
                "DAVID_PI_MAINTENANCE_NOTE_RETENTION_MODE", "off"
            ).strip().lower(),
            upload_parts_mode=os.environ.get(
                "DAVID_PI_MAINTENANCE_UPLOAD_PARTS_MODE", "off"
            ).strip().lower(),
            preview_interval=_integer(
                "DAVID_PI_MAINTENANCE_PREVIEW_INTERVAL", 3600, 60, 86400
            ),
            heartbeat_interval=_integer(
                "DAVID_PI_MAINTENANCE_HEARTBEAT_INTERVAL", 15, 5, 30
            ),
            status_interval=_integer(
                "DAVID_PI_MAINTENANCE_STATUS_INTERVAL", 900, 60, 3600
            ),
        )


@dataclass(frozen=True)
class Task:
    name: str
    interval: int
    enabled: bool
    action: Callable[["Lease"], dict]


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.strip().lower() in {"1", "true", "yes"}:
        return True
    if value.strip().lower() in {"0", "false", "no"}:
        return False
    raise MaintenanceError("configuration_invalid")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError) as error:
        raise MaintenanceError("configuration_invalid") from error
    if not minimum <= value <= maximum:
        raise MaintenanceError("configuration_invalid")
    return value


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _safe_component(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise MaintenanceError("configuration_invalid")
    return name


def _open_directory(path: Path, expected_uid: int | None = None, private=False):
    descriptor = -1
    try:
        path_metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(path_metadata.st_mode):
            raise OSError
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if _identity(metadata) != _identity(path_metadata):
            raise OSError
        if expected_uid is not None and metadata.st_uid != expected_uid:
            raise OSError
        if private and stat.S_IMODE(metadata.st_mode) & 0o022:
            raise OSError
        return descriptor, metadata
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise MaintenanceError("storage_identity_changed") from error


class PinnedDirectory:
    """A no-symlink directory whose pathname and inode are continuously checked."""

    def __init__(self, path: Path, expected_uid: int, private=True):
        self.path = path
        self.expected_uid = expected_uid
        self.fd, metadata = _open_directory(path, expected_uid, private)
        self.identity = _identity(metadata)
        self.device = metadata.st_dev

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def revalidate(self) -> None:
        if self.fd < 0:
            raise MaintenanceError("storage_identity_changed")
        try:
            current = os.fstat(self.fd)
            path_metadata = self.path.lstat()
            if (
                self.path.is_symlink()
                or not stat.S_ISDIR(current.st_mode)
                or _identity(current) != self.identity
                or _identity(path_metadata) != self.identity
                or current.st_uid != self.expected_uid
                or stat.S_IMODE(current.st_mode) & 0o022
            ):
                raise OSError
        except OSError as error:
            raise MaintenanceError("storage_identity_changed") from error

    def regular_identity(self, name: str, allow_missing=False):
        name = _safe_component(name)
        self.revalidate()
        try:
            metadata = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing:
                return None
            raise MaintenanceError("storage_identity_changed")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != self.device
            or metadata.st_nlink != 1
            or metadata.st_uid != self.expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise MaintenanceError("storage_identity_changed")
        return _identity(metadata)

    def open_regular(self, name: str, flags: int, mode=0o640, create=False):
        name = _safe_component(name)
        before = self.regular_identity(name, allow_missing=create)
        open_flags = (
            flags
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        if create:
            open_flags |= os.O_CREAT
        descriptor = -1
        try:
            descriptor = os.open(name, open_flags, mode, dir_fd=self.fd)
            metadata = os.fstat(descriptor)
            pathname = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != self.device
                or metadata.st_nlink != 1
                or metadata.st_uid != self.expected_uid
                or _identity(metadata) != _identity(pathname)
                or (before is not None and _identity(metadata) != before)
            ):
                raise OSError
            if create or (flags & os.O_ACCMODE) != os.O_RDONLY:
                os.fchmod(descriptor, mode)
            self.revalidate()
            return descriptor, _identity(metadata)
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise MaintenanceError("storage_identity_changed") from error

    def atomic_json(self, name: str, value: dict, guard: Callable[[], None]) -> None:
        name = _safe_component(name)
        guard()
        self.regular_identity(name, allow_missing=True)
        temporary = f".{name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        temporary_identity = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o640,
                dir_fd=self.fd,
            )
            metadata = os.fstat(descriptor)
            temporary_identity = _identity(metadata)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != self.device
                or metadata.st_nlink != 1
                or metadata.st_uid != self.expected_uid
            ):
                raise OSError
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(value, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            guard()
            temp_path = os.stat(
                temporary, dir_fd=self.fd, follow_symlinks=False
            )
            if _identity(temp_path) != _identity(metadata) or temp_path.st_nlink != 1:
                raise OSError
            self.regular_identity(name, allow_missing=True)
            os.replace(
                temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd
            )
            if self.regular_identity(name) != _identity(metadata):
                raise OSError
            guard()
            os.fsync(self.fd)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                self.revalidate()
                current = os.stat(
                    temporary, dir_fd=self.fd, follow_symlinks=False
                )
                if (
                    temporary_identity is not None
                    and _identity(current) == temporary_identity
                    and current.st_nlink == 1
                    and current.st_uid == self.expected_uid
                ):
                    os.unlink(temporary, dir_fd=self.fd)
                    self.revalidate()
            except (OSError, MaintenanceError):
                pass
            raise


class DataGuard:
    """Pinned, read-only view of the protected family storage."""

    def __init__(self, config: Config):
        self.config = config
        self.fd, metadata = _open_directory(config.data_root)
        self.identity = _identity(metadata)
        self.device = metadata.st_dev

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def revalidate_root(self) -> None:
        try:
            current = os.fstat(self.fd)
            pathname = self.config.data_root.lstat()
            if (
                self.config.data_root.is_symlink()
                or _identity(current) != self.identity
                or _identity(pathname) != self.identity
                or not stat.S_ISDIR(current.st_mode)
            ):
                raise OSError
        except OSError as error:
            raise MaintenanceError("storage_identity_changed") from error

    def _open_components(self, components: tuple[str, ...]):
        parent = os.dup(self.fd)
        try:
            for component in components:
                component = _safe_component(component)
                descriptor = os.open(
                    component,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent,
                )
                metadata = os.fstat(descriptor)
                pathname = os.stat(
                    component, dir_fd=parent, follow_symlinks=False
                )
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_dev != self.device
                    or _identity(metadata) != _identity(pathname)
                ):
                    os.close(descriptor)
                    raise OSError
                os.close(parent)
                parent = descriptor
            self.revalidate_root()
            return parent
        except FileNotFoundError as error:
            os.close(parent)
            raise MaintenanceError("observation_unavailable") from error
        except (OSError, MaintenanceError) as error:
            os.close(parent)
            if isinstance(error, MaintenanceError):
                raise
            raise MaintenanceError("storage_identity_changed") from error

    def sentinel(self) -> None:
        self.revalidate_root()
        descriptor = -1
        try:
            descriptor = os.open(
                ".david-pi-storage",
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.fd,
            )
            metadata = os.fstat(descriptor)
            pathname = os.stat(
                ".david-pi-storage", dir_fd=self.fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != self.device
                or metadata.st_nlink != 1
                or _identity(metadata) != _identity(pathname)
                or metadata.st_size > 512
            ):
                raise OSError
            raw = os.read(descriptor, 513)
            if raw.decode("utf-8").strip() != self.config.expected_data_id:
                raise OSError
            after = os.stat(
                ".david-pi-storage", dir_fd=self.fd, follow_symlinks=False
            )
            if _identity(after) != _identity(metadata) or after.st_nlink != 1:
                raise OSError
            self.revalidate_root()
        except (OSError, UnicodeDecodeError) as error:
            raise MaintenanceError("sentinel_invalid") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def anchor_identity(self) -> tuple[int, int]:
        descriptor = self._open_components(OPERATIONS_COMPONENTS)
        try:
            return _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def read_only_database(self, components: tuple[str, ...]):
        parent = self._open_components(components[:-1])
        name = _safe_component(components[-1])
        descriptor = -1
        connection = None
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
            metadata = os.fstat(descriptor)
            pathname = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_dev != self.device
                or metadata.st_nlink != 1
                or _identity(metadata) != _identity(pathname)
            ):
                raise OSError
            uri = f"file:/proc/self/fd/{parent}/{name}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection, parent, descriptor, _identity(metadata)
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                connection.close()
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent)
            raise MaintenanceError("database_unavailable") from error

    def verify_read_database(
        self, parent: int, descriptor: int, name: str, identity: tuple[int, int]
    ) -> None:
        try:
            current = os.fstat(descriptor)
            pathname = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (
                _identity(current) != identity
                or _identity(pathname) != identity
                or current.st_dev != self.device
                or current.st_nlink != 1
            ):
                raise OSError
            self.revalidate_root()
        except OSError as error:
            raise MaintenanceError("storage_identity_changed") from error


class Lease:
    """Directory-inode singleton plus a checked diagnostic lock file."""

    def __init__(
        self,
        config: Config,
        data: DataGuard,
        operations: PinnedDirectory,
        runtime: PinnedDirectory,
        lock_fd: int,
        lock_identity: tuple[int, int],
    ):
        self.config = config
        self.data = data
        self.operations = operations
        self.runtime = runtime
        self.lock_fd = lock_fd
        self.lock_identity = lock_identity

    def validate(self) -> None:
        self.data.sentinel()
        self.operations.revalidate()
        self.runtime.revalidate()
        if self.data.anchor_identity() != self.operations.identity:
            raise MaintenanceError("storage_identity_changed")
        try:
            current = os.fstat(self.lock_fd)
            pathname = os.stat(
                LOCK_NAME, dir_fd=self.operations.fd, follow_symlinks=False
            )
            if (
                _identity(current) != self.lock_identity
                or _identity(pathname) != self.lock_identity
                or current.st_nlink != 1
                or current.st_uid != self.config.operations_uid
                or stat.S_IMODE(current.st_mode) & 0o022
            ):
                raise OSError
        except OSError as error:
            raise MaintenanceError("storage_identity_changed") from error

    def close(self) -> None:
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.lock_fd)
        except OSError:
            pass
        try:
            fcntl.flock(self.operations.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        self.runtime.close()
        self.operations.close()
        self.data.close()


def validate_config(config: Config) -> None:
    if not config.expected_data_id or len(config.expected_data_id) > 200:
        raise MaintenanceError("configuration_invalid")
    if config.operations_uid != os.geteuid():
        raise MaintenanceError("configuration_invalid")
    if any(
        mode not in PREVIEW_MODES
        for mode in (
            config.photo_retention_mode,
            config.note_retention_mode,
            config.upload_parts_mode,
        )
    ):
        raise MaintenanceError("configuration_invalid")
    if not 30 <= config.metrics_interval <= 86400:
        raise MaintenanceError("configuration_invalid")
    if not 60 <= config.preview_interval <= 86400:
        raise MaintenanceError("configuration_invalid")
    if not 5 <= config.heartbeat_interval <= 30:
        raise MaintenanceError("configuration_invalid")
    if not 60 <= config.status_interval <= 3600:
        raise MaintenanceError("configuration_invalid")
    paths = (
        config.data_root,
        config.operations_root,
        config.operations_anchor,
        config.runtime_root,
        config.server_status_path,
    )
    if any(not path.is_absolute() for path in paths):
        raise MaintenanceError("configuration_invalid")
    expected_anchor = config.data_root.joinpath(*OPERATIONS_COMPONENTS)
    if config.operations_anchor != expected_anchor:
        raise MaintenanceError("configuration_invalid")
    if config.operations_root == config.data_root or config.runtime_root == config.data_root:
        raise MaintenanceError("configuration_invalid")


def acquire_singleton(config: Config):
    """Pin both mounts and flock their immutable operations-directory inode."""
    validate_config(config)
    data = DataGuard(config)
    operations = runtime = None
    lock_fd = -1
    try:
        data.sentinel()
        operations = PinnedDirectory(
            config.operations_root, config.operations_uid, private=True
        )
        if data.anchor_identity() != operations.identity:
            raise MaintenanceError("storage_identity_changed")
        runtime = PinnedDirectory(
            config.runtime_root, config.operations_uid, private=True
        )
        try:
            fcntl.flock(operations.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            runtime.close()
            operations.close()
            data.close()
            return None
        lock_fd, lock_identity = operations.open_regular(
            LOCK_NAME, os.O_RDWR, create=True
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise MaintenanceError("singleton_busy") from error
        lease = Lease(config, data, operations, runtime, lock_fd, lock_identity)
        lease.validate()
        return lease
    except Exception:
        if lock_fd >= 0:
            os.close(lock_fd)
        if runtime is not None:
            runtime.close()
        if operations is not None:
            operations.close()
        data.close()
        raise


def _close_read_database(connection, parent, descriptor) -> None:
    connection.close()
    os.close(descriptor)
    os.close(parent)


def _table_columns(connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as error:
        raise MaintenanceError("database_unavailable") from error


def preview_photo_retention(lease: Lease, now: datetime | None = None) -> dict:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    connection, parent, descriptor, identity = lease.data.read_only_database(
        ("photos.db",)
    )
    try:
        if "deleted_at" not in _table_columns(connection, "photos"):
            raise MaintenanceError("database_unavailable")
        count = connection.execute(
            "SELECT COUNT(*) FROM photos WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff.isoformat(),),
        ).fetchone()[0]
        lease.data.verify_read_database(parent, descriptor, "photos.db", identity)
    except sqlite3.Error as error:
        raise MaintenanceError("database_unavailable") from error
    finally:
        _close_read_database(connection, parent, descriptor)
    return {"candidate_count": int(count), "mode": "preview"}


def preview_note_retention(lease: Lease, now: datetime | None = None) -> dict:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    connection, parent, descriptor, identity = lease.data.read_only_database(
        ("platform", "notes.db")
    )
    try:
        if "deleted_at" not in _table_columns(connection, "notes"):
            raise MaintenanceError("database_unavailable")
        count = connection.execute(
            "SELECT COUNT(*) FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff.isoformat(),),
        ).fetchone()[0]
        lease.data.verify_read_database(parent, descriptor, "notes.db", identity)
    except sqlite3.Error as error:
        raise MaintenanceError("database_unavailable") from error
    finally:
        _close_read_database(connection, parent, descriptor)
    return {"candidate_count": int(count), "mode": "preview"}


def preview_upload_parts(lease: Lease, now: float | None = None) -> dict:
    cutoff = (time.time() if now is None else now) - 24 * 3600
    count = 0
    for components in (("incoming",), ("tmp", "uploads"), ("files", "incoming")):
        try:
            descriptor = lease.data._open_components(components)
        except MaintenanceError as error:
            if error.code == "observation_unavailable":
                continue
            raise
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if not entry.name.endswith(".part"):
                        continue
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                        if (
                            stat.S_ISREG(metadata.st_mode)
                            and metadata.st_dev == lease.data.device
                            and metadata.st_mtime < cutoff
                        ):
                            count += 1
                    except OSError:
                        continue
            lease.data.revalidate_root()
        finally:
            os.close(descriptor)
    return {"candidate_count": count, "mode": "preview"}


def _validate_metrics_files(
    lease: Lease, expected_metrics_identity: tuple[int, int] | None = None
) -> None:
    lease.validate()
    identity = lease.operations.regular_identity(METRICS_NAME, allow_missing=True)
    if expected_metrics_identity is not None and identity != expected_metrics_identity:
        raise MaintenanceError("storage_identity_changed")
    for name in METRICS_AUXILIARY:
        lease.operations.regular_identity(name, allow_missing=True)


def write_metric_sample(lease: Lease, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    payload = load_server_status(lease.config.server_status_path, now=now)
    sample = metric_sample(payload)
    history = history_values(payload)
    _validate_metrics_files(lease)
    uri = f"file:/proc/self/fd/{lease.operations.fd}/{METRICS_NAME}?mode=rwc"
    connection = None
    metrics_descriptor = -1
    metrics_identity = None
    try:
        metrics_descriptor, metrics_identity = lease.operations.open_regular(
            METRICS_NAME, os.O_RDWR, create=True
        )
        _validate_metrics_files(lease, metrics_identity)
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        _validate_metrics_files(lease, metrics_identity)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=DELETE")
        _validate_metrics_files(lease, metrics_identity)
        connection.execute("BEGIN IMMEDIATE")
        _validate_metrics_files(lease, metrics_identity)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS system_metrics ("
            "timestamp INTEGER PRIMARY KEY, cpu REAL NOT NULL, memory REAL NOT NULL, "
            "temperature REAL, disk_used REAL NOT NULL, load1 REAL NOT NULL)"
        )
        _validate_metrics_files(lease, metrics_identity)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(system_metrics)")
        }
        for name in (
            "swap",
            "root_disk_used",
            "container_memory",
            "health_latency",
            "backup_state",
            "queue_depth",
        ):
            if name not in columns:
                lease.validate()
                connection.execute(
                    f"ALTER TABLE system_metrics ADD COLUMN {name} REAL"
                )
                _validate_metrics_files(lease, metrics_identity)
        lease.validate()
        connection.execute(
            """INSERT OR REPLACE INTO system_metrics
               (timestamp,cpu,memory,temperature,disk_used,load1,swap,root_disk_used,
                container_memory,health_latency,backup_state,queue_depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sample["timestamp"],
                sample["cpu"],
                sample["memory"],
                sample["temperature"],
                sample["disk_used"],
                sample["load1"],
                history.get("swap"),
                history.get("root_disk_used"),
                history.get("container_memory"),
                history.get("health_latency"),
                history.get("backup_state"),
                history.get("queue_depth"),
            ),
        )
        _validate_metrics_files(lease, metrics_identity)
        connection.execute(
            "DELETE FROM system_metrics WHERE timestamp < ?",
            (sample["timestamp"] - 30 * 86400,),
        )
        _validate_metrics_files(lease, metrics_identity)
        connection.commit()
        _validate_metrics_files(lease, metrics_identity)
    except sqlite3.Error as error:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        raise MaintenanceError("database_unavailable") from error
    finally:
        try:
            if connection is not None:
                connection.close()
        finally:
            if metrics_descriptor >= 0:
                try:
                    _validate_metrics_files(lease, metrics_identity)
                finally:
                    os.close(metrics_descriptor)
    return {"sample_written": True}


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _safe_result(result: dict | None) -> dict:
    result = result or {}
    safe = {}
    if isinstance(result.get("candidate_count"), int):
        safe["candidate_count"] = max(
            0, min(result["candidate_count"], 1_000_000_000)
        )
    if result.get("mode") == "preview":
        safe["mode"] = "preview"
    if result.get("sample_written") is True:
        safe["sample_written"] = True
    return safe


def _error_code(error: Exception) -> str:
    if isinstance(error, (MaintenanceError, ObservationError)):
        return error.code if error.code in ALLOWED_ERROR_CODES else "task_failed"
    return "task_failed"


class Worker:
    def __init__(self, config: Config, tasks: list[Task] | None = None):
        self.config = config
        self.started_at = time.time()
        self.next_due: dict[str, float] = {}
        self.task_status: dict[str, dict] = {}
        self.tasks = tasks or [
            Task(
                "metrics",
                config.metrics_interval,
                config.metrics_enabled,
                lambda lease: write_metric_sample(lease),
            ),
            Task(
                "photo_retention",
                config.preview_interval,
                config.photo_retention_mode == "preview",
                preview_photo_retention,
            ),
            Task(
                "note_retention",
                config.preview_interval,
                config.note_retention_mode == "preview",
                preview_note_retention,
            ),
            Task(
                "upload_parts",
                config.preview_interval,
                config.upload_parts_mode == "preview",
                preview_upload_parts,
            ),
        ]
        for task in self.tasks:
            if (
                task.name in self.next_due
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", task.name)
                or not 1 <= task.interval <= 86400
                or not callable(task.action)
            ):
                raise MaintenanceError("configuration_invalid")
            self.next_due[task.name] = 0.0
            self.task_status[task.name] = {
                "state": "pending" if task.enabled else "disabled",
                "error_code": "none",
            }

    def run_due(
        self,
        lease: Lease,
        monotonic_now: float | None = None,
        wall_now: float | None = None,
    ) -> dict:
        monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
        wall_now = time.time() if wall_now is None else wall_now
        for task in self.tasks:
            if not task.enabled or monotonic_now < self.next_due[task.name]:
                continue
            started = time.monotonic()
            status = {
                "state": "ok",
                "error_code": "none",
                "last_started_at": _iso(wall_now),
            }
            try:
                lease.validate()
                status.update(_safe_result(task.action(lease)))
            except Exception as error:
                status.update(
                    {"state": "unavailable", "error_code": _error_code(error)}
                )
            status["last_completed_at"] = _iso(time.time())
            status["duration_ms"] = max(
                0, int((time.monotonic() - started) * 1000)
            )
            self.task_status[task.name] = status
            retry = (
                self.config.heartbeat_interval
                if status["state"] != "ok"
                else task.interval
            )
            self.next_due[task.name] = monotonic_now + retry
        states = [
            value["state"]
            for value in self.task_status.values()
            if value["state"] != "disabled"
        ]
        overall = (
            "healthy" if states and all(state == "ok" for state in states) else "degraded"
        )
        return {
            "schema_version": 2,
            "worker": "david-pi-maintenance",
            "state": overall,
            "started_at": _iso(self.started_at),
            "updated_at": _iso(time.time()),
            "privacy": STATUS_PRIVACY,
            "tasks": self.task_status,
        }


def _status_fingerprint(status: dict) -> str:
    material = {
        "state": status["state"],
        "tasks": {
            name: {
                key: value[key]
                for key in (
                    "state",
                    "error_code",
                    "candidate_count",
                    "mode",
                    "sample_written",
                )
                if key in value
            }
            for name, value in status["tasks"].items()
        },
    }
    return json.dumps(material, separators=(",", ":"), sort_keys=True)


def status_write_due(
    status: dict,
    last_fingerprint: str | None,
    last_write: float,
    monotonic_now: float,
    interval: int,
) -> tuple[bool, str]:
    """Decide persistence without treating heartbeat timestamps as state changes."""
    fingerprint = _status_fingerprint(status)
    return (
        fingerprint != last_fingerprint
        or monotonic_now - last_write >= interval,
        fingerprint,
    )


def publish_persistent_status(lease: Lease, status: dict) -> None:
    lease.operations.atomic_json(STATUS_NAME, status, lease.validate)


def publish_heartbeat(lease: Lease, status: dict) -> None:
    lease.runtime.atomic_json(
        HEARTBEAT_NAME,
        {
            "schema_version": 2,
            "worker": "david-pi-maintenance",
            "state": status["state"],
            "updated_at": status["updated_at"],
            "privacy": STATUS_PRIVACY,
        },
        lease.validate,
    )


def run_once(config: Config, tasks: list[Task] | None = None) -> int:
    """Run one cycle; invalid storage creates no worker file."""
    lease = acquire_singleton(config)
    if lease is None:
        return 75
    try:
        status = Worker(config, tasks).run_due(lease)
        publish_persistent_status(lease, status)
        publish_heartbeat(lease, status)
        return 0
    finally:
        lease.close()


def run_forever(config: Config, stop: Event | None = None) -> int:
    lease = acquire_singleton(config)
    if lease is None:
        return 75
    stop = stop or Event()
    worker = Worker(config)
    last_fingerprint = None
    last_status_write = 0.0
    try:
        while not stop.is_set():
            lease.validate()
            status = worker.run_due(lease)
            current = time.monotonic()
            due, fingerprint = status_write_due(
                status,
                last_fingerprint,
                last_status_write,
                current,
                config.status_interval,
            )
            if due:
                publish_persistent_status(lease, status)
                last_fingerprint = fingerprint
                last_status_write = current
            publish_heartbeat(lease, status)
            stop.wait(config.heartbeat_interval)
        return 0
    finally:
        lease.close()


def check_health(
    config: Config, maximum_age: int = 90, now: float | None = None
) -> bool:
    try:
        validate_config(config)
        return heartbeat_health(
            "maintenance", config.runtime_root, config.operations_uid,
            maximum_age=maximum_age, now=now,
        )
    except (ValueError, TypeError, MaintenanceError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run source-safe David-Pi maintenance"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check-health", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        config = Config.from_environment()
        if arguments.check_health:
            return 0 if check_health(config) else 1
        if arguments.once:
            return run_once(config)
        stop = Event()
        signal.signal(signal.SIGTERM, lambda _signal, _frame: stop.set())
        signal.signal(signal.SIGINT, lambda _signal, _frame: stop.set())
        return run_forever(config, stop)
    except Exception as error:
        print(f"david-pi-maintenance: {_error_code(error)}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
