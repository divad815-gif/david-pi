#!/usr/bin/env python3
"""Create and verify privacy-safe evidence for an isolated restore pass.

The receipt intentionally proves only what ``david_pi_restore_drill.py`` can
observe itself: authenticated data was restored and checked in a loopback-only
network namespace.  It cannot claim that the application booted or that a full
disaster-recovery exercise completed.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from pathlib import Path

from david_pi_fd_tree import create_child_file, fd_mount_id, open_child_file
from david_pi_snapshot_manifest import (
    SNAPSHOT_ID,
    UTC_FORMAT,
    sign_authenticated_payload,
    strict_utc_timestamp,
    verify_authenticated_payload,
)


RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KIND = "david-pi-isolated-restore-evidence"
RECEIPT_FILE = "latest-restore-evidence.json"
RECEIPT_SENTINEL = ".david-pi-restore-evidence"
RECEIPT_SENTINEL_VALUE = b"david-pi-restore-evidence-v1\n"
MAX_RECEIPT_BYTES = 16 * 1024
DEFAULT_MAX_AGE_SECONDS = 90 * 24 * 60 * 60
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_ISSUANCE_DELAY_SECONDS = 15 * 60
HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
CONTENT_SCOPES = {
    "core": "databases_and_configuration",
    "sample": "signed_deterministic_sample",
    "full": "complete_signed_data_tree",
}
RECEIPT_KEYS = {
    "schema_version",
    "kind",
    "receipt_id",
    "issued_at",
    "restore_completed_at",
    "result",
    "scope",
    "mode",
    "content_verification",
    "snapshot_id",
    "manifest_payload_sha256",
    "network_isolation_verified",
    "signed_database_evidence_preserved",
    "application_boot_verified",
    "disaster_recovery_complete",
}


class ReceiptStaleError(ValueError):
    """The receipt is authentic but is outside its accepted time window."""


class ReceiptBindingError(ValueError):
    """The receipt is authentic but describes a different current snapshot."""


def _canonical_now(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("receipt time must be timezone-aware")
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime(UTC_FORMAT)


def _report_completion(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("restore report completion is invalid")
    try:
        completed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("restore report completion is invalid") from error
    if completed.tzinfo is None or completed.utcoffset() is None:
        raise ValueError("restore report completion is invalid")
    return completed.astimezone(timezone.utc)


def create_restore_receipt(
    report: dict,
    signing_key: bytes,
    *,
    issued_at: datetime | None = None,
    receipt_id: str | None = None,
) -> dict:
    """Sign a content-neutral receipt for one successfully isolated restore."""
    if not isinstance(report, dict):
        raise ValueError("restore report must be an object")
    if report.get("state") != "isolated_data_verified":
        raise ValueError("restore report does not prove isolated data verification")
    required_true = (
        "network_isolation_verified",
        "signed_database_evidence_preserved",
        "application_layout_ready",
        "storage_sentinel_ready",
    )
    if any(report.get(field) is not True for field in required_true):
        raise ValueError("restore report is missing required successful evidence")
    if (
        report.get("application_boot_verified") is not False
        or report.get("drill_complete") is not False
    ):
        raise ValueError("restore report makes an unsupported completion claim")
    mode = report.get("mode")
    if mode not in CONTENT_SCOPES:
        raise ValueError("restore report mode is invalid")
    snapshot_id = report.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("restore report snapshot id is invalid")
    manifest_digest = report.get("manifest_sha256")
    if not isinstance(manifest_digest, str) or not HEX_256.fullmatch(manifest_digest):
        raise ValueError("restore report manifest digest is invalid")

    completed = _report_completion(report.get("completed_at")).replace(microsecond=0)
    issuance_text = _canonical_now(issued_at)
    issuance = strict_utc_timestamp(issuance_text, "receipt issuance")
    delay = (issuance - completed).total_seconds()
    if delay < 0 or delay > MAX_ISSUANCE_DELAY_SECONDS:
        raise ValueError("receipt issuance is not adjacent to the restore completion")
    identifier = receipt_id or secrets.token_hex(32)
    if not isinstance(identifier, str) or not HEX_256.fullmatch(identifier):
        raise ValueError("receipt id is invalid")

    payload = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "receipt_id": identifier,
        "issued_at": issuance_text,
        "restore_completed_at": completed.strftime(UTC_FORMAT),
        "result": "passed",
        "scope": "isolated_data_restore",
        "mode": mode,
        "content_verification": CONTENT_SCOPES[mode],
        "snapshot_id": snapshot_id,
        "manifest_payload_sha256": manifest_digest,
        "network_isolation_verified": True,
        "signed_database_evidence_preserved": True,
        "application_boot_verified": False,
        "disaster_recovery_complete": False,
    }
    return sign_authenticated_payload(payload, signing_key)


def verify_restore_receipt(
    document: dict,
    signing_key: bytes,
    *,
    expected_snapshot_id: str,
    expected_manifest_sha256: str,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> dict:
    """Verify signature, schema, freshness, and current-snapshot binding."""
    payload = verify_authenticated_payload(
        document, signing_key, "restore evidence receipt"
    )
    if set(payload) != RECEIPT_KEYS:
        raise ValueError("restore evidence receipt has an invalid schema")
    if (
        payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or payload.get("kind") != RECEIPT_KIND
        or payload.get("result") != "passed"
        or payload.get("scope") != "isolated_data_restore"
    ):
        raise ValueError("restore evidence receipt claim is invalid")
    identifier = payload.get("receipt_id")
    if not isinstance(identifier, str) or not HEX_256.fullmatch(identifier):
        raise ValueError("restore evidence receipt id is invalid")
    mode = payload.get("mode")
    if mode not in CONTENT_SCOPES or payload.get("content_verification") != CONTENT_SCOPES[mode]:
        raise ValueError("restore evidence receipt mode is invalid")
    snapshot_id = payload.get("snapshot_id")
    manifest_digest = payload.get("manifest_payload_sha256")
    if not isinstance(snapshot_id, str) or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("restore evidence receipt snapshot id is invalid")
    if not isinstance(manifest_digest, str) or not HEX_256.fullmatch(manifest_digest):
        raise ValueError("restore evidence receipt manifest digest is invalid")
    if any(
        payload.get(field) is not True
        for field in (
            "network_isolation_verified",
            "signed_database_evidence_preserved",
        )
    ):
        raise ValueError("restore evidence receipt is missing required proof")
    if (
        payload.get("application_boot_verified") is not False
        or payload.get("disaster_recovery_complete") is not False
    ):
        raise ValueError("restore evidence receipt makes an unsupported completion claim")

    issued = strict_utc_timestamp(payload.get("issued_at"), "receipt issuance")
    completed = strict_utc_timestamp(
        payload.get("restore_completed_at"), "restore completion"
    )
    if completed > issued or (issued - completed).total_seconds() > MAX_ISSUANCE_DELAY_SECONDS:
        raise ValueError("restore evidence receipt timing is invalid")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("receipt verification time must be timezone-aware")
    current = current.astimezone(timezone.utc)
    age = (current - issued).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("restore evidence receipt is from the future")
    if type(max_age_seconds) is not int or max_age_seconds <= 0:
        raise ValueError("restore evidence receipt maximum age is invalid")
    if age > max_age_seconds:
        raise ReceiptStaleError("restore evidence receipt is stale")
    if (
        snapshot_id != expected_snapshot_id
        or manifest_digest != expected_manifest_sha256
    ):
        raise ReceiptBindingError(
            "restore evidence receipt does not match the current signed snapshot"
        )
    return payload


def _read_all(descriptor: int, maximum: int, label: str) -> bytes:
    chunks = []
    remaining = maximum + 1
    while remaining:
        block = os.read(descriptor, min(remaining, 64 * 1024))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    contents = b"".join(chunks)
    if len(contents) > maximum:
        raise ValueError(f"{label} exceeds its size limit")
    return contents


def _open_receipt_directory(directory: Path, expected_uid: int) -> int:
    directory = Path(directory)
    if (
        not directory.is_absolute()
        or Path(os.path.normpath(directory)) != directory
        or directory.is_symlink()
        or directory.resolve(strict=True) != directory
    ):
        raise ValueError("restore evidence directory must be an absolute canonical path")
    expected = directory.lstat()
    if (
        not stat.S_ISDIR(expected.st_mode)
        or expected.st_uid != expected_uid
        or stat.S_IMODE(expected.st_mode) != 0o700
    ):
        raise PermissionError("restore evidence directory ownership or mode is unsafe")
    descriptor = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
            or not stat.S_ISDIR(opened.st_mode)
        ):
            raise RuntimeError("restore evidence directory was replaced")
        sentinel_fd = open_child_file(descriptor, RECEIPT_SENTINEL)
        try:
            sentinel = os.fstat(sentinel_fd)
            if (
                not stat.S_ISREG(sentinel.st_mode)
                or sentinel.st_uid != expected_uid
                or sentinel.st_dev != opened.st_dev
                or sentinel.st_nlink != 1
                or stat.S_IMODE(sentinel.st_mode) != 0o600
                or fd_mount_id(sentinel_fd) != fd_mount_id(descriptor)
                or _read_all(sentinel_fd, len(RECEIPT_SENTINEL_VALUE), "restore evidence sentinel")
                != RECEIPT_SENTINEL_VALUE
            ):
                raise PermissionError("restore evidence sentinel is unsafe")
        finally:
            os.close(sentinel_fd)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_receipt_file(descriptor: int, parent_fd: int, expected_uid: int) -> None:
    metadata = os.fstat(descriptor)
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_dev != parent.st_dev
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or fd_mount_id(descriptor) != fd_mount_id(parent_fd)
    ):
        raise PermissionError("restore evidence receipt file is unsafe")


def publish_restore_receipt(
    directory: Path,
    document: dict,
    *,
    expected_uid: int | None = None,
) -> Path:
    """Atomically replace the fixed receipt through a pinned private parent."""
    owner = os.geteuid() if expected_uid is None else expected_uid
    directory = Path(directory)
    directory_fd = _open_receipt_directory(directory, owner)
    temporary_name = None
    temporary_fd = None
    try:
        try:
            existing_fd = open_child_file(directory_fd, RECEIPT_FILE)
        except FileNotFoundError:
            existing_fd = None
        if existing_fd is not None:
            try:
                _validate_receipt_file(existing_fd, directory_fd, owner)
            finally:
                os.close(existing_fd)
        contents = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(contents) > MAX_RECEIPT_BYTES:
            raise ValueError("restore evidence receipt exceeds its size limit")
        for _ in range(32):
            candidate = f".restore-evidence.{secrets.token_hex(16)}.tmp"
            try:
                temporary_fd = create_child_file(
                    directory_fd, candidate, flags=os.O_WRONLY, mode=0o600
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            break
        if temporary_fd is None:
            raise FileExistsError("could not allocate restore evidence output")
        offset = 0
        while offset < len(contents):
            written = os.write(temporary_fd, contents[offset:])
            if written <= 0:
                raise OSError("short restore evidence write")
            offset += written
        os.fchmod(temporary_fd, 0o600)
        os.fsync(temporary_fd)
        _validate_receipt_file(temporary_fd, directory_fd, owner)
        if os.fstat(temporary_fd).st_size != len(contents):
            raise RuntimeError("restore evidence output size changed")
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            RECEIPT_FILE,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        published_fd = open_child_file(directory_fd, RECEIPT_FILE)
        try:
            _validate_receipt_file(published_fd, directory_fd, owner)
            published = _read_all(
                published_fd, MAX_RECEIPT_BYTES, "restore evidence receipt"
            )
            if published != contents:
                raise RuntimeError("published restore evidence receipt changed")
        finally:
            os.close(published_fd)
        os.fsync(directory_fd)
        return directory / RECEIPT_FILE
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        # A failed private random temporary is retained as forensic evidence;
        # pathname cleanup after a failure is intentionally not attempted.
        os.close(directory_fd)


def read_restore_receipt(
    path: Path,
    *,
    expected_uid: int | None = None,
) -> dict:
    """Read the fixed receipt without following a file or parent symlink."""
    owner = os.geteuid() if expected_uid is None else expected_uid
    path = Path(path)
    if path.name != RECEIPT_FILE:
        raise ValueError("restore evidence receipt path must use the fixed filename")
    directory_fd = _open_receipt_directory(path.parent, owner)
    try:
        receipt_fd = open_child_file(directory_fd, RECEIPT_FILE)
        try:
            _validate_receipt_file(receipt_fd, directory_fd, owner)
            contents = _read_all(
                receipt_fd, MAX_RECEIPT_BYTES, "restore evidence receipt"
            )
        finally:
            os.close(receipt_fd)
    finally:
        os.close(directory_fd)
    try:
        document = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("restore evidence receipt is not valid UTF-8 JSON") from error
    if not isinstance(document, dict):
        raise ValueError("restore evidence receipt must be an object")
    return document
