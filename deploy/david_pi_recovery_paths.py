#!/usr/bin/env python3
"""Fail-closed path gates for David-Pi snapshot creation and publication."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import errno
import os
import posixpath
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import david_pi_fd_tree as fd_tree
from david_pi_fd_tree import (
    basename_glob_excluded,
    clear_directory_fd as fd_clear_directory,
    copy_evidence_tree,
    create_child_file as fd_create_child_file,
    data_backup_excluded,
    fd_mount_id,
    format_identity,
    inventory_tree_fd,
    open_child_directory as fd_open_child_directory,
    open_child_file as fd_open_child_file,
    open_directory_chain,
    parse_identity,
    pin_child as fd_pin_child,
    relative_parts,
    require_path_binding,
    root_identity,
)


SNAPSHOT_ID = re.compile(r"^20[0-9]{6}T[0-9]{6}Z$")
WORK_ID = re.compile(r"^\.incomplete-(20[0-9]{6}T[0-9]{6}Z)$")
EXPECTED_CHILDREN = ("data", "config", "databases")
STATUS_FILES = {"last-attempt.json", "last-success.json"}
WORK_METADATA_FILES = {"MANIFEST.txt"}
MAX_ATOMIC_WRITE_BYTES = 1024 * 1024


def after_latest_symlink_create(_snapshots_fd, _temporary_name):
    """Test seam after a candidate latest link is created but before use."""

# Linux openat2(2) is required for mutable-tree operations. A same-filesystem
# bind mount has the same st_dev as its parent, so st_dev alone cannot prove
# that a destructive traversal stayed inside the snapshot work tree.
SYS_OPENAT2 = 437
RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
_LIBC = ctypes.CDLL(None, use_errno=True)


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_ulonglong),
        ("mode", ctypes.c_ulonglong),
        ("resolve", ctypes.c_ulonglong),
    ]


def _openat2(directory_fd, relative, flags, *, mode=0, allow_final_symlink=False):
    """Open one relative component without crossing mounts or link redirects."""
    if (
        not isinstance(relative, str)
        or not relative
        or "/" in relative
        or relative in {".", ".."}
    ):
        raise ValueError("descriptor traversal received an unsafe path component")
    resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV
    if not allow_final_symlink:
        resolve |= RESOLVE_NO_SYMLINKS
    how = _OpenHow(flags=flags | os.O_CLOEXEC, mode=mode, resolve=resolve)
    descriptor = _LIBC.syscall(
        SYS_OPENAT2,
        directory_fd,
        os.fsencode(relative),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if descriptor < 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOSYS:
            raise RuntimeError("openat2 is required for protected recovery traversal")
        if error_number == errno.EXDEV:
            raise ValueError("protected recovery traversal crossed a mount boundary")
        if error_number in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("protected recovery traversal encountered a link redirect")
        raise OSError(error_number, os.strerror(error_number), relative)
    return descriptor


def _pin_child(directory_fd, name):
    """Pin any direct child, including a final symlink, without crossing mounts."""
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW
    return _openat2(
        directory_fd,
        name,
        flags,
        allow_final_symlink=True,
    )


def _open_child_directory(directory_fd, name):
    return _openat2(
        directory_fd,
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


def _strict_utc_timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ValueError(f"{label} timestamp is invalid") from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{label} timestamp is not canonical UTC")
    return value


def _strict_snapshot_id(value, label="snapshot id"):
    if not isinstance(value, str) or not SNAPSHOT_ID.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if parsed.strftime("%Y%m%dT%H%M%SZ") != value:
        raise ValueError(f"{label} is not canonical UTC")
    return value


def _absolute(path, label):
    path = Path(path)
    if not path.is_absolute() or Path(os.path.normpath(path)) != path:
        raise ValueError(f"{label} must be an absolute canonical path")
    return path


def _metadata(path, label, *, kind, uid, device=None, mode=None):
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"{label} is missing") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"{label} must not be a symlink")
    expected_kind = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        raise ValueError(f"{label} must be a {kind}")
    if uid is not None and metadata.st_uid != uid:
        raise PermissionError(f"{label} has an unsafe owner")
    if device is not None and metadata.st_dev != device:
        raise ValueError(f"{label} crossed the protected filesystem")
    permissions = stat.S_IMODE(metadata.st_mode)
    if mode is not None and permissions != mode:
        raise PermissionError(f"{label} must have mode {mode:04o}")
    if permissions & 0o022:
        raise PermissionError(f"{label} must not be group/world writable")
    return metadata


def _direct_child(parent, child, expected_name, label):
    if child.parent != parent or child.name != expected_name:
        raise ValueError(f"{label} is not the expected direct child")


def _canonical_existing(path, label):
    if path.resolve(strict=True) != path:
        raise ValueError(f"{label} is not canonically contained")


def _safe_relative(value, label):
    value = str(value)
    relative = Path(value)
    if (
        not value
        or relative.is_absolute()
        or value != relative.as_posix()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} must be a canonical relative path")
    if relative.parts[0] not in EXPECTED_CHILDREN:
        raise ValueError(f"{label} must remain inside a snapshot data tree")
    return relative


def _atomic_write(parent, name, contents, *, allowed_names, uid=None, device=None):
    """Atomically replace one allowlisted direct child without following links."""
    uid = os.geteuid() if uid is None else uid
    parent = _absolute(parent, "atomic output parent")
    if name not in allowed_names or Path(name).name != name:
        raise ValueError("atomic output name is not allowed")
    if not isinstance(contents, bytes) or len(contents) > MAX_ATOMIC_WRITE_BYTES:
        raise ValueError("atomic output is invalid or too large")
    parent_metadata = _metadata(
        parent,
        "atomic output parent",
        kind="directory",
        uid=uid,
        device=device,
    )
    _canonical_existing(parent, "atomic output parent")
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary_name = None
    temporary_fd = None
    try:
        opened_parent = os.fstat(directory_fd)
        if (
            opened_parent.st_dev != parent_metadata.st_dev
            or opened_parent.st_ino != parent_metadata.st_ino
            or opened_parent.st_uid != uid
            or stat.S_IMODE(opened_parent.st_mode)
            != stat.S_IMODE(parent_metadata.st_mode)
        ):
            raise RuntimeError("atomic output parent was replaced")
        for _ in range(32):
            candidate = f".{name}.{secrets.token_hex(16)}.tmp"
            try:
                temporary_fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None:
            raise FileExistsError("could not allocate a collision-free atomic output")
        temporary_metadata = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != uid
            or temporary_metadata.st_dev != parent_metadata.st_dev
            or temporary_metadata.st_nlink != 1
        ):
            raise PermissionError("atomic temporary output is unsafe")
        offset = 0
        while offset < len(contents):
            written = os.write(temporary_fd, contents[offset:])
            if written <= 0:
                raise OSError("short atomic output write")
            offset += written
        os.fchmod(temporary_fd, 0o600)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = None

        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != uid
            or existing.st_dev != parent_metadata.st_dev
            or existing.st_nlink != 1
        ):
            raise PermissionError("existing atomic output is unsafe")
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        published = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_uid != uid
            or published.st_dev != parent_metadata.st_dev
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_size != len(contents)
        ):
            raise RuntimeError("published atomic output failed validation")
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def write_destination_file(destination, name, contents):
    destination = _absolute(destination, "backup destination")
    metadata = _metadata(
        destination, "backup destination", kind="directory", uid=os.geteuid()
    )
    _canonical_existing(destination, "backup destination")
    _atomic_write(
        destination,
        name,
        contents,
        allowed_names=STATUS_FILES,
        device=metadata.st_dev,
    )


def write_work_file(destination, snapshots, work, name, contents):
    _validate_work(destination, snapshots, work)
    _atomic_write(
        work,
        name,
        contents,
        allowed_names=WORK_METADATA_FILES,
        device=Path(work).lstat().st_dev,
    )


def validate_snapshot_root(destination, snapshots, uid=None):
    uid = os.geteuid() if uid is None else uid
    destination = _absolute(destination, "backup destination")
    snapshots = _absolute(snapshots, "snapshot directory")
    _direct_child(destination, snapshots, "snapshots", "snapshot directory")
    destination_metadata = _metadata(
        destination, "backup destination", kind="directory", uid=uid
    )
    _canonical_existing(destination, "backup destination")
    _metadata(
        snapshots,
        "snapshot directory",
        kind="directory",
        uid=uid,
        device=destination_metadata.st_dev,
    )
    _canonical_existing(snapshots, "snapshot directory")
    return destination_metadata.st_dev


def prepare_snapshot_root(destination, snapshots):
    destination = _absolute(destination, "backup destination")
    snapshots = _absolute(snapshots, "snapshot directory")
    _direct_child(destination, snapshots, "snapshots", "snapshot directory")
    destination_metadata = _metadata(
        destination, "backup destination", kind="directory", uid=os.geteuid()
    )
    destination_fd = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        opened_destination = os.fstat(destination_fd)
        if (
            opened_destination.st_dev != destination_metadata.st_dev
            or opened_destination.st_ino != destination_metadata.st_ino
        ):
            raise RuntimeError("backup destination was replaced")
        try:
            os.mkdir("snapshots", mode=0o700, dir_fd=destination_fd)
        except FileExistsError:
            pass
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
    validate_snapshot_root(destination, snapshots)


def validate_sentinel(parent, sentinel, expected, label, uid=None):
    uid = os.geteuid() if uid is None else uid
    parent = _absolute(parent, f"{label} parent")
    sentinel = _absolute(sentinel, label)
    _direct_child(parent, sentinel, sentinel.name, label)
    parent_metadata = parent.lstat()
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise ValueError(f"{label} parent must be a real directory")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            descriptor = os.open(
                sentinel.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError(f"{label} must not be a symlink") from error
            raise
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{label} must be a file")
            if metadata.st_uid != uid:
                raise PermissionError(f"{label} has an unsafe owner")
            if metadata.st_dev != parent_metadata.st_dev or metadata.st_nlink != 1:
                raise ValueError(f"{label} is not uniquely contained")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PermissionError(f"{label} must be private")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                contents = handle.read(len(expected) + 3)
            if contents != expected.encode("ascii") + b"\n":
                raise ValueError(f"{label} content is not exact")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _validate_work(destination, snapshots, work, uid=None):
    uid = os.geteuid() if uid is None else uid
    device = validate_snapshot_root(destination, snapshots, uid)
    work = _absolute(work, "working snapshot")
    match = WORK_ID.fullmatch(work.name)
    if work.parent != snapshots or not match:
        raise ValueError("working snapshot must be a direct timestamped child")
    _strict_snapshot_id(match.group(1), "working snapshot id")
    _metadata(
        work, "working snapshot", kind="directory", uid=uid, device=device, mode=0o700
    )
    _canonical_existing(work, "working snapshot")
    marker = work / ".started-at"
    marker_metadata = _metadata(
        marker, "snapshot start marker", kind="file", uid=uid, device=device, mode=0o600
    )
    if marker.parent != work or marker_metadata.st_nlink != 1:
        raise ValueError("snapshot start marker is unsafe")
    raw_started = marker.read_bytes()
    if not raw_started.endswith(b"\n") or raw_started.count(b"\n") != 1:
        raise ValueError("snapshot start marker content is not exact")
    try:
        started_at = raw_started[:-1].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("snapshot start marker is not ASCII") from error
    _strict_utc_timestamp(started_at, "snapshot start marker")
    for name in EXPECTED_CHILDREN:
        child = work / name
        _direct_child(work, child, name, f"snapshot {name} directory")
        _metadata(
            child,
            f"snapshot {name} directory",
            kind="directory",
            uid=uid,
            device=device,
            mode=0o700,
        )
        _canonical_existing(child, f"snapshot {name} directory")
    return started_at


def prepare_work(destination, snapshots, work, started_at=None, resume=False):
    destination = _absolute(destination, "backup destination")
    snapshots = _absolute(snapshots, "snapshot directory")
    work = _absolute(work, "working snapshot")
    validate_snapshot_root(destination, snapshots)
    match = WORK_ID.fullmatch(work.name)
    if work.parent != snapshots or not match:
        raise ValueError("working snapshot must be a direct timestamped child")
    _strict_snapshot_id(match.group(1), "working snapshot id")
    if not resume:
        _strict_utc_timestamp(started_at, "snapshot start")
    destination_fd = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    snapshots_fd = _open_child_directory(destination_fd, "snapshots")
    try:
        if not resume:
            if not started_at:
                raise ValueError("new snapshot requires a start timestamp")
            os.mkdir(work.name, mode=0o700, dir_fd=snapshots_fd)
        work_fd = _open_child_directory(snapshots_fd, work.name)
        try:
            if not resume:
                marker_fd = fd_create_child_file(
                    work_fd,
                    ".started-at",
                    flags=os.O_WRONLY,
                    mode=0o600,
                )
                try:
                    marker_contents = started_at.encode("ascii") + b"\n"
                    offset = 0
                    while offset < len(marker_contents):
                        written = os.write(marker_fd, marker_contents[offset:])
                        if written <= 0:
                            raise OSError("short snapshot start marker write")
                        offset += written
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
            for name in EXPECTED_CHILDREN:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=work_fd)
                except FileExistsError:
                    if not resume:
                        raise
            os.fsync(work_fd)
        finally:
            os.close(work_fd)
        os.fsync(snapshots_fd)
    finally:
        os.close(snapshots_fd)
        os.close(destination_fd)
    return _validate_work(destination, snapshots, work)


def validate_work(destination, snapshots, work, final):
    snapshots = _absolute(snapshots, "snapshot directory")
    work = _absolute(work, "working snapshot")
    final = _absolute(final, "final snapshot")
    started_at = _validate_work(destination, snapshots, work)
    match = WORK_ID.fullmatch(work.name)
    if final.parent != snapshots or final.name != match.group(1):
        raise ValueError("final snapshot is not the matching direct timestamp child")
    _strict_snapshot_id(final.name, "final snapshot id")
    try:
        final.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("final snapshot path already exists")
    return started_at


def _relative_directory_fd(work, relative, *, create=False):
    """Traverse a work-relative directory with openat and no symlink following."""
    relative = _safe_relative(relative, "copy target")
    snapshots_fd = os.open(
        Path(work).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        work_fd = _open_child_directory(snapshots_fd, Path(work).name)
    finally:
        os.close(snapshots_fd)
    current_fd = work_fd
    try:
        for part in relative.parts:
            try:
                next_fd = _open_child_directory(current_fd, part)
            except FileNotFoundError:
                if not create:
                    raise ValueError("copy target directory is missing") from None
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                os.fsync(current_fd)
                next_fd = _open_child_directory(current_fd, part)
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("copy target traverses a symlink or non-directory") from error
                raise
            if current_fd != work_fd:
                os.close(current_fd)
            current_fd = next_fd
        return work_fd, current_fd, relative
    except Exception:
        if current_fd != work_fd:
            os.close(current_fd)
        os.close(work_fd)
        raise


def prepare_copy_target(destination, snapshots, work, relative):
    _validate_work(destination, snapshots, work)
    device = Path(work).lstat().st_dev
    work_fd, target_fd, relative = _relative_directory_fd(work, relative, create=True)
    try:
        metadata = os.fstat(target_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != device
            or metadata.st_uid != os.geteuid()
        ):
            raise PermissionError("copy target directory is unsafe")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise PermissionError("copy target directory must not be group/world writable")
        os.fsync(target_fd)
    finally:
        if target_fd != work_fd:
            os.close(target_fd)
        os.close(work_fd)
    return Path(work) / relative


def validate_mutable_tree(
    destination,
    snapshots,
    work,
    relative,
    *,
    allow_file_symlinks=False,
):
    """Reject redirects and shared regular inodes before a copy mutates a work tree."""
    _validate_work(destination, snapshots, work)
    device = Path(work).lstat().st_dev
    work_fd, target_fd, relative = _relative_directory_fd(work, relative, create=False)
    try:
        target_metadata = os.fstat(target_fd)
        if target_metadata.st_dev != device:
            raise ValueError("mutable tree crossed the protected filesystem")
        _validate_mutable_directory_fd(
            target_fd,
            device,
            relative.as_posix(),
            allow_file_symlinks=allow_file_symlinks,
        )
    finally:
        if target_fd != work_fd:
            os.close(target_fd)
        os.close(work_fd)
    return Path(work) / relative


def _validate_mutable_directory_fd(
    directory_fd,
    device,
    relative,
    *,
    allow_file_symlinks,
):
    directory_metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(directory_metadata.st_mode) or directory_metadata.st_dev != device:
        raise ValueError("mutable tree contains a redirected directory")
    for name in sorted(os.listdir(directory_fd)):
        pinned_fd = _pin_child(directory_fd, name)
        try:
            metadata = os.fstat(pinned_fd)
            relative_child = f"{relative}/{name}" if relative else name
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = _open_child_directory(directory_fd, name)
                try:
                    _validate_mutable_directory_fd(
                        child_fd,
                        device,
                        relative_child,
                        allow_file_symlinks=allow_file_symlinks,
                    )
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISLNK(metadata.st_mode):
                if not allow_file_symlinks:
                    raise ValueError("mutable tree contains a symlink")
                target_value = os.readlink(name, dir_fd=directory_fd)
                normalized = posixpath.normpath(
                    posixpath.join(posixpath.dirname(relative_child), target_value)
                )
                if (
                    metadata.st_dev != device
                    or metadata.st_nlink != 1
                    or not target_value
                    or os.path.isabs(target_value)
                    or normalized == ".."
                    or normalized.startswith("../")
                ):
                    raise ValueError("mutable tree contains an unsafe symlink")
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != device:
                raise ValueError("mutable tree contains an unsafe entry")
            if metadata.st_nlink != 1:
                raise ValueError("mutable tree contains a shared inode")
        finally:
            os.close(pinned_fd)


def _clear_directory_fd(directory_fd, device):
    """Clear a prevalidated directory through pinned descriptors only."""
    for name in sorted(os.listdir(directory_fd)):
        pinned_fd = _pin_child(directory_fd, name)
        try:
            metadata = os.fstat(pinned_fd)
            if metadata.st_dev != device:
                raise ValueError("mutable tree crossed the protected filesystem")
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = _open_child_directory(directory_fd, name)
                try:
                    _clear_directory_fd(child_fd, device)
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise ValueError("mutable tree contains a shared inode")
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise ValueError("mutable tree contains an unsafe entry")
        finally:
            os.close(pinned_fd)
    os.fsync(directory_fd)


def clear_mutable_tree(destination, snapshots, work, relative):
    """Validate the whole tree, then clear it without pathname traversal."""
    _validate_work(destination, snapshots, work)
    device = Path(work).lstat().st_dev
    work_fd, target_fd, relative = _relative_directory_fd(work, relative, create=False)
    try:
        _validate_mutable_directory_fd(
            target_fd,
            device,
            relative.as_posix(),
            allow_file_symlinks=False,
        )
        _clear_directory_fd(target_fd, device)
    finally:
        if target_fd != work_fd:
            os.close(target_fd)
        os.close(work_fd)
    return Path(work) / relative


def resecure_work_roots(destination, snapshots, work):
    """Restore the private direct-child modes that rsync changes to source modes."""
    uid = os.geteuid()
    device = validate_snapshot_root(destination, snapshots, uid)
    work = _absolute(work, "working snapshot")
    if work.parent != snapshots or not WORK_ID.fullmatch(work.name):
        raise ValueError("working snapshot must be a direct timestamped child")
    _metadata(
        work, "working snapshot", kind="directory", uid=uid, device=device, mode=0o700
    )
    snapshots_fd = os.open(
        snapshots, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    )
    try:
        work_fd = _open_child_directory(snapshots_fd, work.name)
    finally:
        os.close(snapshots_fd)
    try:
        for name in EXPECTED_CHILDREN:
            child_fd = _open_child_directory(work_fd, name)
            try:
                metadata = os.fstat(child_fd)
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_dev != device:
                    raise ValueError(f"snapshot {name} directory is unsafe")
                os.fchown(child_fd, uid, -1)
                os.fchmod(child_fd, 0o700)
                os.fsync(child_fd)
            finally:
                os.close(child_fd)
        os.fsync(work_fd)
    finally:
        os.close(work_fd)
    return _validate_work(destination, snapshots, work)


def resolve_latest(snapshots, latest):
    snapshots = _absolute(snapshots, "snapshot directory")
    latest = _absolute(latest, "latest snapshot link")
    validate_snapshot_root(snapshots.parent, snapshots)
    _direct_child(snapshots, latest, "latest", "latest snapshot link")
    metadata = latest.lstat()
    if not stat.S_ISLNK(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError("latest snapshot must be an owned symlink")
    target = os.readlink(latest)
    if not SNAPSHOT_ID.fullmatch(target):
        raise ValueError("latest snapshot link target is not a direct timestamp child")
    _strict_snapshot_id(target, "latest snapshot id")
    snapshot = snapshots / target
    device = snapshots.lstat().st_dev
    _metadata(
        snapshot, "latest snapshot", kind="directory", uid=os.geteuid(), device=device
    )
    _canonical_existing(snapshot, "latest snapshot")
    manifest = snapshot / "MANIFEST.json"
    manifest_metadata = _metadata(
        manifest, "latest snapshot manifest", kind="file", uid=os.geteuid(), device=device
    )
    if stat.S_IMODE(manifest_metadata.st_mode) & 0o077:
        raise PermissionError("latest snapshot manifest must be private")
    return snapshot, target


def publish_latest(destination, snapshots, latest, snapshot_id):
    snapshots = _absolute(snapshots, "snapshot directory")
    latest = _absolute(latest, "latest snapshot link")
    validate_snapshot_root(destination, snapshots)
    _strict_snapshot_id(snapshot_id, "published snapshot id")
    snapshot = snapshots / snapshot_id
    snapshot_parent_metadata = snapshots.lstat()
    snapshot_metadata = _metadata(
        snapshot,
        "published snapshot",
        kind="directory",
        uid=os.geteuid(),
        device=snapshots.lstat().st_dev,
    )
    _direct_child(snapshots, latest, "latest", "latest snapshot link")
    if latest.exists() or latest.is_symlink():
        resolve_latest(snapshots, latest)
    temporary_name = f".latest-{os.getpid()}.tmp"
    directory_fd = os.open(snapshots, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = False
    try:
        opened_snapshots = os.fstat(directory_fd)
        if (
            opened_snapshots.st_dev != snapshot_parent_metadata.st_dev
            or opened_snapshots.st_ino != snapshot_parent_metadata.st_ino
            or snapshot_metadata.st_dev != opened_snapshots.st_dev
        ):
            raise RuntimeError("snapshot publication parent was replaced")
        os.symlink(snapshot_id, temporary_name, dir_fd=directory_fd)
        created = True
        os.replace(temporary_name, "latest", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        created = False
        os.fsync(directory_fd)
    finally:
        if created:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def describe_root_pin(path, descriptor):
    path = _absolute(path, "pinned recovery root")
    require_path_binding(path, descriptor)
    return format_identity(root_identity(descriptor))


def validate_root_pin(path, descriptor, identity):
    path = _absolute(path, "pinned recovery root")
    parsed = parse_identity(identity)
    require_path_binding(path, descriptor, parsed)
    return parsed


def _validate_sentinel_fd(parent_fd, name, expected, label):
    """Validate one private direct-child sentinel through a retained root."""
    descriptor = fd_open_child_file(parent_fd, name)
    try:
        parent = os.fstat(parent_fd)
        parent_mount = fd_mount_id(parent_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_dev != parent.st_dev
            or fd_mount_id(descriptor) != parent_mount
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
        ):
            raise PermissionError(f"{label} is unsafe")
        expected_contents = expected.encode("ascii") + b"\n"
        contents = os.pread(descriptor, len(expected_contents) + 1, 0)
        after = os.fstat(descriptor)
        if contents != expected_contents:
            raise ValueError(f"{label} content is not exact")
        if (
            after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_mode != before.st_mode
            or after.st_uid != before.st_uid
            or after.st_nlink != before.st_nlink
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or fd_mount_id(descriptor) != parent_mount
        ):
            raise RuntimeError(f"{label} changed during validation")
    finally:
        os.close(descriptor)


def validate_sentinels_pinned(
    source,
    source_fd,
    source_identity,
    source_value,
    destination,
    destination_fd,
    destination_identity,
    backup_value,
):
    """Validate both storage sentinels without reopening either root path."""
    validate_root_pin(source, source_fd, source_identity)
    validate_root_pin(destination, destination_fd, destination_identity)
    _validate_sentinel_fd(
        source_fd,
        ".david-pi-storage",
        source_value,
        "production storage sentinel",
    )
    _validate_sentinel_fd(
        destination_fd,
        ".david-pi-backup-storage",
        backup_value,
        "backup storage sentinel",
    )
    validate_root_pin(source, source_fd, source_identity)
    validate_root_pin(destination, destination_fd, destination_identity)


def _validate_pinned_work_fd(work_path, work_fd, identity=None):
    work_path = _absolute(work_path, "working snapshot")
    match = WORK_ID.fullmatch(work_path.name)
    if not match:
        raise ValueError("working snapshot name is invalid")
    _strict_snapshot_id(match.group(1), "working snapshot id")
    expected = parse_identity(identity) if identity is not None else root_identity(work_fd)
    require_path_binding(work_path, work_fd, expected)
    work_metadata = os.fstat(work_fd)
    work_mount = fd_mount_id(work_fd)
    if (
        work_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(work_metadata.st_mode) != 0o700
    ):
        raise PermissionError("working snapshot root is unsafe")
    marker_fd = fd_open_child_file(work_fd, ".started-at")
    try:
        marker = os.fstat(marker_fd)
        if (
            marker.st_dev != work_metadata.st_dev
            or fd_mount_id(marker_fd) != work_mount
            or marker.st_uid != os.geteuid()
            or marker.st_nlink != 1
            or stat.S_IMODE(marker.st_mode) != 0o600
            or not stat.S_ISREG(marker.st_mode)
        ):
            raise PermissionError("snapshot start marker is unsafe")
        raw = os.pread(marker_fd, 128, 0)
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise ValueError("snapshot start marker content is not exact")
        try:
            started_at = raw[:-1].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("snapshot start marker is not ASCII") from error
        _strict_utc_timestamp(started_at, "snapshot start marker")
    finally:
        os.close(marker_fd)
    for name in EXPECTED_CHILDREN:
        child_fd = fd_open_child_directory(work_fd, name)
        try:
            metadata = os.fstat(child_fd)
            if (
                metadata.st_dev != work_metadata.st_dev
                or fd_mount_id(child_fd) != work_mount
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise PermissionError(f"snapshot {name} directory is unsafe")
        finally:
            os.close(child_fd)
    require_path_binding(work_path, work_fd, expected)
    return started_at


def prepare_snapshot_root_pinned(destination, destination_fd, identity):
    destination = _absolute(destination, "backup destination")
    validate_root_pin(destination, destination_fd, identity)
    try:
        os.mkdir("snapshots", mode=0o700, dir_fd=destination_fd)
        os.fsync(destination_fd)
        fd_tree.after_directory_create(destination_fd, "snapshots")
    except FileExistsError:
        pass
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    try:
        metadata = os.fstat(snapshots_fd)
        root = os.fstat(destination_fd)
        if (
            metadata.st_dev != root.st_dev
            or fd_mount_id(snapshots_fd) != fd_mount_id(destination_fd)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise PermissionError("snapshot directory is unsafe")
        os.fsync(snapshots_fd)
    finally:
        os.close(snapshots_fd)
    validate_root_pin(destination, destination_fd, identity)


def list_incomplete_pinned(destination, destination_fd, identity):
    validate_root_pin(destination, destination_fd, identity)
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    try:
        names = []
        for name in sorted(os.listdir(snapshots_fd)):
            if not WORK_ID.fullmatch(name):
                continue
            child_fd = fd_open_child_directory(snapshots_fd, name)
            try:
                metadata = os.fstat(child_fd)
                if (
                    metadata.st_dev != os.fstat(destination_fd).st_dev
                    or fd_mount_id(child_fd) != fd_mount_id(destination_fd)
                ):
                    raise ValueError("incomplete snapshot crossed a mount boundary")
                names.append(name)
            finally:
                os.close(child_fd)
        return names
    finally:
        os.close(snapshots_fd)


def prepare_work_pinned(
    destination,
    destination_fd,
    destination_identity,
    work_name,
    *,
    started_at=None,
    resume=False,
):
    validate_root_pin(destination, destination_fd, destination_identity)
    match = WORK_ID.fullmatch(work_name)
    if not match:
        raise ValueError("working snapshot name is invalid")
    _strict_snapshot_id(match.group(1), "working snapshot id")
    if not resume:
        _strict_utc_timestamp(started_at, "snapshot start")
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    try:
        if not resume:
            os.mkdir(work_name, mode=0o700, dir_fd=snapshots_fd)
            os.fsync(snapshots_fd)
            fd_tree.after_directory_create(snapshots_fd, work_name)
        work_fd = fd_open_child_directory(snapshots_fd, work_name)
        try:
            if not resume:
                marker_fd = fd_create_child_file(
                    work_fd,
                    ".started-at",
                    flags=os.O_WRONLY,
                    mode=0o600,
                )
                try:
                    contents = started_at.encode("ascii") + b"\n"
                    offset = 0
                    while offset < len(contents):
                        count = os.write(marker_fd, contents[offset:])
                        if count <= 0:
                            raise OSError("short snapshot start marker write")
                        offset += count
                    os.fsync(marker_fd)
                finally:
                    os.close(marker_fd)
            for name in EXPECTED_CHILDREN:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=work_fd)
                    os.fsync(work_fd)
                    fd_tree.after_directory_create(work_fd, name)
                except FileExistsError:
                    if not resume:
                        raise
                child_fd = fd_open_child_directory(work_fd, name)
                os.close(child_fd)
            os.fsync(work_fd)
            work_identity = format_identity(root_identity(work_fd))
        finally:
            os.close(work_fd)
    finally:
        os.close(snapshots_fd)
    work_path = Path(destination) / "snapshots" / work_name
    pinned_fd = os.open(
        work_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        result = _validate_pinned_work_fd(work_path, pinned_fd, work_identity)
    finally:
        os.close(pinned_fd)
    validate_root_pin(destination, destination_fd, destination_identity)
    return result, work_identity


def resecure_work_pinned(work_path, work_fd, identity):
    _validate_pinned_work_fd(work_path, work_fd, identity)
    for name in EXPECTED_CHILDREN:
        child_fd = fd_open_child_directory(work_fd, name)
        try:
            os.fchown(child_fd, os.geteuid(), -1)
            os.fchmod(child_fd, 0o700)
            os.fsync(child_fd)
        finally:
            os.close(child_fd)
    os.fsync(work_fd)
    return _validate_pinned_work_fd(work_path, work_fd, identity)


def _open_source_root(source, source_fd=None, source_identity=None):
    source = _absolute(source, "protected copy source")
    if source_fd is None:
        if source.resolve(strict=True) != source or source.is_symlink():
            raise ValueError("protected copy source must be a canonical real directory")
        descriptor = os.open(
            source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        identity = root_identity(descriptor)
    else:
        descriptor = os.dup(source_fd)
        identity = parse_identity(source_identity)
    try:
        require_path_binding(source, descriptor, identity)
    except Exception:
        os.close(descriptor)
        raise
    return source, descriptor, identity


def _destination_records_match(records, destination_fd):
    actual = inventory_tree_fd(destination_fd)
    return actual[1:] == records[1:]


def sync_work_tree_pinned(
    source,
    work_path,
    work_fd,
    work_identity,
    relative,
    *,
    source_fd=None,
    source_identity=None,
    profile="all",
    basename_pattern=None,
    require_distinct_filesystem=True,
):
    _validate_pinned_work_fd(work_path, work_fd, work_identity)
    source, protected_source_fd, protected_source_identity = _open_source_root(
        source, source_fd, source_identity
    )
    target_fd = open_directory_chain(
        work_fd, relative_parts(relative, "snapshot copy target"), create=True
    )
    try:
        if profile == "data":
            excluded = data_backup_excluded
        elif profile == "top-files":
            excluded = basename_glob_excluded(basename_pattern)
        elif profile == "all":
            excluded = None
        else:
            raise ValueError("protected copy profile is invalid")
        records = inventory_tree_fd(protected_source_fd, excluded=excluded)
        # Inventory can be expensive.  Recheck the pathname binding before the
        # first destination deletion so a same-filesystem root replacement at
        # the inventory/copy boundary leaves the incomplete tree untouched.
        require_path_binding(source, protected_source_fd, protected_source_identity)
        fd_clear_directory(target_fd)
        copy_evidence_tree(
            protected_source_fd,
            target_fd,
            records,
            preserve_destination_root=True,
            require_distinct_filesystem=require_distinct_filesystem,
        )
        require_path_binding(source, protected_source_fd, protected_source_identity)
        if inventory_tree_fd(protected_source_fd, excluded=excluded) != records:
            raise RuntimeError("protected copy source changed during synchronization")
        if not _destination_records_match(records, target_fd):
            raise RuntimeError("protected copy destination is not exact")
        os.fsync(target_fd)
    finally:
        os.close(target_fd)
        os.close(protected_source_fd)
    _validate_pinned_work_fd(work_path, work_fd, work_identity)


def copy_work_file_pinned(source, work_path, work_fd, work_identity, relative):
    _validate_pinned_work_fd(work_path, work_fd, work_identity)
    source = _absolute(source, "protected file source")
    target_parts = relative_parts(relative, "snapshot file target")
    if target_parts[-1] != source.name:
        raise ValueError("protected file source and target names must match")
    if source.parent.resolve(strict=True) != source.parent:
        raise ValueError("protected file source parent is not canonical")
    source_parent_fd = os.open(
        source.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        def excluded(path, kind):
            return path != source.name or kind != "file"

        records = inventory_tree_fd(source_parent_fd, excluded=excluded)
        if len(records) != 2:
            raise ValueError("protected file source is not a regular direct child")
        parts = target_parts
        parent_fd = open_directory_chain(work_fd, parts[:-1], create=True)
        try:
            try:
                existing_fd = fd_pin_child(parent_fd, parts[-1])
            except FileNotFoundError:
                existing_fd = None
            if existing_fd is not None:
                try:
                    metadata = os.fstat(existing_fd)
                    if (
                        not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_dev != os.fstat(work_fd).st_dev
                        or fd_mount_id(existing_fd) != fd_mount_id(work_fd)
                        or metadata.st_nlink != 1
                    ):
                        raise ValueError("existing snapshot file target is unsafe")
                    fd_tree.before_entry_mutation(parent_fd, parts[-1], "replace-file")
                    current = os.stat(
                        parts[-1], dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (current.st_dev, current.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise RuntimeError("snapshot file target changed before removal")
                    os.unlink(parts[-1], dir_fd=parent_fd)
                finally:
                    os.close(existing_fd)
            copy_evidence_tree(
                source_parent_fd,
                parent_fd,
                records,
                preserve_destination_root=True,
                require_distinct_filesystem=True,
            )
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(source_parent_fd)
    _validate_pinned_work_fd(work_path, work_fd, work_identity)


def copy_sqlite_databases_pinned(
    source,
    source_fd,
    source_identity,
    work_path,
    work_fd,
    work_identity,
):
    _validate_pinned_work_fd(work_path, work_fd, work_identity)
    source, protected_source_fd, protected_identity = _open_source_root(
        source, source_fd, source_identity
    )
    destination_fd = fd_open_child_directory(work_fd, "databases")
    try:
        def database_excluded(path, kind):
            if kind == "directory":
                return False
            return kind != "file" or not path.endswith(".db")

        source_records = inventory_tree_fd(
            protected_source_fd, excluded=database_excluded
        )
        database_records = [
            record
            for record in source_records
            if record["type"] == "file" and record["path"].endswith(".db")
        ]
        if not database_records:
            raise RuntimeError("no SQLite databases found")
        # The source inventory may be expensive.  A root-name replacement at
        # this exact inventory/mutation boundary must leave the prior database
        # tree intact, just as the main tree synchronizer does.
        require_path_binding(source, protected_source_fd, protected_identity)
        fd_clear_directory(destination_fd)
        for record in database_records:
            parts = relative_parts(record["path"], "database source path")
            source_parent_fd = open_directory_chain(
                protected_source_fd, parts[:-1], create=False
            )
            source_database_fd = fd_open_child_file(source_parent_fd, parts[-1])
            destination_parent_fd = open_directory_chain(
                destination_fd, parts[:-1], create=True
            )
            output_fd = None
            try:
                source_metadata = os.fstat(source_database_fd)
                if (
                    source_metadata.st_dev != os.fstat(protected_source_fd).st_dev
                    or fd_mount_id(source_database_fd)
                    != fd_mount_id(protected_source_fd)
                ):
                    raise ValueError("SQLite source crossed a mount boundary")
                output_fd = fd_create_child_file(
                    destination_parent_fd,
                    parts[-1],
                    flags=os.O_RDWR,
                    mode=0o600,
                )
                source_uri = f"file:/proc/self/fd/{source_database_fd}?mode=ro&immutable=1"
                destination_uri = f"file:/proc/self/fd/{output_fd}?mode=rw"
                with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
                    with sqlite3.connect(destination_uri, uri=True) as dst:
                        src.backup(dst)
                        quick = dst.execute("PRAGMA quick_check").fetchone()
                        foreign_keys = dst.execute(
                            "PRAGMA foreign_key_check"
                        ).fetchall()
                        if not quick or quick[0] != "ok" or foreign_keys:
                            raise RuntimeError(
                                "database verification failed during protected copy"
                            )
                current = os.stat(
                    parts[-1], dir_fd=source_parent_fd, follow_symlinks=False
                )
                if (current.st_dev, current.st_ino) != (
                    source_metadata.st_dev,
                    source_metadata.st_ino,
                ):
                    raise RuntimeError("SQLite source changed during protected copy")
                output_metadata = os.fstat(output_fd)
                if (
                    not stat.S_ISREG(output_metadata.st_mode)
                    or output_metadata.st_dev != os.fstat(destination_fd).st_dev
                    or fd_mount_id(output_fd) != fd_mount_id(destination_fd)
                    or output_metadata.st_nlink != 1
                ):
                    raise RuntimeError("SQLite destination is unsafe")
                os.fchown(output_fd, os.geteuid(), os.getegid())
                os.fchmod(output_fd, 0o600)
                os.fsync(output_fd)
                output_metadata = os.fstat(output_fd)
                if (
                    not stat.S_ISREG(output_metadata.st_mode)
                    or output_metadata.st_dev != os.fstat(destination_fd).st_dev
                    or fd_mount_id(output_fd) != fd_mount_id(destination_fd)
                    or output_metadata.st_nlink != 1
                    or output_metadata.st_uid != os.geteuid()
                    or output_metadata.st_gid != os.getegid()
                    or stat.S_IMODE(output_metadata.st_mode) != 0o600
                ):
                    raise RuntimeError("SQLite destination failed validation")
                os.fsync(destination_parent_fd)
            except Exception:
                # A failed incomplete snapshot is retained.  Pathname cleanup
                # could unlink a concurrently substituted entry.
                raise
            finally:
                if output_fd is not None:
                    os.close(output_fd)
                os.close(destination_parent_fd)
                os.close(source_database_fd)
                os.close(source_parent_fd)
        require_path_binding(source, protected_source_fd, protected_identity)
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
        os.close(protected_source_fd)
    _validate_pinned_work_fd(work_path, work_fd, work_identity)


def _atomic_write_fd(parent_fd, name, contents, allowed_names):
    if name not in allowed_names or Path(name).name != name:
        raise ValueError("atomic output name is not allowed")
    if not isinstance(contents, bytes) or len(contents) > MAX_ATOMIC_WRITE_BYTES:
        raise ValueError("atomic output is invalid or too large")
    parent = os.fstat(parent_fd)
    parent_mount = fd_mount_id(parent_fd)
    temporary_name = None
    descriptor = None
    try:
        try:
            existing_fd = fd_pin_child(parent_fd, name)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                existing = os.fstat(existing_fd)
                if (
                    not stat.S_ISREG(existing.st_mode)
                    or existing.st_uid != os.geteuid()
                    or existing.st_dev != parent.st_dev
                    or fd_mount_id(existing_fd) != parent_mount
                    or existing.st_nlink != 1
                ):
                    raise PermissionError("existing atomic output is unsafe")
            finally:
                os.close(existing_fd)
        for _ in range(32):
            candidate = f".{name}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = fd_create_child_file(
                    parent_fd,
                    candidate,
                    flags=os.O_WRONLY,
                    mode=0o600,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None:
            raise FileExistsError("could not allocate protected atomic output")
        offset = 0
        while offset < len(contents):
            count = os.write(descriptor, contents[offset:])
            if count <= 0:
                raise OSError("short protected atomic write")
            offset += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        published_fd = fd_open_child_file(parent_fd, name)
        try:
            published = os.fstat(published_fd)
            if (
                not stat.S_ISREG(published.st_mode)
                or published.st_uid != os.geteuid()
                or published.st_nlink != 1
                or published.st_dev != parent.st_dev
                or fd_mount_id(published_fd) != parent_mount
                or published.st_size != len(contents)
                or stat.S_IMODE(published.st_mode) != 0o600
            ):
                raise RuntimeError("protected atomic output failed validation")
        finally:
            os.close(published_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # A failed private temporary is deliberately retained.  Its pathname
        # may have been substituted after creation, so cleanup-by-name would
        # turn an otherwise fail-closed error into an unlink race.


def write_destination_file_pinned(destination, destination_fd, identity, name, contents):
    validate_root_pin(destination, destination_fd, identity)
    _atomic_write_fd(destination_fd, name, contents, STATUS_FILES)
    validate_root_pin(destination, destination_fd, identity)


def write_work_file_pinned(work_path, work_fd, identity, name, contents):
    _validate_pinned_work_fd(work_path, work_fd, identity)
    _atomic_write_fd(work_fd, name, contents, WORK_METADATA_FILES)
    _validate_pinned_work_fd(work_path, work_fd, identity)


def fsync_pinned(path, descriptor, identity):
    validate_root_pin(path, descriptor, identity)
    os.fsync(descriptor)


RENAME_NOREPLACE = 1


def _rename_noreplace(source_parent_fd, source, destination_parent_fd, destination):
    result = fd_tree._LIBC.renameat2(
        source_parent_fd,
        os.fsencode(source),
        destination_parent_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def finalize_work_pinned(
    destination,
    destination_fd,
    destination_identity,
    work_path,
    work_fd,
    work_identity,
    snapshot_id,
):
    validate_root_pin(destination, destination_fd, destination_identity)
    _validate_pinned_work_fd(work_path, work_fd, work_identity)
    _strict_snapshot_id(snapshot_id, "final snapshot id")
    if Path(work_path).name != f".incomplete-{snapshot_id}":
        raise ValueError("working snapshot does not match final snapshot id")
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    try:
        current_work_fd = fd_open_child_directory(snapshots_fd, Path(work_path).name)
        try:
            if root_identity(current_work_fd) != root_identity(work_fd):
                raise RuntimeError("working snapshot name was replaced before publication")
        finally:
            os.close(current_work_fd)
        _rename_noreplace(
            snapshots_fd,
            Path(work_path).name,
            snapshots_fd,
            snapshot_id,
        )
        final_fd = fd_open_child_directory(snapshots_fd, snapshot_id)
        try:
            if root_identity(final_fd) != root_identity(work_fd):
                raise RuntimeError("published snapshot identity does not match work")
        finally:
            os.close(final_fd)
        os.fsync(snapshots_fd)
    finally:
        os.close(snapshots_fd)
    validate_root_pin(destination, destination_fd, destination_identity)


def validate_final_pinned(
    destination,
    destination_fd,
    destination_identity,
    final_path,
    snapshot_fd,
    snapshot_identity,
    snapshot_id,
):
    """Bind a published snapshot name to the retained work inode and mount."""
    destination = _absolute(destination, "backup destination")
    final_path = _absolute(final_path, "published snapshot")
    validate_root_pin(destination, destination_fd, destination_identity)
    _strict_snapshot_id(snapshot_id, "published snapshot id")
    expected_path = destination / "snapshots" / snapshot_id
    if final_path != expected_path:
        raise ValueError("published snapshot path does not match its id")
    expected_identity = parse_identity(snapshot_identity)
    if root_identity(snapshot_fd) != expected_identity:
        raise RuntimeError("retained published snapshot descriptor changed identity")
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    try:
        current_fd = fd_open_child_directory(snapshots_fd, snapshot_id)
        try:
            if root_identity(current_fd) != expected_identity:
                raise RuntimeError("published snapshot name was replaced")
        finally:
            os.close(current_fd)
    finally:
        os.close(snapshots_fd)
    if root_identity(snapshot_fd) != expected_identity:
        raise RuntimeError("retained published snapshot descriptor changed identity")
    validate_root_pin(destination, destination_fd, destination_identity)
    return expected_identity


def publish_latest_pinned(
    destination,
    destination_fd,
    destination_identity,
    snapshot_id,
    final_path,
    snapshot_fd,
    snapshot_identity,
):
    validate_final_pinned(
        destination,
        destination_fd,
        destination_identity,
        final_path,
        snapshot_fd,
        snapshot_identity,
        snapshot_id,
    )
    snapshots_fd = fd_open_child_directory(destination_fd, "snapshots")
    temporary_name = None
    try:
        published_snapshot_fd = fd_open_child_directory(snapshots_fd, snapshot_id)
        try:
            manifest_fd = fd_open_child_file(
                published_snapshot_fd, "MANIFEST.json"
            )
            try:
                metadata = os.fstat(manifest_fd)
                if (
                    metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                    or metadata.st_dev != os.fstat(published_snapshot_fd).st_dev
                    or fd_mount_id(manifest_fd)
                    != fd_mount_id(published_snapshot_fd)
                ):
                    raise PermissionError("published snapshot manifest is unsafe")
            finally:
                os.close(manifest_fd)
        finally:
            os.close(published_snapshot_fd)
        try:
            latest_fd = fd_pin_child(snapshots_fd, "latest")
        except FileNotFoundError:
            latest_fd = None
        if latest_fd is not None:
            try:
                metadata = os.fstat(latest_fd)
                if (
                    not stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_dev != os.fstat(snapshots_fd).st_dev
                    or fd_mount_id(latest_fd) != fd_mount_id(snapshots_fd)
                ):
                    raise PermissionError("existing latest snapshot link is unsafe")
                old_target = os.readlink("", dir_fd=latest_fd)
                _strict_snapshot_id(old_target, "existing latest snapshot id")
            finally:
                os.close(latest_fd)
        temporary_name = f".latest-{secrets.token_hex(16)}.tmp"
        os.symlink(snapshot_id, temporary_name, dir_fd=snapshots_fd)
        after_latest_symlink_create(snapshots_fd, temporary_name)
        temporary_fd = fd_pin_child(snapshots_fd, temporary_name)
        try:
            temporary = os.fstat(temporary_fd)
            if (
                not stat.S_ISLNK(temporary.st_mode)
                or temporary.st_uid != os.geteuid()
                or temporary.st_dev != os.fstat(snapshots_fd).st_dev
                or fd_mount_id(temporary_fd) != fd_mount_id(snapshots_fd)
                or os.readlink("", dir_fd=temporary_fd) != snapshot_id
            ):
                raise RuntimeError("candidate latest snapshot link was replaced")
        finally:
            os.close(temporary_fd)
        os.replace(
            temporary_name,
            "latest",
            src_dir_fd=snapshots_fd,
            dst_dir_fd=snapshots_fd,
        )
        temporary_name = None
        latest_fd = fd_pin_child(snapshots_fd, "latest")
        try:
            latest_metadata = os.fstat(latest_fd)
            if (
                not stat.S_ISLNK(latest_metadata.st_mode)
                or latest_metadata.st_uid != os.geteuid()
                or latest_metadata.st_dev != os.fstat(snapshots_fd).st_dev
                or fd_mount_id(latest_fd) != fd_mount_id(snapshots_fd)
                or os.readlink("", dir_fd=latest_fd) != snapshot_id
            ):
                raise RuntimeError("published latest snapshot link is not exact")
        finally:
            os.close(latest_fd)
        os.fsync(snapshots_fd)
    finally:
        # Do not unlink a failed candidate pathname: it may have been replaced.
        os.close(snapshots_fd)
    validate_final_pinned(
        destination,
        destination_fd,
        destination_identity,
        final_path,
        snapshot_fd,
        snapshot_identity,
        snapshot_id,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    describe_pin = subparsers.add_parser("describe-root-pin")
    validate_pin = subparsers.add_parser("validate-root-pin")
    pinned_root = subparsers.add_parser("pinned-prepare-root")
    pinned_incomplete = subparsers.add_parser("pinned-list-incomplete")
    pinned_sentinels = subparsers.add_parser("pinned-validate-sentinels")
    pinned_prepare = subparsers.add_parser("pinned-prepare-work")
    pinned_validate = subparsers.add_parser("pinned-validate-work")
    pinned_resecure = subparsers.add_parser("pinned-resecure-work")
    pinned_sync = subparsers.add_parser("pinned-sync-tree")
    pinned_copy_file = subparsers.add_parser("pinned-copy-file")
    pinned_databases = subparsers.add_parser("pinned-copy-databases")
    pinned_write_destination = subparsers.add_parser("pinned-write-destination")
    pinned_write_work = subparsers.add_parser("pinned-write-work")
    pinned_fsync_parser = subparsers.add_parser("pinned-fsync")
    pinned_finalize = subparsers.add_parser("pinned-finalize")
    pinned_validate_final = subparsers.add_parser("pinned-validate-final")
    pinned_latest = subparsers.add_parser("pinned-publish-latest")
    for command in (describe_pin, validate_pin, pinned_root, pinned_incomplete):
        command.add_argument("--path", type=Path, required=True)
        command.add_argument("--fd", type=int, required=True)
    for command in (validate_pin, pinned_root, pinned_incomplete):
        command.add_argument("--identity", required=True)
    pinned_sentinels.add_argument("--source", type=Path, required=True)
    pinned_sentinels.add_argument("--source-fd", type=int, required=True)
    pinned_sentinels.add_argument("--source-identity", required=True)
    pinned_sentinels.add_argument("--source-value", required=True)
    pinned_sentinels.add_argument("--destination", type=Path, required=True)
    pinned_sentinels.add_argument("--destination-fd", type=int, required=True)
    pinned_sentinels.add_argument("--destination-identity", required=True)
    pinned_sentinels.add_argument("--backup-value", required=True)
    for command in (
        pinned_prepare,
        pinned_write_destination,
        pinned_finalize,
        pinned_validate_final,
        pinned_latest,
    ):
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--destination-fd", type=int, required=True)
        command.add_argument("--destination-identity", required=True)
    pinned_prepare.add_argument("--work-name", required=True)
    pinned_prepare.add_argument("--started-at")
    pinned_prepare.add_argument("--resume", action="store_true")
    for command in (
        pinned_validate,
        pinned_resecure,
        pinned_sync,
        pinned_copy_file,
        pinned_databases,
        pinned_write_work,
        pinned_finalize,
    ):
        command.add_argument("--work-path", type=Path, required=True)
        command.add_argument("--work-fd", type=int, required=True)
        command.add_argument("--work-identity", required=True)
    pinned_sync.add_argument("--source", type=Path, required=True)
    pinned_sync.add_argument("--source-fd", type=int)
    pinned_sync.add_argument("--source-identity")
    pinned_sync.add_argument("--relative", required=True)
    pinned_sync.add_argument(
        "--profile", choices=("all", "data", "top-files"), default="all"
    )
    pinned_sync.add_argument("--basename-pattern")
    pinned_copy_file.add_argument("--source", type=Path, required=True)
    pinned_copy_file.add_argument("--relative", required=True)
    pinned_databases.add_argument("--source", type=Path, required=True)
    pinned_databases.add_argument("--source-fd", type=int, required=True)
    pinned_databases.add_argument("--source-identity", required=True)
    pinned_write_destination.add_argument(
        "--name", choices=sorted(STATUS_FILES), required=True
    )
    pinned_write_work.add_argument(
        "--name", choices=sorted(WORK_METADATA_FILES), required=True
    )
    pinned_fsync_parser.add_argument("--path", type=Path, required=True)
    pinned_fsync_parser.add_argument("--fd", type=int, required=True)
    pinned_fsync_parser.add_argument("--identity", required=True)
    pinned_finalize.add_argument("--snapshot-id", required=True)
    for command in (pinned_validate_final, pinned_latest):
        command.add_argument("--snapshot-id", required=True)
        command.add_argument("--final-path", type=Path, required=True)
        command.add_argument("--snapshot-fd", type=int, required=True)
        command.add_argument("--snapshot-identity", required=True)
    prepare_root = subparsers.add_parser("prepare-root")
    prepare = subparsers.add_parser("prepare-work")
    validate = subparsers.add_parser("validate-work")
    resecure = subparsers.add_parser("resecure-work")
    copy_target = subparsers.add_parser("prepare-copy-target")
    mutable_tree = subparsers.add_parser("validate-mutable-tree")
    clear_tree = subparsers.add_parser("clear-mutable-tree")
    latest = subparsers.add_parser("resolve-latest")
    publish = subparsers.add_parser("publish-latest")
    sentinels = subparsers.add_parser("validate-sentinels")
    write_destination = subparsers.add_parser("write-destination-file")
    write_work = subparsers.add_parser("write-work-file")
    for command in (
        prepare,
        validate,
        resecure,
        copy_target,
        mutable_tree,
        clear_tree,
        write_work,
    ):
        command.add_argument("--destination", type=Path, required=True)
        command.add_argument("--snapshots", type=Path, required=True)
        command.add_argument("--work", type=Path, required=True)
    prepare_root.add_argument("--destination", type=Path, required=True)
    prepare_root.add_argument("--snapshots", type=Path, required=True)
    prepare.add_argument("--started-at")
    prepare.add_argument("--resume", action="store_true")
    validate.add_argument("--final", type=Path, required=True)
    copy_target.add_argument("--relative", required=True)
    mutable_tree.add_argument("--relative", required=True)
    mutable_tree.add_argument("--allow-file-symlinks", action="store_true")
    clear_tree.add_argument("--relative", required=True)
    latest.add_argument("--snapshots", type=Path, required=True)
    latest.add_argument("--latest", type=Path, required=True)
    publish.add_argument("--destination", type=Path, required=True)
    publish.add_argument("--snapshots", type=Path, required=True)
    publish.add_argument("--latest", type=Path, required=True)
    publish.add_argument("--snapshot-id", required=True)
    sentinels.add_argument("--source", type=Path, required=True)
    sentinels.add_argument("--source-value", required=True)
    sentinels.add_argument("--destination", type=Path, required=True)
    sentinels.add_argument("--backup-value", required=True)
    write_destination.add_argument("--destination", type=Path, required=True)
    write_destination.add_argument("--name", choices=sorted(STATUS_FILES), required=True)
    write_work.add_argument("--name", choices=sorted(WORK_METADATA_FILES), required=True)
    args = parser.parse_args(argv)
    if args.command == "describe-root-pin":
        print(describe_root_pin(args.path, args.fd))
    elif args.command == "validate-root-pin":
        validate_root_pin(args.path, args.fd, args.identity)
        print("ok")
    elif args.command == "pinned-prepare-root":
        prepare_snapshot_root_pinned(args.path, args.fd, args.identity)
        print("ok")
    elif args.command == "pinned-list-incomplete":
        for name in list_incomplete_pinned(args.path, args.fd, args.identity):
            print(name)
    elif args.command == "pinned-validate-sentinels":
        validate_sentinels_pinned(
            args.source,
            args.source_fd,
            args.source_identity,
            args.source_value,
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.backup_value,
        )
        print("ok")
    elif args.command == "pinned-prepare-work":
        started, identity = prepare_work_pinned(
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.work_name,
            started_at=args.started_at,
            resume=args.resume,
        )
        print(started)
        print(identity)
    elif args.command == "pinned-validate-work":
        print(_validate_pinned_work_fd(
            args.work_path, args.work_fd, args.work_identity
        ))
    elif args.command == "pinned-resecure-work":
        print(resecure_work_pinned(
            args.work_path, args.work_fd, args.work_identity
        ))
    elif args.command == "pinned-sync-tree":
        if (args.source_fd is None) != (args.source_identity is None):
            raise ValueError("source fd and identity must be supplied together")
        sync_work_tree_pinned(
            args.source,
            args.work_path,
            args.work_fd,
            args.work_identity,
            args.relative,
            source_fd=args.source_fd,
            source_identity=args.source_identity,
            profile=args.profile,
            basename_pattern=args.basename_pattern,
        )
        print("ok")
    elif args.command == "pinned-copy-file":
        copy_work_file_pinned(
            args.source,
            args.work_path,
            args.work_fd,
            args.work_identity,
            args.relative,
        )
        print("ok")
    elif args.command == "pinned-copy-databases":
        copy_sqlite_databases_pinned(
            args.source,
            args.source_fd,
            args.source_identity,
            args.work_path,
            args.work_fd,
            args.work_identity,
        )
        print("ok")
    elif args.command == "pinned-write-destination":
        write_destination_file_pinned(
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.name,
            sys.stdin.buffer.read(MAX_ATOMIC_WRITE_BYTES + 1),
        )
        print("ok")
    elif args.command == "pinned-write-work":
        write_work_file_pinned(
            args.work_path,
            args.work_fd,
            args.work_identity,
            args.name,
            sys.stdin.buffer.read(MAX_ATOMIC_WRITE_BYTES + 1),
        )
        print("ok")
    elif args.command == "pinned-fsync":
        fsync_pinned(args.path, args.fd, args.identity)
        print("ok")
    elif args.command == "pinned-finalize":
        finalize_work_pinned(
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.work_path,
            args.work_fd,
            args.work_identity,
            args.snapshot_id,
        )
        print("ok")
    elif args.command == "pinned-validate-final":
        validate_final_pinned(
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.final_path,
            args.snapshot_fd,
            args.snapshot_identity,
            args.snapshot_id,
        )
        print("ok")
    elif args.command == "pinned-publish-latest":
        publish_latest_pinned(
            args.destination,
            args.destination_fd,
            args.destination_identity,
            args.snapshot_id,
            args.final_path,
            args.snapshot_fd,
            args.snapshot_identity,
        )
        print("ok")
    elif args.command == "prepare-root":
        prepare_snapshot_root(args.destination, args.snapshots)
        print("ok")
    elif args.command == "prepare-work":
        print(prepare_work(
            args.destination, args.snapshots, args.work,
            started_at=args.started_at, resume=args.resume,
        ))
    elif args.command == "validate-work":
        print(validate_work(args.destination, args.snapshots, args.work, args.final))
    elif args.command == "resecure-work":
        print(resecure_work_roots(args.destination, args.snapshots, args.work))
    elif args.command == "prepare-copy-target":
        print(prepare_copy_target(
            args.destination, args.snapshots, args.work, args.relative
        ))
    elif args.command == "validate-mutable-tree":
        print(validate_mutable_tree(
            args.destination,
            args.snapshots,
            args.work,
            args.relative,
            allow_file_symlinks=args.allow_file_symlinks,
        ))
    elif args.command == "clear-mutable-tree":
        print(clear_mutable_tree(
            args.destination,
            args.snapshots,
            args.work,
            args.relative,
        ))
    elif args.command == "resolve-latest":
        snapshot, snapshot_id = resolve_latest(args.snapshots, args.latest)
        print(snapshot)
        print(snapshot_id)
    elif args.command == "publish-latest":
        publish_latest(
            args.destination, args.snapshots, args.latest, args.snapshot_id
        )
    elif args.command == "write-destination-file":
        write_destination_file(
            args.destination, args.name, sys.stdin.buffer.read(MAX_ATOMIC_WRITE_BYTES + 1)
        )
        print("ok")
    elif args.command == "write-work-file":
        write_work_file(
            args.destination,
            args.snapshots,
            args.work,
            args.name,
            sys.stdin.buffer.read(MAX_ATOMIC_WRITE_BYTES + 1),
        )
        print("ok")
    else:
        validate_sentinel(
            args.source,
            args.source / ".david-pi-storage",
            args.source_value,
            "production storage sentinel",
        )
        validate_sentinel(
            args.destination,
            args.destination / ".david-pi-backup-storage",
            args.backup_value,
            "backup storage sentinel",
        )
        print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
