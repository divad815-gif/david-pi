#!/usr/bin/env python3
"""Restore a signed snapshot into an explicitly marked isolated target."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
import secrets
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from david_pi_fd_tree import (
    copy_evidence_tree,
    create_child_file,
    fd_mount_id,
    inventory_tree_fd,
    open_child_directory,
    open_child_file,
    open_directory_chain,
    relative_parts,
    rename_child_noreplace,
    signed_sample_paths,
    verify_open_evidence_record,
)
from david_pi_snapshot_manifest import (
    load_signing_key,
    verify_manifest,
)
from david_pi_restore_receipt import (
    create_restore_receipt,
    publish_restore_receipt,
)
from david_pi_recovery_paths import resolve_latest


RESTORE_SENTINEL = ".david-pi-restore-target"
EXPECTED_SENTINEL = "david-pi-isolated-restore-v1"
EXPECTED_APPLICATION_SENTINEL = b"david-pi-family-storage-v1\n"
APPLICATION_UID = 10001
APPLICATION_GID = 10001
APPLICATION_DATABASE_MODE = 0o640
REQUIRED_EMPTY_DIRECTORIES = (
    "incoming",
    "tmp/uploads",
    "tmp/runtime",
    "files/incoming",
)
REQUIRED_WRITABLE_DIRECTORIES = ("quarantine",)
FORBIDDEN_TARGETS = {
    Path("/"),
    Path("/home"),
    Path("/srv"),
    Path("/srv/data"),
    Path("/srv/backup-data"),
}


def _fd_mount_id(descriptor):
    try:
        contents = Path(f"/proc/self/fdinfo/{descriptor}").read_text(
            encoding="ascii"
        )
    except (OSError, UnicodeError) as error:
        raise RuntimeError("could not establish restore-target mount identity") from error
    for line in contents.splitlines():
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return int(value)
    raise RuntimeError("restore-target mount identity is unavailable")


def _require_path_matches_fd(path, descriptor, label="restore destination"):
    try:
        current = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as error:
        raise RuntimeError(f"{label} pathname was replaced") from error
    pinned = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_dev != pinned.st_dev
        or current.st_ino != pinned.st_ino
        or _fd_mount_id(descriptor) != _path_mount_id(path)
    ):
        raise RuntimeError(f"{label} pathname was replaced")


def _path_mount_id(path):
    descriptor = os.open(
        path,
        getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        return _fd_mount_id(descriptor)
    finally:
        os.close(descriptor)


def _open_validated_target(
    snapshot,
    destination,
    *,
    required_bytes=0,
    expected_uid=0,
    require_mount=True,
    require_distinct_filesystem=True,
):
    snapshot = Path(snapshot).resolve(strict=True)
    unresolved_destination = Path(destination)
    if (
        not unresolved_destination.is_absolute()
        or Path(os.path.normpath(unresolved_destination)) != unresolved_destination
    ):
        raise ValueError("restore destination must be an absolute canonical path")
    if unresolved_destination.is_symlink():
        raise ValueError("restore destination must not be a symlink")
    destination = unresolved_destination
    if destination == Path("/") or any(
        protected != Path("/")
        and (destination == protected or protected in destination.parents)
        for protected in FORBIDDEN_TARGETS
    ):
        raise ValueError("restore destination is a protected broad path")
    if unresolved_destination.resolve(strict=True) != unresolved_destination:
        raise ValueError("restore destination must be a canonical real path")
    if destination == snapshot or destination in snapshot.parents or snapshot in destination.parents:
        raise ValueError("restore destination must be isolated from the snapshot")

    destination_fd = os.open(
        destination,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        _require_path_matches_fd(destination, destination_fd)
        destination_metadata = os.fstat(destination_fd)
        if (
            destination_metadata.st_uid != expected_uid
            or stat.S_IMODE(destination_metadata.st_mode) != 0o700
        ):
            raise PermissionError(
                "restore destination must be owned by the approved user with mode 0700"
            )

        try:
            sentinel_fd = open_child_file(destination_fd, RESTORE_SENTINEL)
        except FileNotFoundError as error:
            raise ValueError("restore destination is missing its safety sentinel") from error
        try:
            sentinel_metadata = os.fstat(sentinel_fd)
            if (
                not stat.S_ISREG(sentinel_metadata.st_mode)
                or sentinel_metadata.st_uid != expected_uid
                or stat.S_IMODE(sentinel_metadata.st_mode) & 0o077
                or sentinel_metadata.st_nlink != 1
                or sentinel_metadata.st_dev != destination_metadata.st_dev
                or fd_mount_id(sentinel_fd) != fd_mount_id(destination_fd)
            ):
                raise PermissionError(
                    "restore destination sentinel ownership or mode is unsafe"
                )
            expected = EXPECTED_SENTINEL.encode("ascii") + b"\n"
            contents = os.read(sentinel_fd, len(expected) + 1)
            if contents != expected or os.read(sentinel_fd, 1):
                raise ValueError("restore destination safety sentinel is invalid")
        finally:
            os.close(sentinel_fd)

        if (
            require_distinct_filesystem
            and destination_metadata.st_dev == snapshot.stat().st_dev
        ):
            raise ValueError(
                "restore destination must use a different filesystem from the snapshot"
            )
        if require_mount:
            parent_fd = os.open(
                destination.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                if _fd_mount_id(parent_fd) == _fd_mount_id(destination_fd):
                    raise ValueError("restore destination must be a dedicated mount point")
            finally:
                os.close(parent_fd)
        filesystem = os.statvfs(f"/proc/self/fd/{destination_fd}")
        available_bytes = filesystem.f_bavail * filesystem.f_frsize
        required_with_headroom = max(
            int(required_bytes * 1.05), int(required_bytes) + 64 * 1024 * 1024
        )
        if available_bytes < required_with_headroom:
            raise OSError("restore destination does not have enough available capacity")
        unexpected = [
            name for name in os.listdir(destination_fd) if name != RESTORE_SENTINEL
        ]
        if unexpected:
            raise ValueError(
                "restore destination must be empty except for its safety sentinel"
            )
        _require_path_matches_fd(destination, destination_fd)
        return snapshot, destination, destination_fd
    except Exception:
        os.close(destination_fd)
        raise


def validate_target(
    snapshot,
    destination,
    *,
    required_bytes=0,
    expected_uid=0,
    require_mount=True,
    require_distinct_filesystem=True,
):
    snapshot, destination, destination_fd = _open_validated_target(
        snapshot,
        destination,
        required_bytes=required_bytes,
        expected_uid=expected_uid,
        require_mount=require_mount,
        require_distinct_filesystem=require_distinct_filesystem,
    )
    os.close(destination_fd)
    return snapshot, destination


def _after_manifest_verified(_snapshot, _manifest):
    """Test seam at the exact signed-evidence-to-copy boundary."""


def _after_target_child_created(_parent_fd, _name):
    """Test seam before NO_XDEV re-open of a new target child."""


def _before_application_sentinel_open(_data_root_fd):
    """Test seam immediately before the sentinel is pinned for mutation."""


def _before_final_target_validation(_destination_fd):
    """Test seam before every visible restored descendant is re-pinned."""


def _create_child_directory(parent_fd, name):
    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    os.fsync(parent_fd)
    _after_target_child_created(parent_fd, name)
    # A late mount over the new name is rejected before it can receive any
    # content or metadata operation.
    return open_child_directory(parent_fd, name)


def _validate_target_descendants(destination_fd):
    """Reject a late mount or redirect anywhere below a restored top-level tree."""
    destination = os.fstat(destination_fd)
    destination_mount = fd_mount_id(destination_fd)
    for name in ("databases", "config", "data"):
        descriptor = open_child_directory(destination_fd, name)
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_dev != destination.st_dev
                or fd_mount_id(descriptor) != destination_mount
            ):
                raise ValueError("restored target tree crossed a mount boundary")
            # Inventory itself performs descriptor-relative, NO_XDEV recursion,
            # so a mount inserted below the top-level child also fails here.
            inventory_tree_fd(descriptor)
        finally:
            os.close(descriptor)


def _evidence_tree(manifest, name):
    evidence = manifest.get("_recovery_evidence")
    if not isinstance(evidence, dict):
        raise RuntimeError("verified manifest has no immutable recovery evidence")
    return evidence["trees"][name]


def _sha256_fd(descriptor):
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(descriptor, 1024 * 1024, offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _open_relative_file(root_fd, raw_path):
    parts = relative_parts(raw_path, "restore file path")
    parent_fd = open_directory_chain(root_fd, parts[:-1], create=False)
    try:
        descriptor = open_child_file(parent_fd, parts[-1])
    finally:
        os.close(parent_fd)
    return descriptor


def verify_restored_databases(destination_fd, manifest):
    verified = 0
    destination_metadata = os.fstat(destination_fd)
    destination_mount = fd_mount_id(destination_fd)
    for record in manifest["databases"]:
        descriptor = _open_relative_file(destination_fd, record["path"])
        try:
            metadata = os.fstat(descriptor)
            if (
                metadata.st_dev != destination_metadata.st_dev
                or fd_mount_id(descriptor) != destination_mount
                or metadata.st_size != record["byte_size"]
            ):
                raise RuntimeError("a restored database is missing or has the wrong size")
            if _sha256_fd(descriptor) != record["sha256"]:
                raise RuntimeError("a restored database hash does not match")
            if (
                format(stat.S_IMODE(metadata.st_mode), "04o") != record["mode"]
                or metadata.st_uid != record["uid"]
                or metadata.st_gid != record["gid"]
            ):
                raise RuntimeError("a restored database's ownership or mode does not match")
            with sqlite3.connect(
                f"file:/proc/self/fd/{descriptor}?mode=ro&immutable=1", uri=True
            ) as connection:
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("a restored database failed quick_check")
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("a restored database failed foreign_key_check")
            verified += 1
        finally:
            os.close(descriptor)
    return verified


def _ensure_application_directory(data_root_fd, raw_relative, uid, gid):
    parts = relative_parts(
        raw_relative, "application directory", allow_root=True
    )
    descriptor = open_directory_chain(
        data_root_fd, parts, create=True, mode=0o700
    )
    try:
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, 0o750)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _database_data_path(raw_path):
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or len(relative.parts) < 2
        or relative.parts[0] != "databases"
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("signed database path cannot be assembled under /data")
    return PurePosixPath(*relative.parts[1:]).as_posix()


def _copy_database_for_application(
    evidence_root_fd, data_root_fd, record, uid, gid
):
    target_relative = _database_data_path(record["path"])
    target_parts = relative_parts(target_relative, "application database target")
    parent_relative = (
        PurePosixPath(*target_parts[:-1]).as_posix()
        if len(target_parts) > 1
        else "."
    )
    _ensure_application_directory(data_root_fd, parent_relative, uid, gid)
    parent_fd = open_directory_chain(data_root_fd, target_parts[:-1], create=False)
    source_fd = _open_relative_file(evidence_root_fd, target_relative)
    descriptor = None
    try:
        descriptor = create_child_file(
            parent_fd,
            target_parts[-1],
            flags=os.O_WRONLY,
            mode=APPLICATION_DATABASE_MODE,
        )
        offset = 0
        digest = hashlib.sha256()
        while True:
            block = os.pread(source_fd, 1024 * 1024, offset)
            if not block:
                break
            digest.update(block)
            written_offset = 0
            while written_offset < len(block):
                written = os.write(descriptor, block[written_offset:])
                if written <= 0:
                    raise OSError("short database restore write")
                written_offset += written
            offset += len(block)
        if offset != record["byte_size"] or digest.hexdigest() != record["sha256"]:
            raise RuntimeError("database evidence changed during application assembly")
        os.fchmod(descriptor, APPLICATION_DATABASE_MODE)
        os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("application database output is unsafe")
        os.fsync(parent_fd)
    except Exception:
        # A failed disposable restore remains incomplete.  Avoid pathname
        # cleanup because a concurrent replacement could redirect unlink.
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(source_fd)
        os.close(parent_fd)

    verified_fd = _open_relative_file(data_root_fd, target_relative)
    try:
        metadata = os.fstat(verified_fd)
        if (
            metadata.st_size != record["byte_size"]
            or _sha256_fd(verified_fd) != record["sha256"]
            or stat.S_IMODE(metadata.st_mode) != APPLICATION_DATABASE_MODE
            or metadata.st_uid != uid
            or metadata.st_gid != gid
        ):
            raise RuntimeError("application database copy does not match signed evidence")
        with sqlite3.connect(
            f"file:/proc/self/fd/{verified_fd}?mode=ro&immutable=1", uri=True
        ) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("application database copy failed quick_check")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("application database copy failed foreign_key_check")
    finally:
        os.close(verified_fd)


def _restore_application_sentinel(snapshot_data_fd, data_root_fd, manifest, uid, gid):
    records = _evidence_tree(manifest, "data")
    signed = next(
        (record for record in records if record["path"] == ".david-pi-storage"),
        None,
    )
    if signed is None or signed["type"] != "file":
        raise RuntimeError("signed application storage sentinel is missing")
    source_fd = _open_relative_file(snapshot_data_fd, ".david-pi-storage")
    try:
        verify_open_evidence_record(source_fd, signed, snapshot_data_fd)
        if (
            os.fstat(source_fd).st_size != signed["size"]
            or _sha256_fd(source_fd) != signed["sha256"]
            or signed["size"] != len(EXPECTED_APPLICATION_SENTINEL)
            or os.pread(source_fd, signed["size"] + 1, 0)
            != EXPECTED_APPLICATION_SENTINEL
        ):
            raise RuntimeError("signed application storage sentinel is unsafe")
    finally:
        os.close(source_fd)
    try:
        existing_fd = open_child_file(data_root_fd, ".david-pi-storage")
    except FileNotFoundError:
        copy_evidence_tree(
            snapshot_data_fd,
            data_root_fd,
            records,
            selected_paths={".david-pi-storage"},
            preserve_destination_root=True,
            clear_destination=False,
            require_distinct_filesystem=False,
        )
    else:
        os.close(existing_fd)
    _before_application_sentinel_open(data_root_fd)
    target_fd = open_child_file(data_root_fd, ".david-pi-storage")
    try:
        verify_open_evidence_record(
            target_fd,
            signed,
            data_root_fd,
            require_unique=True,
        )
        if (
            os.fstat(target_fd).st_size != len(EXPECTED_APPLICATION_SENTINEL)
            or os.pread(target_fd, len(EXPECTED_APPLICATION_SENTINEL) + 1, 0)
            != EXPECTED_APPLICATION_SENTINEL
        ):
            raise RuntimeError("restored application storage sentinel is unsafe")
        os.fchown(target_fd, uid, gid)
        os.fchmod(target_fd, 0o600)
        os.fsync(target_fd)
        expected_output = {
            **signed,
            "mode": "0600",
            "uid": uid,
            "gid": gid,
        }
        verify_open_evidence_record(
            target_fd,
            expected_output,
            data_root_fd,
            require_unique=True,
        )
    finally:
        os.close(target_fd)
    os.fsync(data_root_fd)
    return True


def assemble_application_data(snapshot_fd, destination_fd, manifest, *, uid, gid):
    if type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0:
        raise ValueError("application uid and gid must be non-negative integers")
    try:
        data_root_fd = open_child_directory(destination_fd, "data")
    except FileNotFoundError:
        data_root_fd = _create_child_directory(destination_fd, "data")
    databases_fd = open_child_directory(destination_fd, "databases")
    snapshot_data_fd = open_child_directory(snapshot_fd, "data")
    try:
        _ensure_application_directory(data_root_fd, ".", uid, gid)
        for record in sorted(manifest["databases"], key=lambda item: item["path"]):
            _copy_database_for_application(
                databases_fd, data_root_fd, record, uid, gid
            )
        for relative in REQUIRED_EMPTY_DIRECTORIES:
            _ensure_application_directory(data_root_fd, relative, uid, gid)
            directory_fd = open_directory_chain(
                data_root_fd, relative_parts(relative), create=False
            )
            try:
                if os.listdir(directory_fd):
                    raise RuntimeError("a required restore staging directory is not empty")
            finally:
                os.close(directory_fd)
        for relative in REQUIRED_WRITABLE_DIRECTORIES:
            _ensure_application_directory(data_root_fd, relative, uid, gid)
        sentinel_ready = _restore_application_sentinel(
            snapshot_data_fd, data_root_fd, manifest, uid, gid
        )
    finally:
        os.close(snapshot_data_fd)
        os.close(databases_fd)
        os.close(data_root_fd)
    return {
        "application_data_root": "data",
        "application_uid": uid,
        "application_gid": gid,
        "required_empty_directories": list(REQUIRED_EMPTY_DIRECTORIES),
        "required_writable_directories": list(REQUIRED_WRITABLE_DIRECTORIES),
        "storage_sentinel_ready": sentinel_ready,
        "application_layout_ready": sentinel_ready,
    }


def _atomic_write_report(destination, report):
    contents = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if isinstance(destination, int):
        directory_fd = os.dup(destination)
        expected_directory = os.fstat(destination)
    else:
        expected_directory = Path(destination).lstat()
        directory_fd = os.open(
            Path(destination),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    directory_metadata = os.fstat(directory_fd)
    directory_mount = fd_mount_id(directory_fd)
    if (
        directory_metadata.st_dev != expected_directory.st_dev
        or directory_metadata.st_ino != expected_directory.st_ino
        or not stat.S_ISDIR(directory_metadata.st_mode)
    ):
        os.close(directory_fd)
        raise RuntimeError("restore report parent was replaced")
    descriptor = None
    temporary_name = None
    try:
        try:
            existing = os.stat(
                "restore-report.json", dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            raise PermissionError("existing restore report is unsafe")
        for _ in range(32):
            candidate = f".restore-report.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = create_child_file(
                    directory_fd,
                    candidate,
                    flags=os.O_WRONLY,
                    mode=0o600,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if descriptor is None:
            raise FileExistsError("could not allocate a collision-free restore report")
        for offset in range(0, len(contents), 1024 * 1024):
            block = contents[offset : offset + 1024 * 1024]
            written = 0
            while written < len(block):
                count = os.write(descriptor, block[written:])
                if count <= 0:
                    raise OSError("short restore report write")
                written += count
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_dev != directory_metadata.st_dev
            or fd_mount_id(descriptor) != directory_mount
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("restore report temporary output is unsafe")
        os.close(descriptor)
        descriptor = None
        rename_child_noreplace(
            directory_fd, temporary_name, "restore-report.json"
        )
        temporary_name = None
        published_fd = open_child_file(directory_fd, "restore-report.json")
        try:
            published = os.fstat(published_fd)
            if (
                not stat.S_ISREG(published.st_mode)
                or published.st_uid != os.geteuid()
                or published.st_dev != directory_metadata.st_dev
                or fd_mount_id(published_fd) != directory_mount
                or published.st_nlink != 1
                or stat.S_IMODE(published.st_mode) != 0o600
                or published.st_size != len(contents)
                or _sha256_fd(published_fd)
                != hashlib.sha256(contents).hexdigest()
            ):
                raise RuntimeError("published restore report failed validation")
        finally:
            os.close(published_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # Leave a private failed temporary file rather than risk unlinking a
        # concurrently substituted pathname.
        os.close(directory_fd)


def restore(
    snapshot,
    destination,
    manifest_path,
    signing_key,
    mode="core",
    sample_count=100,
    *,
    expected_target_uid=0,
    application_uid=APPLICATION_UID,
    application_gid=APPLICATION_GID,
    require_mount=True,
    require_distinct_filesystem=True,
    network_isolation_verified=False,
):
    snapshot = Path(snapshot).resolve(strict=True)
    snapshot_fd = os.open(
        snapshot,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        snapshot_identity = os.fstat(snapshot_fd)
        snapshot_mount = _fd_mount_id(snapshot_fd)
        manifest = verify_manifest(
            snapshot,
            manifest_path,
            signing_key,
            verify_files=True,
            snapshot_fd=snapshot_fd,
        )
        _require_path_matches_fd(snapshot, snapshot_fd, "snapshot root")
        if (
            os.fstat(snapshot_fd).st_dev != snapshot_identity.st_dev
            or os.fstat(snapshot_fd).st_ino != snapshot_identity.st_ino
            or _fd_mount_id(snapshot_fd) != snapshot_mount
        ):
            raise RuntimeError("snapshot root descriptor changed identity")
        _after_manifest_verified(snapshot, manifest)
        _require_path_matches_fd(snapshot, snapshot_fd, "snapshot root")

        content_evidence = _evidence_tree(manifest, "data")
        selected_sample = (
            signed_sample_paths(content_evidence, sample_count)
            if mode == "sample"
            else []
        )
        if mode not in {"core", "sample", "full"}:
            raise ValueError("restore mode must be core, sample, or full")
        database_bytes = sum(record["byte_size"] for record in manifest["databases"])
        required_bytes = (
            database_bytes * 2 + manifest["configuration"]["logical_bytes"]
        )
        content_by_path = {record["path"]: record for record in content_evidence}
        if mode == "sample":
            required_bytes += sum(
                content_by_path[path]["size"] for path in selected_sample
            )
        elif mode == "full":
            required_bytes += manifest["content"]["logical_bytes"]

        snapshot, destination, destination_fd = _open_validated_target(
            snapshot,
            destination,
            required_bytes=required_bytes,
            expected_uid=expected_target_uid,
            require_mount=require_mount,
            require_distinct_filesystem=require_distinct_filesystem,
        )
        try:
            started_at = datetime.now(timezone.utc)
            _require_path_matches_fd(destination, destination_fd)
            for tree_name in ("databases", "config"):
                source_tree_fd = open_child_directory(snapshot_fd, tree_name)
                destination_tree_fd = _create_child_directory(
                    destination_fd, tree_name
                )
                try:
                    copy_evidence_tree(
                        source_tree_fd,
                        destination_tree_fd,
                        _evidence_tree(manifest, tree_name),
                        require_distinct_filesystem=require_distinct_filesystem,
                    )
                    if inventory_tree_fd(destination_tree_fd) != _evidence_tree(
                        manifest, tree_name
                    ):
                        raise RuntimeError(
                            f"restored {tree_name} tree does not match signed evidence"
                        )
                finally:
                    os.close(destination_tree_fd)
                    os.close(source_tree_fd)
                _require_path_matches_fd(destination, destination_fd)
                _require_path_matches_fd(snapshot, snapshot_fd, "snapshot root")

            sampled_files = 0
            sampled_bytes = 0
            if mode in {"sample", "full"}:
                source_data_fd = open_child_directory(snapshot_fd, "data")
                destination_data_fd = _create_child_directory(destination_fd, "data")
                try:
                    copied = copy_evidence_tree(
                        source_data_fd,
                        destination_data_fd,
                        content_evidence,
                        selected_paths=(
                            set(selected_sample) if mode == "sample" else None
                        ),
                        require_distinct_filesystem=require_distinct_filesystem,
                    )
                    if mode == "full" and inventory_tree_fd(
                        destination_data_fd
                    ) != content_evidence:
                        raise RuntimeError(
                            "full restored data tree does not match signed evidence"
                        )
                    if mode == "full":
                        sampled_files = sum(
                            record["type"] == "file" for record in copied
                        )
                        sampled_bytes = sum(
                            record.get("size", 0)
                            for record in copied
                            if record["type"] == "file"
                        )
                    else:
                        sampled_files = len(selected_sample)
                        sampled_bytes = sum(
                            content_by_path[path]["size"]
                            for path in selected_sample
                        )
                finally:
                    os.close(destination_data_fd)
                    os.close(source_data_fd)
                _require_path_matches_fd(destination, destination_fd)
                _require_path_matches_fd(snapshot, snapshot_fd, "snapshot root")

            database_count = verify_restored_databases(destination_fd, manifest)
            _require_path_matches_fd(destination, destination_fd)
            layout = assemble_application_data(
                snapshot_fd,
                destination_fd,
                manifest,
                uid=application_uid,
                gid=application_gid,
            )
            _require_path_matches_fd(destination, destination_fd)
            _require_path_matches_fd(snapshot, snapshot_fd, "snapshot root")
            completed_at = datetime.now(timezone.utc)
            report = {
                "schema_version": 1,
                "state": (
                    "isolated_data_verified"
                    if network_isolation_verified
                    else "data_verified"
                ),
                "mode": mode,
                "snapshot_id": manifest["snapshot_id"],
                "manifest_sha256": manifest["integrity"]["payload_sha256"],
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_seconds": (completed_at - started_at).total_seconds(),
                "database_count": database_count,
                "database_tree_sha256": manifest["database_tree"]["tree_sha256"],
                "database_tree_file_count": manifest["database_tree"]["file_count"],
                "database_evidence_root": "databases",
                "signed_database_evidence_preserved": True,
                "content_tree_verified_before_application_assembly": mode == "full",
                "sampled_file_count": sampled_files,
                "sampled_bytes": sampled_bytes,
                "network_isolation_verified": bool(network_isolation_verified),
                "application_boot_verified": False,
                "drill_complete": False,
                **layout,
            }
            _before_final_target_validation(destination_fd)
            _validate_target_descendants(destination_fd)
            _atomic_write_report(destination_fd, report)
            _validate_target_descendants(destination_fd)
            _require_path_matches_fd(destination, destination_fd)
            return report
        finally:
            os.close(destination_fd)
    finally:
        os.close(snapshot_fd)


def parse_args():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--snapshot", type=Path)
    source.add_argument(
        "--latest-snapshots",
        type=Path,
        help="resolve the hardened latest link inside this snapshots directory",
    )
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--signing-key-file", type=Path, required=True)
    parser.add_argument(
        "--receipt-directory",
        type=Path,
        help=(
            "publish a signed, content-neutral result into an already "
            "sentinel-protected private directory"
        ),
    )
    parser.add_argument("--mode", choices=("core", "sample", "full"), default="core")
    parser.add_argument("--sample-count", type=int, default=100)
    parser.add_argument("--application-uid", type=int, default=APPLICATION_UID)
    parser.add_argument("--application-gid", type=int, default=APPLICATION_GID)
    return parser.parse_args()


def verify_network_isolation():
    current_namespace = os.stat("/proc/self/ns/net").st_ino
    host_namespace = os.stat("/proc/1/ns/net").st_ino
    interfaces = {path.name for path in Path("/sys/class/net").iterdir()}
    if current_namespace == host_namespace or interfaces - {"lo"}:
        raise RuntimeError("restore drill must run in a separate loopback-only network namespace")
    return True


def main():
    args = parse_args()
    isolated = verify_network_isolation()
    if args.latest_snapshots is not None:
        if args.manifest is not None:
            raise ValueError("an explicit manifest cannot be used with latest snapshots")
        snapshots = args.latest_snapshots
        snapshot, _snapshot_id = resolve_latest(snapshots, snapshots / "latest")
    else:
        snapshot = args.snapshot.resolve()
    manifest = args.manifest or snapshot / "MANIFEST.json"
    signing_key = load_signing_key(args.signing_key_file)
    report = restore(
        snapshot,
        args.destination,
        manifest,
        signing_key,
        mode=args.mode,
        sample_count=max(0, args.sample_count),
        application_uid=args.application_uid,
        application_gid=args.application_gid,
        network_isolation_verified=isolated,
    )
    if args.receipt_directory is not None:
        receipt = create_restore_receipt(report, signing_key)
        publish_restore_receipt(args.receipt_directory, receipt)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
