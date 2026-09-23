#!/usr/bin/env python3
"""Create and verify content-neutral manifests for independent data snapshots."""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from david_pi_fd_tree import (
    create_child_file,
    fd_mount_id,
    inventory_tree_fd,
    open_child_directory,
    open_child_file,
    open_directory_chain,
    validate_evidence,
    verify_open_evidence_record,
)


MANIFEST_SCHEMA_VERSION = 4
RECOVERY_EVIDENCE_SCHEMA_VERSION = 1
RECOVERY_EVIDENCE_FILE = "RESTORE-EVIDENCE.json"
MINIMUM_SIGNING_KEY_BYTES = 32
TREE_KEYS = {
    "file_count", "directory_count", "symlink_count", "logical_bytes",
    "unique_inode_count", "unique_inode_bytes", "tree_sha256",
}
DATABASE_RECORD_KEYS = {
    "path", "byte_size", "mode", "uid", "gid", "sha256", "quick_check",
    "foreign_key_errors",
}
SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024
EVIDENCE_RECORD_KEYS = {"path", "byte_size", "sha256"}


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fd_mount_id(descriptor):
    """Return Linux's mount identity for a pinned descriptor, or fail closed."""
    try:
        contents = Path(f"/proc/self/fdinfo/{descriptor}").read_text(
            encoding="ascii"
        )
    except (OSError, UnicodeError) as error:
        raise RuntimeError("could not establish snapshot mount identity") from error
    for line in contents.splitlines():
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return int(value)
    raise RuntimeError("snapshot mount identity is unavailable")


def _path_mount_id(path):
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path, flags)
    try:
        return _fd_mount_id(descriptor)
    finally:
        os.close(descriptor)


def _require_mount_id(path, expected):
    if _path_mount_id(path) != expected:
        raise RuntimeError("snapshot tree crosses a mount boundary")


def _xattr_digest(path, *, follow_symlinks=True):
    """Hash xattr names and values without placing private metadata in a manifest."""
    digest = hashlib.sha256()
    try:
        names = sorted(
            os.listxattr(path, follow_symlinks=follow_symlinks),
            key=lambda value: os.fsencode(value),
        )
    except OSError as error:
        if error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return digest.hexdigest()
        raise RuntimeError("could not inventory snapshot extended attributes") from error
    for name in names:
        try:
            value = os.getxattr(path, name, follow_symlinks=follow_symlinks)
        except OSError as error:
            raise RuntimeError("could not read a snapshot extended attribute") from error
        encoded_name = os.fsencode(name)
        digest.update(len(encoded_name).to_bytes(8, "big"))
        digest.update(encoded_name)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def require_exact_keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError(f"{label} has an invalid schema")


def sign_authenticated_payload(payload, signing_key):
    """Return the repository's canonical HMAC envelope for one JSON object.

    Recovery receipts deliberately share this implementation with snapshot
    manifests.  Keeping one signer/verifier prevents a status-only evidence
    format from quietly accepting weaker canonicalization or integrity rules.
    """
    if not isinstance(payload, dict) or "integrity" in payload:
        raise ValueError("signed payload must be an object without integrity metadata")
    encoded = canonical_json(payload)
    return {
        **payload,
        "integrity": {
            "algorithm": "hmac-sha256",
            "payload_sha256": hashlib.sha256(encoded).hexdigest(),
            "signature": hmac.new(signing_key, encoded, hashlib.sha256).hexdigest(),
        },
    }


def verify_authenticated_payload(document, signing_key, label="signed payload"):
    """Verify and return a copy of a canonical HMAC-authenticated payload."""
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be an object")
    payload = dict(document)
    integrity = payload.pop("integrity", None)
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "hmac-sha256":
        raise ValueError(f"{label} has no supported signature")
    require_exact_keys(
        integrity,
        {"algorithm", "payload_sha256", "signature"},
        f"{label} integrity",
    )
    encoded = canonical_json(payload)
    expected_payload = hashlib.sha256(encoded).hexdigest()
    expected_signature = hmac.new(signing_key, encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(
        str(integrity.get("payload_sha256", "")), expected_payload
    ):
        raise ValueError(f"{label} payload digest does not match")
    if not hmac.compare_digest(
        str(integrity.get("signature", "")), expected_signature
    ):
        raise ValueError(f"{label} signature does not match")
    return payload


def strict_utc_timestamp(value, label):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.strptime(value, UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError(f"{label} must be a calendar-valid UTC timestamp") from error
    if parsed.strftime(UTC_FORMAT) != value:
        raise ValueError(f"{label} must round-trip as canonical UTC")
    return parsed


def _snapshot_root(snapshot):
    snapshot = Path(snapshot)
    if not snapshot.is_absolute():
        raise ValueError("snapshot path must be absolute")
    metadata = snapshot.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or snapshot.resolve(strict=True) != snapshot
    ):
        raise ValueError("snapshot root must be a canonical real directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise PermissionError("snapshot root ownership or mode is unsafe")
    return snapshot, metadata


def _read_private_direct_file(
    parent, name, label, maximum, *, parent_fd=None
):
    parent, parent_metadata = _snapshot_root(parent)
    directory_fd = (
        os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if parent_fd is None
        else os.dup(parent_fd)
    )
    descriptor = None
    try:
        opened_parent = os.fstat(directory_fd)
        if (
            opened_parent.st_dev != parent_metadata.st_dev
            or opened_parent.st_ino != parent_metadata.st_ino
            or fd_mount_id(directory_fd) != _path_mount_id(parent)
        ):
            raise RuntimeError(f"{label} parent was replaced")
        try:
            descriptor = open_child_file(directory_fd, name)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise PermissionError(f"{label} must not be a symlink") from error
            raise
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_dev != parent_metadata.st_dev
            or fd_mount_id(descriptor) != fd_mount_id(directory_fd)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > maximum
        ):
            raise PermissionError(f"{label} is unsafe")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > maximum:
            raise ValueError(f"{label} is too large")
        return contents
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _atomic_write_snapshot_file(snapshot, name, contents, *, snapshot_fd=None):
    snapshot, snapshot_metadata = _snapshot_root(snapshot)
    if name not in {"MANIFEST.json", RECOVERY_EVIDENCE_FILE}:
        raise ValueError("snapshot metadata output name is not allowed")
    if snapshot_fd is None:
        directory_fd = os.open(
            snapshot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    else:
        directory_fd = os.dup(snapshot_fd)
    temporary_name = None
    descriptor = None
    try:
        opened_snapshot = os.fstat(directory_fd)
        if (
            opened_snapshot.st_dev != snapshot_metadata.st_dev
            or opened_snapshot.st_ino != snapshot_metadata.st_ino
            or opened_snapshot.st_uid != snapshot_metadata.st_uid
            or stat.S_IMODE(opened_snapshot.st_mode)
            != stat.S_IMODE(snapshot_metadata.st_mode)
        ):
            raise RuntimeError("snapshot manifest parent was replaced")
        try:
            existing = os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or existing.st_dev != snapshot_metadata.st_dev
            or existing.st_nlink != 1
        ):
            raise PermissionError("existing snapshot manifest output is unsafe")
        for _ in range(32):
            prefix = "MANIFEST" if name == "MANIFEST.json" else "RESTORE-EVIDENCE"
            candidate = f".{prefix}.{secrets.token_hex(16)}.tmp"
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
            raise FileExistsError("could not allocate a collision-free manifest output")
        temporary_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.geteuid()
            or temporary_metadata.st_dev != snapshot_metadata.st_dev
            or temporary_metadata.st_nlink != 1
        ):
            raise PermissionError("snapshot manifest temporary output is unsafe")
        offset = 0
        while offset < len(contents):
            written = os.write(descriptor, contents[offset:])
            if written <= 0:
                raise OSError("short manifest write")
            offset += written
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        published = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_uid != os.geteuid()
            or published.st_dev != snapshot_metadata.st_dev
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_size != len(contents)
        ):
            raise RuntimeError("published manifest failed validation")
        os.fsync(directory_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        # Leave a failed private temporary in place.  Cleanup by pathname could
        # unlink a concurrently substituted hardlink or mountpoint name.
        os.close(directory_fd)


def _atomic_write_manifest(snapshot, contents, *, snapshot_fd=None):
    _atomic_write_snapshot_file(
        snapshot, "MANIFEST.json", contents, snapshot_fd=snapshot_fd
    )


def _atomic_write_recovery_evidence(snapshot, contents, *, snapshot_fd=None):
    _atomic_write_snapshot_file(
        snapshot, RECOVERY_EVIDENCE_FILE, contents, snapshot_fd=snapshot_fd
    )


def validate_filesystem_identity(manifest):
    source = manifest.get("source")
    backup = manifest.get("backup")
    require_exact_keys(source, {"device", "filesystem_uuid", "st_dev"}, "snapshot source")
    require_exact_keys(backup, {"device", "filesystem_uuid", "st_dev"}, "snapshot backup")
    for label, record in (("source", source), ("backup", backup)):
        if not isinstance(record["device"], str) or not record["device"].strip():
            raise ValueError(f"snapshot {label} device is invalid")
        if not isinstance(record["filesystem_uuid"], str) or not record["filesystem_uuid"].strip():
            raise ValueError(f"snapshot {label} filesystem UUID is invalid")
        if type(record["st_dev"]) is not int or record["st_dev"] < 0:
            raise ValueError(f"snapshot {label} st_dev is invalid")
    if source["filesystem_uuid"] == backup["filesystem_uuid"]:
        raise ValueError("source and backup filesystem UUIDs must differ")
    if source["st_dev"] == backup["st_dev"]:
        raise ValueError("source and backup st_dev values must differ")


def load_signing_key(path):
    key_path = Path(path)
    key_metadata = key_path.lstat()
    if key_path.is_symlink() or not stat.S_ISREG(key_metadata.st_mode):
        raise PermissionError("snapshot manifest signing key must be a regular non-symlink file")
    if key_metadata.st_uid != os.geteuid():
        raise PermissionError("snapshot manifest signing key must be owned by the invoking user")
    key = key_path.read_bytes()
    if len(key) < MINIMUM_SIGNING_KEY_BYTES:
        raise ValueError("snapshot manifest signing key must contain at least 32 bytes")
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("snapshot manifest signing key must not be group/world accessible")
    return key


def database_record(path, root):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"snapshot database is not a regular file: {path.relative_to(root)}")
    relative = path.relative_to(root).as_posix()
    _database_path(Path(root).resolve(), relative)
    uri = f"file:{path}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        quick = connection.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise RuntimeError(f"quick_check failed for {path.relative_to(root)}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(f"foreign_key_check failed for {path.relative_to(root)}")
    return {
        "path": relative,
        "byte_size": metadata.st_size,
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "sha256": sha256_file(path),
        "quick_check": "ok",
        "foreign_key_errors": 0,
    }


def _tree_record(digest, entry_type, relative, mode, uid, gid, *fields):
    values = (
        entry_type,
        relative.as_posix(),
        format(mode, "04o"),
        str(uid),
        str(gid),
        *(str(field) for field in fields),
    )
    for value in values:
        encoded = value.encode("utf-8", errors="surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)


def _validated_symlink_target(relative, target):
    if not target or os.path.isabs(target):
        raise RuntimeError(f"snapshot contains an unsafe symlink: {relative}")
    normalized = posixpath.normpath(posixpath.join(relative.parent.as_posix(), target))
    if normalized == ".." or normalized.startswith("../"):
        raise RuntimeError(f"snapshot contains an escaping symlink: {relative}")
    return target


def tree_summary(root):
    root = Path(root)
    logical_bytes = 0
    file_count = 0
    directory_count = 0
    symlink_count = 0
    unique_inodes = {}
    inode_hashes = {}
    tree_digest = hashlib.sha256()
    if root.is_symlink():
        raise RuntimeError("snapshot tree root must not be a symlink")
    if not root.exists():
        return {
            "file_count": 0,
            "directory_count": 0,
            "symlink_count": 0,
            "logical_bytes": 0,
            "unique_inode_count": 0,
            "unique_inode_bytes": 0,
            "tree_sha256": tree_digest.hexdigest(),
        }
    if not root.is_dir():
        raise RuntimeError("snapshot tree root must be a directory")
    root_mount_id = _path_mount_id(root)
    root_stat = root.lstat()
    _tree_record(
        tree_digest,
        "root",
        PurePosixPath("."),
        stat.S_IMODE(root_stat.st_mode),
        root_stat.st_uid,
        root_stat.st_gid,
        _xattr_digest(root),
    )
    for directory, directories, names in os.walk(root, followlinks=False):
        directories.sort()
        directory_path = Path(directory)
        _require_mount_id(directory_path, root_mount_id)
        relative_directory = directory_path.relative_to(root)
        if relative_directory != Path("."):
            directory_stat = directory_path.lstat()
            _tree_record(
                tree_digest,
                "directory",
                relative_directory,
                stat.S_IMODE(directory_stat.st_mode),
                directory_stat.st_uid,
                directory_stat.st_gid,
                _xattr_digest(directory_path),
            )
            directory_count += 1
        retained_directories = []
        for name in directories:
            path = directory_path / name
            relative = path.relative_to(root)
            metadata = path.lstat()
            _require_mount_id(path, root_mount_id)
            if stat.S_ISLNK(metadata.st_mode):
                target = _validated_symlink_target(relative, os.readlink(path))
                _tree_record(
                    tree_digest,
                    "symlink",
                    relative,
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_uid,
                    metadata.st_gid,
                    target,
                    _xattr_digest(path, follow_symlinks=False),
                )
                symlink_count += 1
            elif stat.S_ISDIR(metadata.st_mode):
                retained_directories.append(name)
            else:
                raise RuntimeError(f"snapshot contains an unsupported entry: {relative}")
        directories[:] = retained_directories
        for name in sorted(names):
            path = directory_path / name
            relative = path.relative_to(root)
            metadata = path.lstat()
            _require_mount_id(path, root_mount_id)
            if stat.S_ISLNK(metadata.st_mode):
                target = _validated_symlink_target(relative, os.readlink(path))
                _tree_record(
                    tree_digest,
                    "symlink",
                    relative,
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_uid,
                    metadata.st_gid,
                    target,
                    _xattr_digest(path, follow_symlinks=False),
                )
                symlink_count += 1
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"snapshot contains an unsupported entry: {relative}")
            inode = (metadata.st_dev, metadata.st_ino)
            content_hash = inode_hashes.get(inode)
            if content_hash is None:
                content_hash = sha256_file(path)
                inode_hashes[inode] = content_hash
            _tree_record(
                tree_digest,
                "file",
                relative,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
                content_hash,
                _xattr_digest(path),
            )
            file_count += 1
            logical_bytes += metadata.st_size
            unique_inodes.setdefault(inode, metadata.st_size)
    return {
        "file_count": file_count,
        "directory_count": directory_count,
        "symlink_count": symlink_count,
        "logical_bytes": logical_bytes,
        "unique_inode_count": len(unique_inodes),
        "unique_inode_bytes": sum(unique_inodes.values()),
        "tree_sha256": tree_digest.hexdigest(),
    }


def database_inventory(root):
    """Return every database and reject non-database or symlinked entries."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("snapshot database tree root must be a real directory")
    root_mount_id = _path_mount_id(root)
    databases = []
    for directory, directories, names in os.walk(root, followlinks=False):
        directories.sort()
        directory_path = Path(directory)
        _require_mount_id(directory_path, root_mount_id)
        retained = []
        for name in directories:
            child = directory_path / name
            metadata = child.lstat()
            _require_mount_id(child, root_mount_id)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("snapshot database tree must not contain symlinks")
            if not stat.S_ISDIR(metadata.st_mode):
                raise RuntimeError("snapshot database tree contains an unsupported entry")
            retained.append(name)
        directories[:] = retained
        for name in sorted(names):
            path = directory_path / name
            metadata = path.lstat()
            _require_mount_id(path, root_mount_id)
            if stat.S_ISLNK(metadata.st_mode):
                raise RuntimeError("snapshot database tree must not contain symlinks")
            if not stat.S_ISREG(metadata.st_mode) or path.suffix != ".db":
                raise RuntimeError("snapshot database tree contains a non-database entry")
            databases.append(path)
    return sorted(databases)


def _database_path(snapshot, raw_path):
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("snapshot database path is invalid")
    relative = PurePosixPath(raw_path)
    if (
        relative.is_absolute()
        or "\\" in raw_path
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw_path
        or len(raw_path) > 500
        or relative.parts[:1] != ("databases",)
    ):
        raise ValueError("snapshot database path escapes the database tree")
    cursor = snapshot
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("snapshot database path traverses a symlink")
    return snapshot.joinpath(*relative.parts)


def _tree_inventories(snapshot, *, snapshot_fd=None):
    snapshot, snapshot_metadata = _snapshot_root(snapshot)
    if snapshot_fd is None:
        root_fd = os.open(
            snapshot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    else:
        root_fd = os.dup(snapshot_fd)
    try:
        opened = os.fstat(root_fd)
        if (
            opened.st_dev != snapshot_metadata.st_dev
            or opened.st_ino != snapshot_metadata.st_ino
            or fd_mount_id(root_fd) != _path_mount_id(snapshot)
        ):
            raise RuntimeError("snapshot root was replaced during evidence inventory")
        inventories = {}
        for name in ("databases", "data", "config"):
            child_fd = open_child_directory(root_fd, name)
            try:
                inventories[name] = inventory_tree_fd(child_fd)
            finally:
                os.close(child_fd)
        current = snapshot.lstat()
        if (
            current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
            or _path_mount_id(snapshot) != fd_mount_id(root_fd)
        ):
            raise RuntimeError("snapshot root was replaced during evidence inventory")
        return inventories
    finally:
        os.close(root_fd)


def _recovery_evidence_contents(snapshot, *, snapshot_fd=None):
    evidence = {
        "schema_version": RECOVERY_EVIDENCE_SCHEMA_VERSION,
        "trees": _tree_inventories(snapshot, snapshot_fd=snapshot_fd),
    }
    return evidence, canonical_json(evidence) + b"\n"


def _validate_recovery_evidence(contents, record):
    require_exact_keys(record, EVIDENCE_RECORD_KEYS, "recovery evidence record")
    if record.get("path") != RECOVERY_EVIDENCE_FILE:
        raise ValueError("recovery evidence path is invalid")
    if type(record.get("byte_size")) is not int or record["byte_size"] < 1:
        raise ValueError("recovery evidence size is invalid")
    if record["byte_size"] != len(contents):
        raise RuntimeError("recovery evidence size does not match")
    digest = hashlib.sha256(contents).hexdigest()
    if not hmac.compare_digest(str(record.get("sha256", "")), digest):
        raise RuntimeError("recovery evidence hash does not match")
    try:
        evidence = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("recovery evidence is not valid UTF-8 JSON") from error
    require_exact_keys(
        evidence, {"schema_version", "trees"}, "recovery evidence"
    )
    if evidence["schema_version"] != RECOVERY_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported recovery evidence schema")
    require_exact_keys(
        evidence["trees"], {"databases", "data", "config"}, "recovery evidence trees"
    )
    for records in evidence["trees"].values():
        validate_evidence(records)
    return evidence


def create_manifest(
    snapshot,
    metadata,
    signing_key,
    *,
    snapshot_fd=None,
    recovery_evidence_contents=None,
):
    snapshot, _ = _snapshot_root(snapshot)
    if recovery_evidence_contents is None:
        _evidence, recovery_evidence_contents = _recovery_evidence_contents(
            snapshot, snapshot_fd=snapshot_fd
        )
    snapshot_id = metadata.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("snapshot id is invalid")
    started = strict_utc_timestamp(metadata.get("started_at"), "snapshot start")
    completed = strict_utc_timestamp(metadata.get("completed_at"), "snapshot completion")
    if completed < started:
        raise ValueError("snapshot completion precedes its start")
    database_root = snapshot / "databases"
    database_paths = database_inventory(database_root)
    if not database_paths:
        raise RuntimeError("snapshot contains no SQLite databases")
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).strftime(UTC_FORMAT),
        "snapshot_id": snapshot_id,
        "window": {
            "started_at": started.strftime(UTC_FORMAT),
            "completed_at": completed.strftime(UTC_FORMAT),
            "writers_quiesced": True,
        },
        "source": {
            "device": metadata["source_device"],
            "filesystem_uuid": metadata["source_uuid"],
            "st_dev": metadata["source_st_dev"],
        },
        "backup": {
            "device": metadata["backup_device"],
            "filesystem_uuid": metadata["backup_uuid"],
            "st_dev": metadata["backup_st_dev"],
        },
        "release": {"portal_image": metadata["portal_image"]},
        "databases": [database_record(path, snapshot) for path in database_paths],
        "database_tree": tree_summary(database_root),
        "content": tree_summary(snapshot / "data"),
        "configuration": tree_summary(snapshot / "config"),
        "evidence": {
            "path": RECOVERY_EVIDENCE_FILE,
            "byte_size": len(recovery_evidence_contents),
            "sha256": hashlib.sha256(recovery_evidence_contents).hexdigest(),
        },
    }
    validate_filesystem_identity(payload)
    if payload["database_tree"]["file_count"] != len(payload["databases"]):
        raise RuntimeError("snapshot database tree changed during inventory")
    if payload["database_tree"]["symlink_count"] != 0:
        raise RuntimeError("snapshot database tree contains a symlink")
    try:
        signed_evidence = json.loads(recovery_evidence_contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("recovery evidence is invalid") from error
    if _tree_inventories(snapshot, snapshot_fd=snapshot_fd) != signed_evidence.get("trees"):
        raise RuntimeError("snapshot changed while recovery evidence was created")
    return sign_authenticated_payload(payload, signing_key)


def write_manifest(snapshot, output, metadata, signing_key, *, snapshot_fd=None):
    snapshot, _ = _snapshot_root(snapshot)
    output = Path(output)
    if output != snapshot / "MANIFEST.json":
        raise ValueError("snapshot manifest output must be the direct MANIFEST.json child")
    try:
        existing = output.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_uid != os.geteuid()
        or existing.st_dev != snapshot.lstat().st_dev
        or existing.st_nlink != 1
    ):
        raise PermissionError("existing snapshot manifest output is unsafe")
    _evidence, evidence_contents = _recovery_evidence_contents(
        snapshot, snapshot_fd=snapshot_fd
    )
    manifest = create_manifest(
        snapshot,
        metadata,
        signing_key,
        snapshot_fd=snapshot_fd,
        recovery_evidence_contents=evidence_contents,
    )
    contents = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write_recovery_evidence(
        snapshot, evidence_contents, snapshot_fd=snapshot_fd
    )
    _atomic_write_manifest(snapshot, contents, snapshot_fd=snapshot_fd)
    return manifest


def verify_manifest(
    snapshot,
    manifest_path,
    signing_key,
    verify_files=True,
    *,
    snapshot_fd=None,
):
    snapshot, _ = _snapshot_root(snapshot)
    manifest_path = Path(manifest_path)
    if manifest_path != snapshot / "MANIFEST.json":
        raise ValueError("snapshot manifest must be the direct MANIFEST.json child")
    try:
        manifest = json.loads(
            _read_private_direct_file(
                snapshot,
                "MANIFEST.json",
                "snapshot manifest",
                MAX_MANIFEST_BYTES,
                parent_fd=snapshot_fd,
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot manifest is not valid UTF-8 JSON") from error
    require_exact_keys(
        manifest,
        {
            "schema_version", "created_at", "snapshot_id", "window", "source",
            "backup", "release", "databases", "database_tree", "content",
            "configuration", "evidence", "integrity",
        },
        "snapshot manifest",
    )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot manifest schema")
    document = manifest
    manifest = verify_authenticated_payload(
        document, signing_key, "snapshot manifest"
    )
    integrity = dict(document["integrity"])
    snapshot_id = manifest.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("snapshot id is invalid")
    created = strict_utc_timestamp(manifest.get("created_at"), "manifest creation")
    validate_filesystem_identity(manifest)
    require_exact_keys(manifest.get("window"), {"started_at", "completed_at", "writers_quiesced"}, "snapshot window")
    if manifest["window"].get("writers_quiesced") is not True:
        raise ValueError("snapshot window does not record quiesced writers")
    started = strict_utc_timestamp(manifest["window"].get("started_at"), "snapshot start")
    completed = strict_utc_timestamp(manifest["window"].get("completed_at"), "snapshot completion")
    if completed < started:
        raise ValueError("snapshot completion precedes its start")
    if created < started:
        raise ValueError("manifest creation precedes snapshot start")
    require_exact_keys(manifest.get("release"), {"portal_image"}, "snapshot release")
    evidence_contents = _read_private_direct_file(
        snapshot,
        RECOVERY_EVIDENCE_FILE,
        "recovery evidence",
        MAX_EVIDENCE_BYTES,
        parent_fd=snapshot_fd,
    )
    recovery_evidence = _validate_recovery_evidence(
        evidence_contents, manifest.get("evidence")
    )
    for field in ("database_tree", "content", "configuration"):
        require_exact_keys(manifest.get(field), TREE_KEYS, f"snapshot {field} summary")
    if verify_files:
        records = manifest.get("databases")
        if not isinstance(records, list) or not records:
            raise ValueError("snapshot manifest has no database records")
        seen_paths = set()
        for record in records:
            require_exact_keys(record, DATABASE_RECORD_KEYS, "snapshot database record")
            path = _database_path(snapshot, record.get("path"))
            if record["path"] in seen_paths:
                raise ValueError("snapshot database path is duplicated")
            seen_paths.add(record["path"])
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                metadata = None
            if (
                metadata is None
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != record["byte_size"]
            ):
                raise RuntimeError(f"snapshot database is missing or changed: {record['path']}")
            if sha256_file(path) != record["sha256"]:
                raise RuntimeError(f"snapshot database hash mismatch: {record['path']}")
            if database_record(path, snapshot) != record:
                raise RuntimeError(f"snapshot database metadata mismatch: {record['path']}")
        actual_database_tree = tree_summary(snapshot / "databases")
        if actual_database_tree != manifest.get("database_tree"):
            raise RuntimeError("snapshot database tree is missing or changed")
        if (
            actual_database_tree["file_count"] != len(records)
            or actual_database_tree["symlink_count"] != 0
        ):
            raise RuntimeError("snapshot database tree inventory is not exact")
        if {path.relative_to(snapshot).as_posix() for path in database_inventory(snapshot / "databases")} != seen_paths:
            raise RuntimeError("snapshot database inventory does not match its tree")
        for field, directory in (("content", "data"), ("configuration", "config")):
            actual = tree_summary(snapshot / directory)
            if actual != manifest.get(field):
                raise RuntimeError(f"snapshot {field} tree is missing or changed")
        actual_inventories = _tree_inventories(
            snapshot, snapshot_fd=snapshot_fd
        )
        if actual_inventories != recovery_evidence["trees"]:
            raise RuntimeError("snapshot differs from signed recovery evidence")
        if snapshot_fd is not None:
            database_root_fd = open_child_directory(snapshot_fd, "databases")
            try:
                evidence_files = {
                    record["path"]: record
                    for record in recovery_evidence["trees"]["databases"]
                    if record["type"] == "file"
                }
                expected_paths = set()
                for record in manifest["databases"]:
                    relative = PurePosixPath(record["path"])
                    evidence_path = PurePosixPath(*relative.parts[1:]).as_posix()
                    evidence_record = evidence_files.get(evidence_path)
                    if evidence_record is None:
                        raise RuntimeError(
                            "database record is absent from signed recovery evidence"
                        )
                    if (
                        evidence_record["size"] != record["byte_size"]
                        or evidence_record["sha256"] != record["sha256"]
                        or evidence_record["mode"] != record["mode"]
                        or evidence_record["uid"] != record["uid"]
                        or evidence_record["gid"] != record["gid"]
                    ):
                        raise RuntimeError(
                            "database record differs from signed recovery evidence"
                        )
                    parts = tuple(relative.parts[1:])
                    parent_fd = open_directory_chain(
                        database_root_fd, parts[:-1], create=False
                    )
                    try:
                        database_fd = open_child_file(parent_fd, parts[-1])
                    finally:
                        os.close(parent_fd)
                    try:
                        verify_open_evidence_record(
                            database_fd,
                            evidence_record,
                            database_root_fd,
                        )
                        with sqlite3.connect(
                            f"file:/proc/self/fd/{database_fd}?mode=ro&immutable=1",
                            uri=True,
                        ) as connection:
                            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                                raise RuntimeError("snapshot database failed pinned quick_check")
                            if connection.execute("PRAGMA foreign_key_check").fetchall():
                                raise RuntimeError(
                                    "snapshot database failed pinned foreign_key_check"
                                )
                    finally:
                        os.close(database_fd)
                    expected_paths.add(evidence_path)
                if expected_paths != set(evidence_files):
                    raise RuntimeError(
                        "database inventory differs from signed recovery evidence"
                    )
            finally:
                os.close(database_root_fd)
    return {
        **manifest,
        "integrity": integrity,
        "_recovery_evidence": recovery_evidence,
    }


def parse_args():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--snapshot", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--signing-key-file", type=Path, required=True)
    for name in (
        "snapshot-id", "started-at", "completed-at", "source-device", "source-uuid",
        "backup-device", "backup-uuid", "portal-image",
    ):
        create.add_argument(f"--{name}", required=True)
    create.add_argument("--source-st-dev", required=True, type=int)
    create.add_argument("--backup-st-dev", required=True, type=int)
    create.add_argument("--snapshot-fd", type=int)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--snapshot", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--signing-key-file", type=Path, required=True)
    verify.add_argument("--snapshot-fd", type=int)
    return parser.parse_args()


def main():
    args = parse_args()
    key = load_signing_key(args.signing_key_file)
    if args.command == "create":
        metadata = {
            "snapshot_id": args.snapshot_id,
            "started_at": args.started_at,
            "completed_at": args.completed_at,
            "source_device": args.source_device,
            "source_uuid": args.source_uuid,
            "backup_device": args.backup_device,
            "backup_uuid": args.backup_uuid,
            "source_st_dev": args.source_st_dev,
            "backup_st_dev": args.backup_st_dev,
            "portal_image": args.portal_image,
        }
        manifest = write_manifest(
            args.snapshot,
            args.output,
            metadata,
            key,
            snapshot_fd=args.snapshot_fd,
        )
    else:
        manifest = verify_manifest(
            args.snapshot,
            args.manifest,
            key,
            verify_files=True,
            snapshot_fd=args.snapshot_fd,
        )
    print(json.dumps({
        "state": "healthy",
        "snapshot_id": manifest["snapshot_id"],
        "database_count": len(manifest["databases"]),
        "payload_sha256": manifest["integrity"]["payload_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
