"""Owner-bound phone backup APIs integrated with the canonical media library."""

from __future__ import annotations

import hashlib
import hmac
import base64
import fcntl
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from PIL import UnidentifiedImageError
import qrcode

from .android_release import available_android_release
from .identity import current_device
from .installation import public_url, display_name as installation_display_name, get_installation, module_enabled, module_catalog
from .secure_storage import ensure_restricted_directory


TOKEN_TTL_SECONDS = 600
PAIRING_FAILURE_WINDOW_SECONDS = 15 * 60
PAIRING_FAILURE_LIMIT = 5
PAIRING_GLOBAL_FAILURE_LIMIT = 50
PAIRING_TOKEN_FAILURE_LIMIT = 10
MAX_CHUNK_BYTES = int(os.environ.get("DAVID_PI_BACKUP_CHUNK_MAX", 8 * 1024 * 1024))
IOS_MAX_FILE_BYTES = int(os.environ.get("DAVID_PI_IOS_BACKUP_FILE_MAX", 8 * 1024 * 1024 * 1024))
MIN_FREE_BYTES = int(os.environ.get("DAVID_PI_BACKUP_MIN_FREE", 1024 * 1024 * 1024))
PART_RETENTION_SECONDS = int(os.environ.get("DAVID_PI_BACKUP_PART_RETENTION", 14 * 86400))
PRIMARY_SENTINEL = Path(os.environ.get("DAVID_PI_DATA_SENTINEL", "/data/.david-pi-storage"))
PRIMARY_SENTINEL_ID = os.environ.get("DAVID_PI_DATA_ID", "david-pi-family-storage-v1")
SECONDARY_ROOT = os.environ.get("DAVID_PI_SECONDARY_BACKUP_ROOT", "").strip()
SECONDARY_SENTINEL_ID = os.environ.get("DAVID_PI_SECONDARY_DATA_ID", "")
SECONDARY_LEASE_SECONDS = 60 * 60
SECONDARY_MAX_ATTEMPTS = 5
UPLOAD_IO_LEASE_SECONDS = int(
    os.environ.get("DAVID_PI_BACKUP_IO_LEASE_SECONDS", "300")
)
SAFE_NAME = re.compile(r"[^A-Za-z0-9._() -]+")
RECONCILIATION_MAX_ITEMS = int(
    os.environ.get("DAVID_PI_RECONCILIATION_MAX_ITEMS", "50000")
)
RECONCILIATION_MIN_INTERVAL_SECONDS = int(
    os.environ.get("DAVID_PI_RECONCILIATION_MIN_INTERVAL", "60")
)
RECONCILIATION_MAX_BODY_BYTES = int(
    os.environ.get("DAVID_PI_RECONCILIATION_MAX_BODY", str(12 * 1024 * 1024))
)
RECONCILIATION_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
RECONCILIATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}")
RECONCILIATION_FIELDS = {
    "scan_id",
    "complete",
    "item_count",
    "ids_sha256",
    "visible_client_item_ids",
}


def configured_ios_shortcut_url() -> str:
    """Return only an Apple iCloud Shortcut share URL; never trust arbitrary links."""
    value = os.environ.get("IOS_SHORTCUT_ICLOUD_URL", "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.icloud.com"
        or parsed.username
        or parsed.password
        or not re.fullmatch(r"/shortcuts/[A-Za-z0-9]+/?", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        current_app.logger.warning("Ignoring invalid IOS_SHORTCUT_ICLOUD_URL")
        return ""
    return value.rstrip("/")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def cleanup_stale_upload_sessions(db_context, incoming_root: Path, *, now: float | None = None) -> int:
    """Retire stale incomplete rows and their staging files as one bounded sweep."""
    cutoff_epoch = (time.time() if now is None else now) - PART_RETENTION_SECONDS
    cutoff_text = datetime.fromtimestamp(cutoff_epoch, timezone.utc).isoformat()
    retired = 0
    staged_ingests: list[tuple[Path, Path]] = []
    try:
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
            """SELECT id,device_id,part_path FROM device_uploads
               WHERE state IN ('uploading','retryable_error') AND updated_at<=?""",
            (cutoff_text,),
            ).fetchall()
            resolved_incoming = incoming_root.resolve()
            for row in rows:
                raw_path = row["part_path"]
                if raw_path:
                    part = Path(raw_path)
                    try:
                        device_root = incoming_root / row["device_id"]
                        expected_root = device_root.resolve()
                        resolved = part.resolve()
                        if (
                            not device_root.is_symlink()
                            and expected_root.is_relative_to(resolved_incoming)
                            and not part.is_symlink()
                            and resolved.is_relative_to(expected_root)
                            and (not part.exists() or part.is_file())
                        ):
                            part.unlink(missing_ok=True)
                    except OSError:
                        # The database lease is safe to retire even when a corrupt
                        # or unavailable staging path cannot be touched. Orphan
                        # cleanup remains confined to the dedicated incoming root.
                        pass
                changed = connection.execute(
                    """DELETE FROM device_uploads
                       WHERE id=? AND device_id=?
                         AND state IN ('uploading','retryable_error') AND updated_at<=?""",
                    (row["id"], row["device_id"], cutoff_text),
                )
                retired += changed.rowcount

            # An ingesting row may be retired only before any canonical
            # publication evidence exists. Rename the exact staging inode first,
            # then repeat every predicate in the serialized DELETE transaction.
            # Canonical preparation takes its own BEGIN IMMEDIATE and validates
            # this row, so cleanup and publication cannot both win.
            ingesting = connection.execute(
                """SELECT upload.id,upload.device_id,upload.part_path,upload.media_id
                   FROM device_uploads AS upload
                   JOIN device_ingest_intents AS intent
                     ON intent.id=upload.media_id
                    AND intent.upload_id=upload.id
                    AND intent.device_id=upload.device_id
                   WHERE upload.state='ingesting' AND upload.updated_at<=?
                     AND intent.state='prepared'
                     AND NOT EXISTS (SELECT 1 FROM photos WHERE id=upload.media_id)
                     AND NOT EXISTS (
                         SELECT 1 FROM device_media_records
                         WHERE media_id=upload.media_id
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM media_publish_intents
                         WHERE id=upload.media_id
                     )
                   ORDER BY upload.id""",
                (cutoff_text,),
            ).fetchall()
            for row in ingesting:
                part = Path(row["part_path"] or "")
                device_root = incoming_root / row["device_id"]
                expected_root = device_root.resolve()
                retiring = part.with_name(
                    f"{part.name}.retiring-stale-"
                    f"{hashlib.sha256(row['id'].encode('utf-8')).hexdigest()[:16]}"
                )
                try:
                    if (
                        not row["part_path"]
                        or not part.name.endswith(".part")
                        or device_root.is_symlink()
                        or not expected_root.is_relative_to(resolved_incoming)
                        or part.is_symlink()
                        or retiring.is_symlink()
                        or not part.resolve().is_relative_to(expected_root)
                        or not retiring.resolve().is_relative_to(expected_root)
                    ):
                        raise UploadRetirementError(
                            "A stale ingest has an unsafe staging path."
                        )
                    if part.exists():
                        if not part.is_file() or retiring.exists():
                            raise UploadRetirementError(
                                "A stale ingest has an ambiguous staging path."
                            )
                        os.replace(part, retiring)
                    elif retiring.exists():
                        if not retiring.is_file():
                            raise UploadRetirementError(
                                "A stale ingest has an unsafe retirement path."
                            )
                    else:
                        retiring = None
                except OSError as error:
                    raise UploadRetirementError(
                        "A stale ingest staging path could not be retired."
                    ) from error
                staged_pair = (part, retiring) if retiring is not None else None
                if staged_pair is not None:
                    staged_ingests.append(staged_pair)
                changed = connection.execute(
                    """DELETE FROM device_uploads AS upload
                       WHERE upload.id=? AND upload.device_id=?
                         AND upload.state='ingesting' AND upload.updated_at<=?
                         AND upload.media_id=?
                         AND EXISTS (
                             SELECT 1 FROM device_ingest_intents AS intent
                             WHERE intent.id=upload.media_id
                               AND intent.upload_id=upload.id
                               AND intent.device_id=upload.device_id
                               AND intent.state='prepared'
                         )
                         AND NOT EXISTS (SELECT 1 FROM photos WHERE id=upload.media_id)
                         AND NOT EXISTS (
                             SELECT 1 FROM device_media_records
                             WHERE media_id=upload.media_id
                         )
                         AND NOT EXISTS (
                             SELECT 1 FROM media_publish_intents
                             WHERE id=upload.media_id
                         )""",
                    (row["id"], row["device_id"], cutoff_text, row["media_id"]),
                )
                if changed.rowcount != 1:
                    if staged_pair is not None:
                        os.replace(retiring, part)
                        staged_ingests.remove(staged_pair)
                    continue
                deleted_intent = connection.execute(
                    """DELETE FROM device_ingest_intents
                       WHERE id=? AND device_id=? AND upload_id=? AND state='prepared'
                         AND NOT EXISTS (SELECT 1 FROM photos WHERE id=?)
                         AND NOT EXISTS (
                             SELECT 1 FROM device_media_records WHERE media_id=?
                         )
                         AND NOT EXISTS (
                             SELECT 1 FROM media_publish_intents WHERE id=?
                         )""",
                    (
                        row["media_id"], row["device_id"], row["id"],
                        row["media_id"], row["media_id"], row["media_id"],
                    ),
                )
                if deleted_intent.rowcount != 1:
                    raise UploadRetirementError(
                        "A stale device publication intent changed during retirement."
                    )
                retired += 1

            referenced = {
                row["part_path"]
                for row in connection.execute(
                    "SELECT part_path FROM device_uploads WHERE part_path IS NOT NULL"
                ).fetchall()
            }
    except BaseException:
        restore_staged_uploads(staged_ingests)
        raise

    for _, retiring in staged_ingests:
        try:
            retiring.unlink(missing_ok=True)
        except OSError:
            pass

    # Preserve orphan cleanup, but never use a missing database row as
    # permission to touch anything outside the dedicated incoming tree.
    if incoming_root.is_dir():
        resolved_incoming = incoming_root.resolve()
        for part in (
            *incoming_root.glob("*/*.part"),
            *incoming_root.glob("*/*.part.retiring-*"),
        ):
            try:
                if (
                    str(part) not in referenced
                    and part.is_file()
                    and not part.is_symlink()
                    and part.resolve().is_relative_to(resolved_incoming)
                    and part.stat().st_mtime < cutoff_epoch
                ):
                    part.unlink()
                    retired += 1
            except OSError:
                continue
    return retired


class UploadRetirementError(RuntimeError):
    pass


class UploadPartBusy(RuntimeError):
    pass


class UploadLeaseLost(RuntimeError):
    pass


def _confined_upload_part(
    incoming_root: Path,
    device_id: str,
    upload_id: str,
    raw_path: str | None,
) -> Path:
    """Resolve only the deterministic direct-child staging path for an upload."""
    if not raw_path:
        raise ValueError("The upload has no staging path.")
    resolved_incoming = incoming_root.resolve()
    device_root = incoming_root / device_id
    if device_root.is_symlink():
        raise ValueError("The upload device directory is unsafe.")
    expected_root = device_root.resolve()
    if not expected_root.is_relative_to(resolved_incoming):
        raise ValueError("The upload device directory escaped its staging root.")
    part = Path(raw_path)
    expected = device_root / f"{upload_id}.part"
    if (
        part.is_symlink()
        or part.parent.resolve() != expected_root
        or part.resolve() != expected.resolve()
    ):
        raise ValueError("The upload staging path is unsafe.")
    return part


def _open_locked_upload_part(part: Path, *, append: bool) -> tuple[int, os.stat_result]:
    flags = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_WRONLY | os.O_APPEND if append else os.O_RDONLY
    descriptor = os.open(part, flags)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(descriptor)
        raise UploadPartBusy("The upload staging file is in use.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("The upload staging path is not a regular file.")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _descriptor_matches_path(
    descriptor: int, part: Path, expected: os.stat_result | None = None
) -> tuple[bool, os.stat_result]:
    metadata = os.fstat(descriptor)
    try:
        current = os.stat(part, follow_symlinks=False)
    except FileNotFoundError:
        return False, metadata
    baseline = expected or metadata
    matches = (
        stat.S_ISREG(metadata.st_mode)
        and stat.S_ISREG(current.st_mode)
        and metadata.st_dev == baseline.st_dev
        and metadata.st_ino == baseline.st_ino
        and current.st_dev == metadata.st_dev
        and current.st_ino == metadata.st_ino
    )
    return matches, metadata


def _sha256_upload_descriptor(
    descriptor: int, progress: Callable[[], None] | None = None
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb", closefd=True) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            if progress is not None:
                progress()
    return digest.hexdigest()


def _append_request_to_descriptor(
    descriptor: int,
    source,
    content_length: int,
    progress: Callable[[], None] | None = None,
) -> int:
    written = 0
    while written < content_length:
        chunk = source.read(min(1024 * 1024, content_length - written))
        if not chunk:
            break
        view = memoryview(chunk)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise OSError("The upload staging write made no progress.")
            view = view[count:]
            written += count
        if progress is not None:
            progress()
    os.fsync(descriptor)
    return written


def stage_absent_incomplete_uploads(
    connection,
    incoming_root: Path,
    device_id: str,
    scan_id: str,
) -> tuple[list[str], list[tuple[Path, Path]]]:
    """Move this device's absent incomplete parts aside before DB retirement."""
    rows = connection.execute(
        """SELECT id,part_path FROM device_uploads AS upload
           WHERE upload.device_id=?
             AND upload.state IN ('uploading','retryable_error')
             AND upload.io_lease_token IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM reconciliation_visible_ids AS visible
                 WHERE visible.client_item_id=upload.client_item_id
             )
           ORDER BY upload.id""",
        (device_id,),
    ).fetchall()
    expected_root = (incoming_root / device_id).resolve()
    candidates: list[tuple[Path, Path, bool]] = []
    for row in rows:
        if not row["part_path"]:
            continue
        part = Path(row["part_path"])
        retiring = part.with_name(f"{part.name}.retiring-{scan_id}")
        try:
            resolved = part.resolve()
            retiring_resolved = retiring.resolve()
            if (
                part.is_symlink()
                or not resolved.is_relative_to(expected_root)
                or not retiring_resolved.is_relative_to(expected_root)
                or (part.exists() and not part.is_file())
                or retiring.is_symlink()
                or (retiring.exists() and not retiring.is_file())
                or (part.exists() and retiring.exists())
            ):
                raise UploadRetirementError("An absent upload has an unsafe staging path.")
        except OSError as error:
            raise UploadRetirementError(
                "An absent upload staging path could not be validated."
            ) from error
        if part.exists():
            candidates.append((part, retiring, True))
        elif retiring.exists():
            # A process may have stopped after the exact deterministic rename
            # but before SQLite committed. Adopt that same scan's staged inode
            # so replay can finish the receipt instead of failing forever.
            candidates.append((part, retiring, False))

    staged: list[tuple[Path, Path]] = []
    try:
        for part, retiring, needs_move in candidates:
            if needs_move:
                os.replace(part, retiring)
            staged.append((part, retiring))
    except OSError as error:
        for original, retiring in reversed(staged):
            try:
                os.replace(retiring, original)
            except OSError:
                pass
        raise UploadRetirementError(
            "An absent upload staging file could not be retired."
        ) from error
    return [row["id"] for row in rows], staged


def restore_staged_uploads(staged: list[tuple[Path, Path]]) -> None:
    for original, retiring in reversed(staged):
        if retiring.exists() and not original.exists():
            os.replace(retiring, original)


def canonical_reconciliation_ids(values) -> list[str]:
    """Validate and canonicalize one complete Android MediaStore observation.

    IDs are deliberately restricted to a portable ASCII alphabet. This makes
    Python and Kotlin sorting byte-for-byte identical and prevents separator or
    Unicode-normalization ambiguities in digested receipts.
    """
    if not isinstance(values, list):
        raise ValueError("visible_client_item_ids must be an array")
    if len(values) > RECONCILIATION_MAX_ITEMS:
        raise OverflowError("complete scan exceeds the configured item limit")
    canonical: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not RECONCILIATION_ID.fullmatch(value):
            raise ValueError("visible_client_item_ids contains an invalid ID")
        canonical.add(value)
    if len(canonical) > RECONCILIATION_MAX_ITEMS:
        raise OverflowError("complete scan exceeds the configured item limit")
    return sorted(canonical)


def reconciliation_ids_sha256(values: list[str]) -> str:
    """Digest canonical IDs as compact UTF-8 JSON (no ambiguous delimiter)."""
    encoded = json.dumps(
        values, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def canonical_uuid7(value) -> str:
    raw = value if isinstance(value, str) else ""
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("scan_id must be a canonical UUIDv7") from None
    canonical = str(parsed)
    if raw != canonical or parsed.version != 7:
        raise ValueError("scan_id must be a canonical UUIDv7")
    return canonical


def uuid7_timestamp_ms(value: str) -> int:
    return int(uuid.UUID(value).hex[:12], 16)


def safe_filename(value: str) -> str:
    value = SAFE_NAME.sub("_", Path(value or "media").name).strip(" .")
    return value[:240] or "media"


def normalize_capture_timestamp(value: str) -> str:
    """Return a stable UTC cursor or an empty string for unparseable input."""
    raw = str(value or "").strip()[:64]
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def initialize_device_backup(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS device_pairing_tokens (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            manual_code_hash TEXT NOT NULL UNIQUE,
            owner_user_id TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            invalidated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS device_pairing_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS device_pairing_failures_source_time_idx
            ON device_pairing_failures(source_key, attempted_at);
        CREATE TABLE IF NOT EXISTS backup_devices (
            id TEXT PRIMARY KEY,
            credential_hash TEXT NOT NULL UNIQUE,
            owner_user_id TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'android',
            created_at TEXT NOT NULL,
            last_contact_at TEXT,
            last_reconciliation_at TEXT,
            revoked_at TEXT,
            settings_json TEXT NOT NULL DEFAULT '{}',
            uploaded_items INTEGER NOT NULL DEFAULT 0,
            uploaded_bytes INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS backup_devices_owner_idx
            ON backup_devices(owner_user_id, revoked_at);
        CREATE TABLE IF NOT EXISTS device_uploads (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES backup_devices(id) ON DELETE CASCADE,
            client_item_id TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            expected_size INTEGER NOT NULL,
            expected_sha256 TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            capture_timestamp TEXT,
            accepted_offset INTEGER NOT NULL DEFAULT 0,
            part_path TEXT,
            state TEXT NOT NULL,
            media_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error_code TEXT,
            io_lease_token TEXT,
            io_lease_kind TEXT,
            io_lease_expires_at TEXT,
            UNIQUE(device_id, client_item_id)
        );
        CREATE INDEX IF NOT EXISTS device_uploads_device_state_idx
            ON device_uploads(device_id, state, updated_at);
        CREATE TABLE IF NOT EXISTS device_media_records (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES backup_devices(id) ON DELETE CASCADE,
            client_item_id TEXT NOT NULL,
            media_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            owner_user_id TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            capture_timestamp TEXT,
            ingestion_source TEXT NOT NULL DEFAULT 'android_backup',
            primary_verification_state TEXT NOT NULL,
            secondary_verification_state TEXT NOT NULL,
            secondary_attempts INTEGER NOT NULL DEFAULT 0,
            secondary_lease_token TEXT,
            secondary_lease_expires_at TEXT,
            local_source_visible INTEGER NOT NULL DEFAULT 1,
            ingested_at TEXT NOT NULL,
            UNIQUE(device_id, client_item_id)
        );
        CREATE INDEX IF NOT EXISTS device_media_owner_idx
            ON device_media_records(owner_user_id, ingested_at DESC);
        CREATE INDEX IF NOT EXISTS device_media_hash_idx
            ON device_media_records(content_sha256);
        CREATE TABLE IF NOT EXISTS device_ingest_intents (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES backup_devices(id) ON DELETE CASCADE,
            client_item_id TEXT NOT NULL,
            upload_id TEXT,
            original_filename TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            capture_timestamp TEXT,
            ingestion_source TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','committed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(device_id, client_item_id)
        );
        CREATE INDEX IF NOT EXISTS device_ingest_intents_state_idx
            ON device_ingest_intents(state,updated_at,id);
        CREATE TABLE IF NOT EXISTS device_sync_runs (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES backup_devices(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            discovered_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            completed_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            transferred_bytes INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS device_backup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            upload_id TEXT,
            level TEXT NOT NULL,
            event_code TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS device_backup_events_time_idx
            ON device_backup_events(created_at DESC);
        CREATE TABLE IF NOT EXISTS device_reconciliation_receipts (
            device_id TEXT NOT NULL REFERENCES backup_devices(id) ON DELETE CASCADE,
            scan_id TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            ids_sha256 TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            known_visible_count INTEGER NOT NULL,
            changed_count INTEGER NOT NULL,
            PRIMARY KEY(device_id, scan_id)
        );
        CREATE INDEX IF NOT EXISTS device_reconciliation_receipts_latest_idx
            ON device_reconciliation_receipts(device_id, scan_id DESC);
        """
    )
    # Gunicorn workers import the application independently.  Serialize the
    # additive inspection/ALTER phase so two fresh workers cannot race it.
    connection.execute("BEGIN IMMEDIATE")
    pairing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(device_pairing_tokens)")
    }
    if "failed_attempts" not in pairing_columns:
        connection.execute(
            "ALTER TABLE device_pairing_tokens "
            "ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "invalidated_at" not in pairing_columns:
        connection.execute(
            "ALTER TABLE device_pairing_tokens ADD COLUMN invalidated_at TEXT"
        )
    upload_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(device_uploads)")
    }
    if "io_lease_token" not in upload_columns:
        connection.execute(
            "ALTER TABLE device_uploads ADD COLUMN io_lease_token TEXT"
        )
    if "io_lease_kind" not in upload_columns:
        connection.execute(
            "ALTER TABLE device_uploads ADD COLUMN io_lease_kind TEXT"
        )
    if "io_lease_expires_at" not in upload_columns:
        connection.execute(
            "ALTER TABLE device_uploads ADD COLUMN io_lease_expires_at TEXT"
        )
    media_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(device_media_records)")
    }
    if "secondary_attempts" not in media_columns:
        connection.execute(
            "ALTER TABLE device_media_records "
            "ADD COLUMN secondary_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "secondary_lease_token" not in media_columns:
        connection.execute(
            "ALTER TABLE device_media_records ADD COLUMN secondary_lease_token TEXT"
        )
    if "secondary_lease_expires_at" not in media_columns:
        connection.execute(
            "ALTER TABLE device_media_records ADD COLUMN secondary_lease_expires_at TEXT"
        )
    connection.execute(
        """CREATE INDEX IF NOT EXISTS device_media_secondary_work_idx
           ON device_media_records(
               secondary_verification_state,secondary_lease_expires_at,ingested_at
           )"""
    )


def primary_storage_ready(data_root: Path) -> bool:
    try:
        expected = PRIMARY_SENTINEL.read_text(encoding="utf-8").strip()
        return (
            expected == PRIMARY_SENTINEL_ID
            and data_root.resolve().is_dir()
            and PRIMARY_SENTINEL.resolve().is_relative_to(data_root.resolve())
        )
    except (OSError, ValueError):
        return False


class SecondaryHashMismatch(RuntimeError):
    pass


def _verified_file_sha256(
    path: Path, progress: Callable[[], None] | None = None
) -> str:
    if path.is_symlink() or not path.is_file():
        raise OSError("Secondary verification requires a regular file.")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if progress is not None:
                progress()
    return digest.hexdigest()


def _fsync_directory_chain(directory: Path, root: Path) -> None:
    """Durably publish a file and any newly-created parents below root."""
    current = directory
    while True:
        if not current.is_relative_to(root):
            raise OSError("Secondary destination escaped its storage root.")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(current, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("Secondary destination parent is not a directory.")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if current == root:
            return
        current = current.parent


def secondary_verify_once(
    db_context,
    data_root: Path,
    secondary_root: Path,
    sentinel_id: str,
    *,
    progress: Callable[[], None] | None = None,
) -> bool:
    """Claim, copy, and lease-fence one retryable secondary content hash."""
    sentinel = secondary_root / ".david-pi-secondary-storage"
    try:
        resolved_root = secondary_root.resolve()
        if (
            not sentinel_id
            or sentinel.is_symlink()
            or not sentinel.is_file()
            or not sentinel.resolve().is_relative_to(resolved_root)
            or sentinel.read_text(encoding="utf-8").strip() != sentinel_id
            or not resolved_root.is_dir()
        ):
            return False
    except (OSError, ValueError):
        return False

    claimed_at = datetime.now(timezone.utc)
    claimed_at_text = claimed_at.isoformat()
    lease_expires = (claimed_at + timedelta(seconds=SECONDARY_LEASE_SECONDS)).isoformat()
    lease_token = uuid.uuid4().hex
    with db_context() as connection:
        connection.execute("BEGIN IMMEDIATE")
        candidate = connection.execute(
            """SELECT content_sha256 FROM device_media_records
               WHERE secondary_attempts<? AND (
                   secondary_verification_state IN (
                       'secondary_pending','secondary_error','secondary_hash_mismatch'
                   ) OR (
                       secondary_verification_state='secondary_copying'
                       AND (
                           secondary_lease_expires_at IS NULL
                           OR secondary_lease_expires_at<=?
                       )
                   )
               )
               ORDER BY CASE secondary_verification_state
                            WHEN 'secondary_pending' THEN 0
                            WHEN 'secondary_copying' THEN 1
                            ELSE 2
                        END,ingested_at,id LIMIT 1""",
            (SECONDARY_MAX_ATTEMPTS, claimed_at_text),
        ).fetchone()
        if not candidate:
            return False
        content_hash = candidate["content_sha256"]
        connection.execute(
            """UPDATE device_media_records
               SET secondary_verification_state='secondary_copying',
                   secondary_attempts=secondary_attempts+1,
                   secondary_lease_token=?,secondary_lease_expires_at=?
               WHERE content_sha256=? AND secondary_attempts<? AND (
                   secondary_verification_state IN (
                       'secondary_pending','secondary_error','secondary_hash_mismatch'
                   ) OR (
                       secondary_verification_state='secondary_copying'
                       AND (
                           secondary_lease_expires_at IS NULL
                           OR secondary_lease_expires_at<=?
                       )
                   )
               )""",
            (
                lease_token,
                lease_expires,
                content_hash,
                SECONDARY_MAX_ATTEMPTS,
                claimed_at_text,
            ),
        )
        claimed = connection.execute(
            """SELECT d.id,d.media_id,d.secondary_attempts,p.stored_path
               FROM device_media_records AS d
               JOIN photos AS p ON p.id=d.media_id
               WHERE d.secondary_lease_token=?
               ORDER BY p.stored_path,d.id""",
            (lease_token,),
        ).fetchall()
        for media_id in {row["media_id"] for row in claimed}:
            connection.execute(
                "UPDATE photos SET secondary_verification_state='secondary_copying' WHERE id=?",
                (media_id,),
            )
    if not claimed:
        return False
    if progress is not None:
        progress()

    originals = (data_root / "originals").resolve()
    secondary_originals = (resolved_root / "originals").resolve()
    outcome = "fully_protected"
    for stored_path in dict.fromkeys(row["stored_path"] for row in claimed):
        source = (data_root / "originals" / stored_path).resolve()
        destination = (resolved_root / "originals" / stored_path).resolve()
        temporary = destination.with_name(f"{destination.name}.part-{lease_token}")
        try:
            if (
                not source.is_relative_to(originals)
                or not destination.is_relative_to(secondary_originals)
                or source.is_symlink()
                or not source.is_file()
                or destination.is_symlink()
            ):
                raise OSError("Secondary copy path is unsafe.")
            if destination.exists():
                if _verified_file_sha256(destination, progress) == content_hash:
                    _fsync_directory_chain(destination.parent, resolved_root)
                    continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.resolve().is_relative_to(secondary_originals):
                raise OSError("Secondary destination escaped its storage root.")
            digest = hashlib.sha256()
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    digest.update(chunk)
                    outgoing.write(chunk)
                    if progress is not None:
                        progress()
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if not hmac.compare_digest(digest.hexdigest(), content_hash):
                raise SecondaryHashMismatch("Primary content hash changed during copy.")
            os.replace(temporary, destination)
            if not hmac.compare_digest(
                _verified_file_sha256(destination, progress), content_hash
            ):
                raise SecondaryHashMismatch("Published secondary content did not verify.")
            _fsync_directory_chain(destination.parent, resolved_root)
        except SecondaryHashMismatch:
            outcome = "secondary_hash_mismatch"
            break
        except OSError:
            outcome = "secondary_error"
            break
        finally:
            try:
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()
            except OSError:
                pass

    with db_context() as connection:
        connection.execute("BEGIN IMMEDIATE")
        owned = connection.execute(
            """SELECT id,media_id,secondary_attempts FROM device_media_records
               WHERE secondary_lease_token=? AND secondary_verification_state='secondary_copying'""",
            (lease_token,),
        ).fetchall()
        media_states: dict[str, list[str]] = {}
        for record in owned:
            record_state = outcome
            if outcome != "fully_protected" and record["secondary_attempts"] >= SECONDARY_MAX_ATTEMPTS:
                record_state = "secondary_failed"
            changed = connection.execute(
                """UPDATE device_media_records
                   SET secondary_verification_state=?,secondary_lease_token=NULL,
                       secondary_lease_expires_at=NULL
                   WHERE id=? AND secondary_lease_token=?
                     AND secondary_verification_state='secondary_copying'""",
                (record_state, record["id"], lease_token),
            )
            if changed.rowcount == 1:
                media_states.setdefault(record["media_id"], []).append(record_state)
        for media_id, states in media_states.items():
            photo_state = (
                "fully_protected"
                if all(state == "fully_protected" for state in states)
                else outcome
                if any(state != "secondary_failed" for state in states)
                else "secondary_failed"
            )
            connection.execute(
                "UPDATE photos SET secondary_verification_state=? WHERE id=?",
                (photo_state, media_id),
            )
    if progress is not None:
        progress()
    return outcome == "fully_protected" and bool(media_states)


def prepare_device_ingest_intent(
    connection,
    *,
    device_id: str,
    client_item_id: str,
    upload_id: str | None,
    original_filename: str,
    content_sha256: str,
    byte_size: int,
    mime_type: str,
    capture_timestamp: str | None,
    ingestion_source: str,
    now: str,
):
    """Persist or validate the stable identity of one device publication."""
    existing = connection.execute(
        """SELECT * FROM device_ingest_intents
           WHERE device_id=? AND client_item_id=?""",
        (device_id, client_item_id),
    ).fetchone()
    expected = {
        "device_id": device_id,
        "client_item_id": client_item_id,
        "upload_id": upload_id,
        "original_filename": original_filename,
        "content_sha256": content_sha256,
        "byte_size": int(byte_size),
        "mime_type": mime_type,
        "capture_timestamp": capture_timestamp,
        "ingestion_source": ingestion_source,
    }
    if existing:
        if any(existing[key] != value for key, value in expected.items()):
            raise sqlite3.IntegrityError(
                "A device item was reused with conflicting publication metadata."
            )
        return dict(existing)
    media_id = uuid.uuid4().hex
    connection.execute(
        """INSERT INTO device_ingest_intents
           (id,device_id,client_item_id,upload_id,original_filename,content_sha256,
            byte_size,mime_type,capture_timestamp,ingestion_source,state,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
        (
            media_id,
            device_id,
            client_item_id,
            upload_id,
            original_filename,
            content_sha256,
            int(byte_size),
            mime_type,
            capture_timestamp,
            ingestion_source,
            now,
            now,
        ),
    )
    return dict(
        connection.execute(
            "SELECT * FROM device_ingest_intents WHERE id=?", (media_id,)
        ).fetchone()
    )


def bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def admitted_device_owner(owner_id: str) -> bool:
    """Membership is checked on every bearer use, including already paired phones."""
    config = get_installation()
    return config is None or any(member["login"] == owner_id for member in config["members"])


def authenticate_device(
    connection: sqlite3.Connection, *, touch_last_contact: bool = True
):
    credential = bearer_token()
    if len(credential) < 32:
        return None
    digest = token_hash(credential)
    row = connection.execute(
        "SELECT * FROM backup_devices WHERE credential_hash=? AND revoked_at IS NULL",
        (digest,),
    ).fetchone()
    if row and not admitted_device_owner(row["owner_user_id"]):
        return None
    if row and touch_last_contact:
        connection.execute(
            "UPDATE backup_devices SET last_contact_at=? WHERE id=?", (utcnow(), row["id"])
        )
    return row


def error(
    code: str,
    message: str,
    status: int,
    retry_after: int | None = None,
    *,
    details: dict | None = None,
):
    payload = dict(details or {})
    # Reserved protocol fields cannot be replaced accidentally by details.
    payload.update(code=code, message=message)
    response = jsonify(error=payload)
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def pairing_source_key() -> str:
    """Return a stable, non-plaintext rate-limit key.

    The digest keeps identity and address values out of the throttle table and
    out of operational logs.  A verified Tailscale identity is preferred so a
    client cannot bypass the limiter merely by changing its network address.
    """
    actor = current_device()
    owner_user_id = str(actor.get("owner_id") or "").strip().casefold() or None
    source = (
        f"identity:{owner_user_id}"
        if owner_user_id
        else f"address:{request.remote_addr or 'missing'}"
    )
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def pairing_failure_counts(
    connection: sqlite3.Connection, source_key: str, cutoff: str
) -> tuple[int, int]:
    connection.execute(
        "DELETE FROM device_pairing_failures WHERE attempted_at<?", (cutoff,)
    )
    row = connection.execute(
        """SELECT COUNT(*) AS global_failures,
                  COALESCE(SUM(CASE WHEN source_key=? THEN 1 ELSE 0 END),0)
                      AS source_failures
           FROM device_pairing_failures WHERE attempted_at>=?""",
        (source_key, cutoff),
    ).fetchone()
    return (
        int(row["source_failures"] if isinstance(row, sqlite3.Row) else row[1]),
        int(row["global_failures"] if isinstance(row, sqlite3.Row) else row[0]),
    )


def record_pairing_failure(
    connection: sqlite3.Connection,
    source_key: str,
    now: str,
) -> None:
    """Persist one failure and invalidate active tokens at ten total failures.

    Pairing codes intentionally never enter this table, application logs, or
    error responses.  Applying failures to every currently active, short-lived
    token ensures an unknown or missing identity cannot avoid token invalidation
    merely because the submitted guess cannot identify its intended token.
    The tradeoff is a bounded denial of pairing: an attacked user must generate
    a fresh code.  It cannot grant access or alter an already paired device.
    """
    connection.execute(
        "INSERT INTO device_pairing_failures(source_key,attempted_at) VALUES (?,?)",
        (source_key, now),
    )
    connection.execute(
        """UPDATE device_pairing_tokens
           SET failed_attempts=failed_attempts+1,
               invalidated_at=CASE
                   WHEN failed_attempts+1>=? THEN ? ELSE invalidated_at END
           WHERE consumed_at IS NULL AND invalidated_at IS NULL AND expires_at>?""",
        (PAIRING_TOKEN_FAILURE_LIMIT, now, now),
    )


def init_device_backup(
    app, db_context, canonical_ingest, rollback_ingest, data_root: Path
) -> None:
    blueprint = Blueprint("device_backup", __name__)
    incoming_root = data_root / "incoming" / "device-backup"
    app_root = Path(app.root_path)

    def current_android_release():
        return available_android_release(app_root, app.logger)

    ensure_restricted_directory(incoming_root)

    with db_context() as connection:
        initialize_device_backup(connection)

    def cleanup_stale_parts() -> int:
        return cleanup_stale_upload_sessions(db_context, incoming_root)

    def advance_ios_cursor(connection, device_id: str, capture_timestamp: str, now: str) -> str:
        """Advance an iPhone cursor monotonically after an item is safely present."""
        cursor = normalize_capture_timestamp(capture_timestamp)
        row = connection.execute(
            "SELECT settings_json FROM backup_devices WHERE id=?", (device_id,)
        ).fetchone()
        settings = json.loads((row["settings_json"] if row else "{}") or "{}")
        previous = normalize_capture_timestamp(settings.get("ios_capture_cursor", ""))
        if cursor and (not previous or cursor > previous):
            settings["ios_capture_cursor"] = cursor
            settings["ios_checkpoint_at"] = now
            connection.execute(
                "UPDATE backup_devices SET settings_json=?,last_contact_at=? WHERE id=?",
                (json.dumps(settings, separators=(",", ":")), now, device_id),
            )
            return cursor
        return previous

    def publication_callback(intent_id: str):
        """Link a canonical photo to its device transaction in the same commit."""

        def publish(connection, photo) -> None:
            intent = connection.execute(
                """SELECT intent.*,device.owner_user_id,device.owner_name
                   FROM device_ingest_intents AS intent
                   JOIN backup_devices AS device ON device.id=intent.device_id
                   WHERE intent.id=?""",
                (intent_id,),
            ).fetchone()
            if not intent:
                raise sqlite3.IntegrityError("Device publication intent disappeared.")
            if (
                photo is None
                or photo["id"] != intent["id"]
                or photo["owner_id"] != intent["owner_user_id"]
                or photo["source_device_id"] != intent["device_id"]
                or photo["ingestion_source"] != intent["ingestion_source"]
                or photo["content_sha256"] != intent["content_sha256"]
                or int(photo["byte_size"]) != int(intent["byte_size"])
            ):
                raise sqlite3.IntegrityError(
                    "Canonical media does not match its device publication intent."
                )
            record = connection.execute(
                """SELECT * FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (intent["device_id"], intent["client_item_id"]),
            ).fetchone()
            expected_record = {
                "media_id": intent["id"],
                "owner_user_id": intent["owner_user_id"],
                "original_filename": intent["original_filename"],
                "content_sha256": intent["content_sha256"],
                "byte_size": int(intent["byte_size"]),
                "capture_timestamp": intent["capture_timestamp"],
                "ingestion_source": intent["ingestion_source"],
            }
            if record is None:
                connection.execute(
                    """INSERT INTO device_media_records
                       (id,device_id,client_item_id,media_id,owner_user_id,
                        original_filename,content_sha256,byte_size,capture_timestamp,
                        ingestion_source,primary_verification_state,
                        secondary_verification_state,ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        intent["device_id"],
                        intent["client_item_id"],
                        intent["id"],
                        intent["owner_user_id"],
                        intent["original_filename"],
                        intent["content_sha256"],
                        intent["byte_size"],
                        intent["capture_timestamp"],
                        intent["ingestion_source"],
                        "primary_verified",
                        "secondary_pending",
                        intent["updated_at"],
                    ),
                )
            elif any(record[key] != value for key, value in expected_record.items()):
                raise sqlite3.IntegrityError(
                    "Device media record conflicts with its publication intent."
                )

            now = utcnow()
            transitioned = connection.execute(
                """UPDATE device_ingest_intents
                   SET state='committed',updated_at=?
                   WHERE id=? AND state='prepared'""",
                (now, intent["id"]),
            ).rowcount
            if intent["upload_id"]:
                upload = connection.execute(
                    "SELECT * FROM device_uploads WHERE id=? AND device_id=?",
                    (intent["upload_id"], intent["device_id"]),
                ).fetchone()
                if (
                    not upload
                    or upload["client_item_id"] != intent["client_item_id"]
                    or upload["expected_sha256"] != intent["content_sha256"]
                    or int(upload["expected_size"]) != int(intent["byte_size"])
                    or upload["state"] not in {"ingesting", "primary_verified"}
                    or (
                        upload["state"] == "primary_verified"
                        and upload["media_id"] != intent["id"]
                    )
                ):
                    raise sqlite3.IntegrityError(
                        "Device upload conflicts with its publication intent."
                    )
                connection.execute(
                    """UPDATE device_uploads
                       SET state='primary_verified',media_id=?,accepted_offset=expected_size,
                           updated_at=?,part_path=NULL,error_code=NULL
                       WHERE id=? AND device_id=? AND state='ingesting'""",
                    (intent["id"], now, intent["upload_id"], intent["device_id"]),
                )
            if transitioned == 1:
                connection.execute(
                    """UPDATE backup_devices
                       SET uploaded_items=uploaded_items+1,
                           uploaded_bytes=uploaded_bytes+?,last_contact_at=?
                       WHERE id=?""",
                    (intent["byte_size"], now, intent["device_id"]),
                )
                if intent["ingestion_source"] == "ios_shortcut_backup":
                    advance_ios_cursor(
                        connection,
                        intent["device_id"],
                        intent["capture_timestamp"] or "",
                        now,
                    )
            elif transitioned != 0 or intent["state"] != "committed":
                raise sqlite3.IntegrityError(
                    "Device publication intent changed unexpectedly."
                )

        return publish

    def publication_precommit_validator(intent_id: str):
        """Fence canonical publication against stale-ingest retirement."""

        def validate(connection) -> None:
            row = connection.execute(
                """SELECT intent.*,upload.state AS upload_state,
                          upload.media_id AS upload_media_id,
                          upload.expected_sha256,upload.expected_size,
                          device.owner_user_id
                   FROM device_ingest_intents AS intent
                   JOIN device_uploads AS upload
                     ON upload.id=intent.upload_id AND upload.device_id=intent.device_id
                   JOIN backup_devices AS device ON device.id=intent.device_id
                   WHERE intent.id=?""",
                (intent_id,),
            ).fetchone()
            if not row:
                raise sqlite3.IntegrityError(
                    "Device upload disappeared before canonical publication."
                )
            valid_state = (
                row["state"] == "prepared" and row["upload_state"] == "ingesting"
            ) or (
                row["state"] == "committed"
                and row["upload_state"] == "primary_verified"
            )
            if (
                not valid_state
                or row["upload_media_id"] != row["id"]
                or row["expected_sha256"] != row["content_sha256"]
                or int(row["expected_size"]) != int(row["byte_size"])
                or row["owner_user_id"] is None
            ):
                raise sqlite3.IntegrityError(
                    "Device upload changed before canonical publication."
                )

        return validate

    def secondary_verify_one() -> bool:
        if not SECONDARY_ROOT or not SECONDARY_SENTINEL_ID:
            return False
        return secondary_verify_once(
            db_context,
            data_root,
            Path(SECONDARY_ROOT),
            SECONDARY_SENTINEL_ID,
        )

    def housekeeping():
        while True:
            try:
                cleanup_stale_parts()
                secondary_verify_one()
            except Exception:
                app.logger.exception("Device backup housekeeping failed")
            time.sleep(3600)

    if os.environ.get("DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING") != "1":
        threading.Thread(target=housekeeping, daemon=True).start()

    def require_portal_identity():
        actor = current_device()
        return actor if actor["verified"] and actor["owner_id"] and admitted_device_owner(actor["owner_id"]) else None

    @blueprint.get("/connect")
    @blueprint.get("/device-backup")
    def setup_page():
        actor = require_portal_identity()
        if not actor:
            return "Open this page through private Tailscale HTTPS.", 403
        with db_context() as connection:
            devices = connection.execute(
                """SELECT d.id,d.display_name,d.platform,d.created_at,d.last_contact_at,
                          d.last_reconciliation_at,d.revoked_at,d.uploaded_items,d.uploaded_bytes,
                          SUM(CASE WHEN u.state IN ('uploading','ingesting') THEN 1 ELSE 0 END)
                              AS active_uploads,
                          SUM(CASE WHEN u.state='ingesting' THEN 1 ELSE 0 END) AS stalled_ingests,
                          SUM(CASE WHEN u.state IN ('queued','retryable_error') THEN 1 ELSE 0 END)
                              AS pending_uploads,
                          SUM(CASE WHEN u.state IN ('retryable_error','permanent_error') THEN 1 ELSE 0 END)
                              AS failed_uploads
                   FROM backup_devices d LEFT JOIN device_uploads u ON u.device_id=d.id
                   WHERE d.owner_user_id=? AND d.revoked_at IS NULL GROUP BY d.id
                   ORDER BY d.created_at DESC""",
                (actor["owner_id"],),
            ).fetchall()
        android_release = current_android_release()
        return render_template(
            "device_backup.html", devices=[dict(row) for row in devices],
            apk_available=android_release is not None,
            apk_sha256=(android_release or {}).get("artifact", {}).get("sha256"),
            apk_version=(android_release or {}).get("version_name"),
            backup_enabled=module_enabled("device_backup"),
            portal_origin=public_url(),
            server_display_name=installation_display_name(),
        )

    @blueprint.post("/api/device-backup/pairing-token")
    def create_pairing_token():
        actor = require_portal_identity()
        if not actor:
            return error("tailscale_identity_required", "Open through private Tailscale HTTPS.", 403)
        if not public_url():
            return error("server_not_configured", "Complete private HTTPS setup before pairing a phone.", 503)
        token = secrets.token_urlsafe(32)
        manual = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.now(timezone.utc)
        token_id = uuid.uuid4().hex
        with db_context() as connection:
            connection.execute(
                """INSERT INTO device_pairing_tokens
                   (id,token_hash,manual_code_hash,owner_user_id,owner_name,created_at,expires_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    token_id, token_hash(token), token_hash(manual), actor["owner_id"],
                    actor["name"], now.isoformat(),
                    (now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(),
                ),
            )
        deep_link = (
            f"davidpibackup://pair?token={quote(token)}"
            f"&server={quote(public_url(), safe='')}"
        )
        qr_image = qrcode.make(deep_link)
        qr_bytes = io.BytesIO()
        qr_image.save(qr_bytes, format="PNG")
        return jsonify(
            pairing_token=token, manual_code=manual,
            server_url=public_url(),
            expires_at=(now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(),
            deep_link=deep_link,
            qr_data_uri=(
                "data:image/png;base64,"
                + base64.b64encode(qr_bytes.getvalue()).decode("ascii")
            ),
        )

    def create_ios_credential_file():
        """Create an owner-bound iPhone credential without a pairing Shortcut."""
        actor = require_portal_identity()
        if not actor:
            return error("tailscale_identity_required", "Open through private Tailscale HTTPS.", 403)
        credential, device_id = create_ios_device(actor)
        response = send_file(
            io.BytesIO(credential.encode("utf-8")),
            mimetype="text/plain; charset=utf-8",
            as_attachment=True,
            download_name="DavidPiBackupCredential.txt",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-David-Pi-Device-Id"] = device_id
        return response

    def create_ios_device(actor):
        credential = secrets.token_urlsafe(48)
        device_id = str(uuid.uuid4())
        now = utcnow()
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO backup_devices
                   (id,credential_hash,owner_user_id,owner_name,display_name,platform,
                    created_at,last_contact_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    device_id, token_hash(credential), actor["owner_id"], actor["name"],
                    "iPhone Shortcut", "ios_shortcut", now, now,
                ),
            )
        return credential, device_id

    def download_ios_shortcut():
        """Retire the unsigned export that modern iOS refuses to import."""
        actor = require_portal_identity()
        if not actor:
            return error("tailscale_identity_required", "Open through private Tailscale HTTPS.", 403)
        return error(
            "unsigned_shortcut_retired",
            "Install the Apple-shared master Shortcut from the Phone backup page, then pair it with a one-time code.",
            410,
        )

    @blueprint.post("/api/v1/device-backup/pair")
    def pair():
        payload = request.get_json(silent=True) or {}
        forbidden = {
            "owner_user_id", "owner_id", "owner", "username", "visibility",
            "destination_path", "access_control",
        }
        if forbidden.intersection(payload):
            return error(
                "server_controls_ownership",
                "Ownership and access are assigned by David-Pi during pairing.",
                422,
            )
        supplied = str(payload.get("pairing_token") or payload.get("manual_code") or "")
        platform = "ios_shortcut" if request.path.startswith("/api/v1/ios-backup/") else "android"
        default_name = "iPhone Shortcut" if platform == "ios_shortcut" else "Android phone"
        display_name = safe_filename(str(payload.get("device_name") or default_name))[:80]
        if not supplied:
            return error("pairing_token_required", "Enter the pairing code.", 400)
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        cutoff = (
            now_value - timedelta(seconds=PAIRING_FAILURE_WINDOW_SECONDS)
        ).isoformat()
        source_key = pairing_source_key()
        credential = secrets.token_urlsafe(48)
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source_failures, global_failures = pairing_failure_counts(
                connection, source_key, cutoff
            )
            if (
                source_failures >= PAIRING_FAILURE_LIMIT
                or global_failures >= PAIRING_GLOBAL_FAILURE_LIMIT
            ):
                # Ten rows are sufficient both to keep the 15-minute block and
                # to invalidate active tokens per source.  The explicit global
                # cap also stops rotating sources from growing the DB without
                # bound.  Once either cap is stored, further blocked requests
                # are read-only until the window rolls forward.
                if (
                    source_failures < PAIRING_TOKEN_FAILURE_LIMIT
                    and global_failures < PAIRING_GLOBAL_FAILURE_LIMIT
                ):
                    record_pairing_failure(
                        connection, source_key, now
                    )
                return error(
                    "pairing_rate_limited",
                    "Too many pairing attempts. Try again later.",
                    429,
                    PAIRING_FAILURE_WINDOW_SECONDS,
                )
            digest = token_hash(supplied)
            row = connection.execute(
                """SELECT * FROM device_pairing_tokens
                   WHERE (token_hash=? OR manual_code_hash=?)
                     AND consumed_at IS NULL AND invalidated_at IS NULL
                     AND failed_attempts<? AND expires_at>?""",
                (digest, digest, PAIRING_TOKEN_FAILURE_LIMIT, now),
            ).fetchone()
            if not row:
                record_pairing_failure(
                    connection, source_key, now
                )
                return error("pairing_invalid", "That pairing code is invalid or expired.", 401)
            if not admitted_device_owner(row["owner_user_id"]):
                return error("member_removed", "This person is no longer admitted to the household.", 401)
            consumed = connection.execute(
                "UPDATE device_pairing_tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, row["id"]),
            )
            if consumed.rowcount != 1:
                return error("pairing_used", "That pairing code has already been used.", 409)
            device_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO backup_devices
                   (id,credential_hash,owner_user_id,owner_name,display_name,platform,
                    created_at,last_contact_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    device_id, token_hash(credential), row["owner_user_id"],
                    row["owner_name"], display_name, platform, now, now,
                ),
            )
            connection.execute(
                "DELETE FROM device_pairing_failures WHERE source_key=?",
                (source_key,),
            )
        return jsonify(
            device_id=device_id, device_credential=credential,
            owner_name=row["owner_name"], api_version=1,
            installation_id=(get_installation() or {}).get("instance_id", ""),
            enabled_modules=[item["id"] for item in module_catalog() if item["mode"] != "disabled"],
            member_id=row["owner_user_id"],
            display_name=installation_display_name(), server_url=public_url(),
        ), 201

    @blueprint.post("/api/v1/device-backup/uploads")
    def create_upload():
        payload = request.get_json(silent=True) or {}
        forbidden = {
            "owner_user_id", "owner_id", "owner", "username", "visibility",
            "destination_path", "access_control", "collection_id", "album_id",
        }
        if forbidden.intersection(payload):
            return error(
                "server_controls_ownership",
                "The paired device owner and access settings cannot be overridden.",
                422,
            )
        try:
            expected_size = int(payload.get("byte_size"))
        except (TypeError, ValueError):
            return error("invalid_size", "A valid byte size is required.", 422)
        expected_hash = str(payload.get("sha256") or "").lower()
        if expected_size <= 0 or expected_size > 2 * 1024 * 1024 * 1024:
            return error("invalid_size", "This item is outside the supported size limit.", 413)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            return error("invalid_hash", "A SHA-256 value is required.", 422)
        client_item_id = str(payload.get("client_item_id") or "")[:200]
        if not client_item_id:
            return error("client_item_required", "A stable local item ID is required.", 422)
        original_filename = safe_filename(
            str(payload.get("original_filename") or "media")
        )
        mime_type = str(payload.get("mime_type") or "application/octet-stream")[:120]
        capture_timestamp = payload.get("capture_timestamp")
        dedupe_device = None
        dedupe_intent = None
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device:
                return error("device_unauthorized", "This phone is not paired.", 401)
            existing_item = connection.execute(
                """SELECT media_id,content_sha256,byte_size FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (device["id"], client_item_id),
            ).fetchone()
            if existing_item:
                if (
                    int(existing_item["byte_size"]) != expected_size
                    or not hmac.compare_digest(
                        existing_item["content_sha256"], expected_hash
                    )
                ):
                    return error(
                        "local_source_changed",
                        "This local item identity is already bound to different content.",
                        422,
                    )
                return jsonify(state="already_present", media_id=existing_item["media_id"])
            existing_intent = connection.execute(
                """SELECT * FROM device_ingest_intents
                   WHERE device_id=? AND client_item_id=?""",
                (device["id"], client_item_id),
            ).fetchone()
            if existing_intent:
                intent_hash = existing_intent["content_sha256"]
                intent_size = existing_intent["byte_size"]
                intent_hash_matches = (
                    isinstance(intent_hash, str)
                    and hmac.compare_digest(intent_hash, expected_hash)
                )
                intent_size_matches = (
                    isinstance(intent_size, int)
                    and not isinstance(intent_size, bool)
                    and intent_size == expected_size
                )
                if not (intent_hash_matches and intent_size_matches):
                    # Publication preparation freezes content identity.  Keep
                    # the old intent untouched and let the phone rehash/re-key
                    # the changed local generation instead of accepting its
                    # bytes under an in-flight media ID.
                    return error(
                        "local_source_changed",
                        "This local item identity already has a publication for different content.",
                        422,
                    )
                # Filename, MIME, capture time, and source are descriptive and
                # may drift after a lost response.  The persisted intent is the
                # canonical frozen metadata for an idempotent resume.
                dedupe_intent = dict(existing_intent)
                dedupe_device = dict(device)
            session = connection.execute(
                "SELECT * FROM device_uploads WHERE device_id=? AND client_item_id=?",
                (device["id"], client_item_id),
            ).fetchone()
            if dedupe_intent is None and session:
                stored_hash = session["expected_sha256"]
                stored_size = session["expected_size"]
                hash_matches = isinstance(stored_hash, str) and hmac.compare_digest(
                    stored_hash, expected_hash
                )
                size_matches = (
                    isinstance(stored_size, int)
                    and not isinstance(stored_size, bool)
                    and stored_size == expected_size
                )
                session_matches = hash_matches and size_matches
                if session["state"] == "retryable_error" or (
                    session["state"] in ("uploading", "queued")
                    and not session_matches
                ):
                    # This session ID is disclosed only after authenticating its
                    # owning device.  A phone which lost its local server ID can
                    # durably adopt and abandon the stale session, then retry
                    # without ever treating its old full staging part as the new
                    # same-size source.
                    response = error(
                        "upload_session_conflict",
                        "This local item identity has an active upload for different content.",
                        409,
                    )
                    response.headers["Upload-Id"] = session["id"]
                    return response
                if session["state"] == "permanent_error":
                    if not session_matches:
                        return error(
                            "local_source_changed",
                            "This rejected local item identity is bound to different content.",
                            422,
                        )
                    return error(
                        session["error_code"] or "invalid_media",
                        "Android returned an unreadable media item. It remains safely rejected.",
                        422,
                    )
                if session["state"] not in ("uploading", "queued"):
                    return error(
                        "upload_state_inconsistent",
                        "This upload cannot be restarted without administrator review.",
                        409,
                    )
                try:
                    part = _confined_upload_part(
                        incoming_root,
                        device["id"],
                        session["id"],
                        session["part_path"],
                    )
                except (OSError, ValueError):
                    return error(
                        "unsafe_upload_path",
                        "The upload staging path is invalid.",
                        500,
                    )
                if part.exists():
                    if not part.is_file():
                        return error(
                            "unsafe_upload_path",
                            "The upload staging path is invalid.",
                            500,
                        )
                    offset = part.stat().st_size
                    connection.execute(
                        """UPDATE device_uploads
                           SET accepted_offset=?,state='uploading',updated_at=?
                           WHERE id=? AND state IN ('uploading','queued')""",
                        (offset, utcnow(), session["id"]),
                    )
                    return jsonify(upload_id=session["id"], state="uploading", offset=offset)
                # A process can die after a crash-safe abandon rename and
                # before SQLite deletes the row. Retire that unusable row so a
                # client which received a typed missing-session response can
                # create a fresh session for the same stable local identity.
                connection.execute(
                    """DELETE FROM device_uploads
                       WHERE id=? AND device_id=? AND state IN ('uploading','queued')""",
                    (session["id"], device["id"]),
                )
                session = None
            # Preflight dedupe is an observable hash/size oracle, so it may use
            # only the caller's own logical media or content already visible to
            # the household. Global physical reuse is still safe after this
            # caller has uploaded and proved every byte in complete_upload().
            physical = None if dedupe_intent else connection.execute(
                """SELECT id FROM photos
                   WHERE COALESCE(content_sha256, sha256)=? AND byte_size=?
                     AND deleted_at IS NULL
                     AND (owner_id=? OR visibility='shared')
                   ORDER BY uploaded_at LIMIT 1""",
                (expected_hash, expected_size, device["owner_user_id"]),
            ).fetchone()
            upload_id = str(uuid.uuid4())
            now = utcnow()
            if physical:
                dedupe_device = dict(device)
                dedupe_intent = prepare_device_ingest_intent(
                    connection,
                    device_id=device["id"],
                    client_item_id=client_item_id,
                    upload_id=None,
                    original_filename=original_filename,
                    content_sha256=expected_hash,
                    byte_size=expected_size,
                    mime_type=mime_type,
                    capture_timestamp=capture_timestamp,
                    ingestion_source="android_backup",
                    now=now,
                )
            elif dedupe_intent is None:
                if not primary_storage_ready(data_root):
                    return error("primary_storage_unavailable", "Primary storage is unavailable.", 503, 60)
                if shutil.disk_usage(data_root).free < expected_size * 2 + MIN_FREE_BYTES:
                    return error("low_storage", "Primary storage does not have enough free space.", 507)
                active = connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE state='uploading'"
                ).fetchone()[0]
                if active >= int(os.environ.get("DAVID_PI_BACKUP_GLOBAL_UPLOADS", "1")):
                    return error("upload_busy", "Another backup upload is active.", 429, 30)
                device_root = incoming_root / device["id"]
                ensure_restricted_directory(device_root)
                part = device_root / f"{upload_id}.part"
                part.touch(exist_ok=False)
                connection.execute(
                    """INSERT INTO device_uploads
                       (id,device_id,client_item_id,original_filename,expected_size,expected_sha256,
                        mime_type,capture_timestamp,accepted_offset,part_path,state,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        upload_id, device["id"], client_item_id,
                        original_filename,
                        expected_size, expected_hash,
                        mime_type,
                        capture_timestamp, 0, str(part), "uploading", now, now,
                    ),
                )
        if dedupe_device:
            try:
                result = canonical_ingest(
                    staged_path=None,
                    original_filename=dedupe_intent["original_filename"],
                    mime_type=dedupe_intent["mime_type"],
                    owner_user_id=dedupe_device["owner_user_id"],
                    owner_name=dedupe_device["owner_name"],
                    visibility="shared",
                    capture_timestamp=dedupe_intent["capture_timestamp"],
                    source_device_id=dedupe_device["id"],
                    ingestion_source=dedupe_intent["ingestion_source"],
                    authoritative_sha256=dedupe_intent["content_sha256"],
                    authoritative_size=dedupe_intent["byte_size"],
                    media_id=dedupe_intent["id"],
                    publication_callback=publication_callback(dedupe_intent["id"]),
                )
            except Exception:
                current_app.logger.exception("Device backup dedupe publication failed")
                return error(
                    "ingestion_failed",
                    "The verified item could not be linked safely.",
                    500,
                )
            return jsonify(state="already_present", media_id=result["id"])
        return jsonify(upload_id=upload_id, state="uploading", offset=0, chunk_size=MAX_CHUNK_BYTES), 201

    def owned_upload(connection, upload_id):
        device = authenticate_device(connection)
        if not device:
            return None, None
        upload = connection.execute(
            "SELECT * FROM device_uploads WHERE id=? AND device_id=?",
            (upload_id, device["id"]),
        ).fetchone()
        return device, upload

    def claim_upload_io(
        connection,
        upload,
        kind,
        *,
        states=("uploading",),
        allow_active_abandon_takeover=False,
    ):
        """Acquire a short durable fence without holding SQLite during file I/O."""
        if upload["state"] not in states:
            return None
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        prior_token = upload["io_lease_token"]
        prior_kind = upload["io_lease_kind"]
        prior_expiry = upload["io_lease_expires_at"]
        available = (
            not prior_token
            or not prior_expiry
            or prior_expiry <= now
            # Only a caller which has already proved the prior abandon crossed
            # its deterministic rename boundary may adopt that live-looking
            # lease.  A second DELETE must never steal a token while the first
            # request still owns the canonical staging inode.
            or (
                kind == "abandon"
                and prior_kind == "abandon"
                and allow_active_abandon_takeover
            )
        )
        if not available:
            return None
        token = secrets.token_hex(32)
        expires_at = (
            now_value + timedelta(seconds=max(30, UPLOAD_IO_LEASE_SECONDS))
        ).isoformat()
        state_placeholders = ",".join("?" for _ in states)
        changed = connection.execute(
            f"""UPDATE device_uploads
                SET io_lease_token=?,io_lease_kind=?,io_lease_expires_at=?,updated_at=?
                WHERE id=? AND device_id=? AND state IN ({state_placeholders})
                  AND io_lease_token IS ?""",
            (
                token,
                kind,
                expires_at,
                now,
                upload["id"],
                upload["device_id"],
                *states,
                prior_token,
            ),
        )
        return token if changed.rowcount == 1 else None

    def renew_upload_io(upload_id, device_id, token, kind) -> bool:
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        expires_at = (
            now_value + timedelta(seconds=max(30, UPLOAD_IO_LEASE_SECONDS))
        ).isoformat()
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE device_uploads
                   SET io_lease_expires_at=?,updated_at=?
                   WHERE id=? AND device_id=? AND state='uploading'
                     AND io_lease_token=? AND io_lease_kind=?""",
                (expires_at, now, upload_id, device_id, token, kind),
            )
        return changed.rowcount == 1

    def release_upload_io(upload_id, device_id, token, kind) -> None:
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE device_uploads
                   SET io_lease_token=NULL,io_lease_kind=NULL,
                       io_lease_expires_at=NULL,updated_at=?
                   WHERE id=? AND device_id=? AND io_lease_token=?
                     AND io_lease_kind=?""",
                (utcnow(), upload_id, device_id, token, kind),
            )

    @blueprint.route("/api/v1/device-backup/uploads/<upload_id>", methods=["HEAD"])
    def upload_offset(upload_id):
        with db_context() as connection:
            _, upload = owned_upload(connection, upload_id)
            if not upload:
                return "", 404
            if upload["state"] in ("ingesting", "primary_verified"):
                # Completion is durable even if the phone missed the response. The
                # staging path may be under an atomic publication intent, so report
                # the verified length and let the phone replay completion rather
                # than trying to upload bytes into a non-uploading session.
                offset = upload["expected_size"]
            elif upload["state"] == "uploading" and upload["part_path"]:
                try:
                    part = _confined_upload_part(
                        incoming_root,
                        upload["device_id"],
                        upload["id"],
                        upload["part_path"],
                    )
                except (OSError, ValueError):
                    return error(
                        "unsafe_upload_path",
                        "The upload staging path is invalid.",
                        500,
                    )
                if not part.exists():
                    response = error(
                        "upload_session_missing",
                        "The incomplete upload staging file is missing.",
                        404,
                    )
                    response.headers["Upload-Error-Code"] = "upload_session_missing"
                    return response
                try:
                    metadata = os.stat(part, follow_symlinks=False)
                except OSError:
                    return error(
                        "upload_session_missing",
                        "The incomplete upload staging file is unavailable.",
                        404,
                    )
                if not stat.S_ISREG(metadata.st_mode):
                    return error(
                        "unsafe_upload_path",
                        "The upload staging path is invalid.",
                        500,
                    )
                offset = metadata.st_size
            elif upload["state"] in ("permanent_error", "retryable_error"):
                response = current_app.response_class(status=422)
                response.headers["Upload-Error-Code"] = upload["error_code"] or (
                    "invalid_media" if upload["state"] == "permanent_error" else "upload_retry_required"
                )
                response.headers["Upload-State"] = upload["state"]
                return response
            else:
                return "", 409
        response = current_app.response_class(status=204)
        response.headers["Upload-Offset"] = str(offset)
        response.headers["Upload-Length"] = str(upload["expected_size"])
        response.headers["Upload-State"] = upload["state"]
        return response

    @blueprint.delete("/api/v1/device-backup/uploads/<upload_id>")
    def abandon_upload(upload_id):
        """Discard only the authenticated device's incomplete staging session."""
        token = None
        device_id = None
        part = None
        retiring = None
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            device, upload = owned_upload(connection, upload_id)
            if not upload:
                # Do not disclose another device's session identity, and make a
                # lost DELETE response safe for the originating phone to replay.
                return "", 204
            if upload["state"] not in ("uploading", "queued", "retryable_error"):
                return error(
                    "upload_not_abandonable",
                    "A completed or permanently rejected upload cannot be abandoned.",
                    409,
                )
            try:
                part = _confined_upload_part(
                    incoming_root,
                    device["id"],
                    upload["id"],
                    upload["part_path"],
                )
            except (OSError, ValueError):
                return error(
                    "unsafe_upload_path", "The upload staging path is invalid.", 500
                )
            retiring = part.with_name(
                f"{part.name}.retiring-abandon-"
                f"{hashlib.sha256(upload['id'].encode('utf-8')).hexdigest()[:16]}"
            )
            try:
                part_metadata = os.lstat(part)
            except FileNotFoundError:
                part_metadata = None
            except OSError:
                return error(
                    "staging_cleanup_failed",
                    "The incomplete upload could not be inspected safely.",
                    500,
                )
            try:
                retiring_metadata = os.lstat(retiring)
            except FileNotFoundError:
                retiring_metadata = None
            except OSError:
                return error(
                    "staging_cleanup_failed",
                    "The incomplete upload could not be inspected safely.",
                    500,
                )
            adopt_crashed_abandon = (
                part_metadata is None
                and retiring_metadata is not None
                and stat.S_ISREG(retiring_metadata.st_mode)
                and not stat.S_ISLNK(retiring_metadata.st_mode)
            )
            device_id = device["id"]
            token = claim_upload_io(
                connection,
                upload,
                "abandon",
                states=("uploading", "queued", "retryable_error"),
                allow_active_abandon_takeover=adopt_crashed_abandon,
            )
            if not token:
                return error(
                    "upload_busy",
                    "This upload is being updated. Try again shortly.",
                    429,
                    2,
                )

        descriptor = -1
        moved = False
        try:
            if retiring.is_symlink():
                raise ValueError("The upload retirement path is unsafe.")
            part_exists = part.exists()
            retiring_exists = retiring.exists()
            if part_exists and retiring_exists:
                raise ValueError("The upload retirement path is ambiguous.")
            if part_exists:
                descriptor, _metadata = _open_locked_upload_part(part, append=False)
            elif retiring_exists:
                metadata = os.stat(retiring, follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("The upload retirement path is unsafe.")

            with db_context() as connection:
                connection.execute("BEGIN IMMEDIATE")
                device, current = owned_upload(connection, upload_id)
                if (
                    not current
                    or not device
                    or device["id"] != device_id
                    or current["state"] not in ("uploading", "queued", "retryable_error")
                    or not hmac.compare_digest(current["io_lease_token"] or "", token)
                    or current["io_lease_kind"] != "abandon"
                ):
                    raise UploadLeaseLost("The upload changed before it was abandoned.")
                current_part = _confined_upload_part(
                    incoming_root,
                    device_id,
                    upload_id,
                    current["part_path"],
                )
                if current_part != part:
                    raise UploadLeaseLost("The upload staging path changed.")
                if descriptor >= 0:
                    matches, _metadata = _descriptor_matches_path(descriptor, part)
                    if not matches or retiring.exists():
                        raise UploadLeaseLost("The upload staging inode changed.")
                    os.replace(part, retiring)
                    moved = True
                elif retiring.exists():
                    metadata = os.stat(retiring, follow_symlinks=False)
                    if retiring.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                        raise ValueError("The upload retirement path is unsafe.")
                elif part.exists():
                    raise UploadLeaseLost("The upload staging inode changed.")
                deleted = connection.execute(
                    """DELETE FROM device_uploads
                       WHERE id=? AND device_id=? AND io_lease_token=?
                         AND io_lease_kind='abandon'
                         AND state IN ('uploading','queued','retryable_error')""",
                    (upload_id, device_id, token),
                )
                if deleted.rowcount != 1:
                    raise UploadLeaseLost("The upload changed before deletion.")
        except UploadPartBusy:
            release_upload_io(upload_id, device_id, token, "abandon")
            return error(
                "upload_busy",
                "This upload is being updated. Try again shortly.",
                429,
                2,
            )
        except (OSError, ValueError, UploadLeaseLost):
            if moved:
                try:
                    if retiring.exists() and not part.exists():
                        os.replace(retiring, part)
                except OSError:
                    current_app.logger.exception(
                        "Could not restore an uncommitted abandoned upload [%s]",
                        upload_id,
                    )
            release_upload_io(upload_id, device_id, token, "abandon")
            current_app.logger.exception(
                "Could not retire incomplete upload safely [%s]", upload_id
            )
            return error(
                "staging_cleanup_failed",
                "The incomplete upload could not be discarded safely.",
                500,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        try:
            retiring.unlink(missing_ok=True)
        except OSError:
            # The row is already durably absent. Dedicated confined orphan
            # housekeeping will retry removal without affecting saved media.
            current_app.logger.warning(
                "Deferred abandoned upload staging cleanup [%s]", upload_id
            )
        return "", 204

    @blueprint.patch("/api/v1/device-backup/uploads/<upload_id>")
    def append_upload(upload_id):
        try:
            supplied_offset = int(request.headers.get("Upload-Offset", "-1"))
        except ValueError:
            return error("offset_required", "Upload-Offset is required.", 400)
        content_length = request.content_length
        if content_length is None or content_length < 1 or content_length > MAX_CHUNK_BYTES:
            return error("chunk_size_invalid", "Use a non-empty chunk no larger than 8 MiB.", 413)
        token = None
        device_id = None
        upload_data = None
        part = None
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            device, upload = owned_upload(connection, upload_id)
            if not upload or upload["state"] != "uploading":
                return error("upload_not_found", "This upload is not active.", 404)
            token = claim_upload_io(connection, upload, "append")
            if not token:
                return error(
                    "upload_busy",
                    "This upload is being updated. Try again shortly.",
                    429,
                    2,
                )
            device_id = device["id"]
            upload_data = dict(upload)

        try:
            part = _confined_upload_part(
                incoming_root,
                device_id,
                upload_id,
                upload_data["part_path"],
            )
        except (OSError, ValueError):
            release_upload_io(upload_id, device_id, token, "append")
            return error(
                "unsafe_upload_path", "The upload staging path is invalid.", 500
            )

        descriptor = -1
        initial_metadata = None
        current_offset = None
        written = 0
        failure = None
        lease_lost = False
        next_renewal = time.monotonic() + max(
            10.0, max(30, UPLOAD_IO_LEASE_SECONDS) / 3
        )

        def renew_if_due():
            nonlocal next_renewal
            if time.monotonic() < next_renewal:
                return
            if not renew_upload_io(upload_id, device_id, token, "append"):
                raise UploadLeaseLost("The append lease changed during transfer.")
            next_renewal = time.monotonic() + max(
                10.0, max(30, UPLOAD_IO_LEASE_SECONDS) / 3
            )

        try:
            descriptor, initial_metadata = _open_locked_upload_part(part, append=True)
            matches, initial_metadata = _descriptor_matches_path(
                descriptor, part, initial_metadata
            )
            if not matches:
                raise OSError("The upload staging inode changed.")
            current_offset = initial_metadata.st_size
            if supplied_offset != current_offset:
                failure = "offset_mismatch"
            elif current_offset + content_length > upload_data["expected_size"]:
                failure = "upload_too_long"
            else:
                written = _append_request_to_descriptor(
                    descriptor,
                    request.stream,
                    content_length,
                    renew_if_due,
                )
                if written != content_length:
                    failure = "chunk_incomplete"
        except FileNotFoundError:
            failure = "upload_session_missing"
        except UploadPartBusy:
            failure = "upload_busy"
        except UploadLeaseLost:
            lease_lost = True
            failure = "upload_changed"
        except OSError:
            current_app.logger.exception("Device upload append failed [%s]", upload_id)
            failure = "staging_write_failed"
        except Exception:
            current_app.logger.exception(
                "Device upload request stream failed [%s]", upload_id
            )
            failure = "staging_write_failed"

        new_offset = current_offset
        checkpointed = False
        try:
            if descriptor >= 0:
                matches, final_metadata = _descriptor_matches_path(
                    descriptor, part, initial_metadata
                )
                if matches:
                    new_offset = final_metadata.st_size
                with db_context() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT * FROM device_uploads WHERE id=? AND device_id=?",
                        (upload_id, device_id),
                    ).fetchone()
                    row_matches = (
                        current
                        and current["state"] == "uploading"
                        and hmac.compare_digest(
                            current["io_lease_token"] or "", token
                        )
                        and current["io_lease_kind"] == "append"
                        and current["part_path"] == upload_data["part_path"]
                        and current["expected_size"] == upload_data["expected_size"]
                        and hmac.compare_digest(
                            current["expected_sha256"],
                            upload_data["expected_sha256"],
                        )
                    )
                    if row_matches and matches and new_offset <= current["expected_size"]:
                        changed = connection.execute(
                            """UPDATE device_uploads
                               SET accepted_offset=?,updated_at=?,io_lease_token=NULL,
                                   io_lease_kind=NULL,io_lease_expires_at=NULL
                               WHERE id=? AND device_id=? AND io_lease_token=?
                                 AND io_lease_kind='append' AND state='uploading'""",
                            (new_offset, utcnow(), upload_id, device_id, token),
                        )
                        checkpointed = changed.rowcount == 1
                    elif row_matches:
                        connection.execute(
                            """UPDATE device_uploads
                               SET io_lease_token=NULL,io_lease_kind=NULL,
                                   io_lease_expires_at=NULL,updated_at=?
                               WHERE id=? AND device_id=? AND io_lease_token=?
                                 AND io_lease_kind='append'""",
                            (utcnow(), upload_id, device_id, token),
                        )
            else:
                release_upload_io(upload_id, device_id, token, "append")
        except (OSError, sqlite3.Error, TypeError):
            lease_lost = True
            failure = "staging_write_failed"
            current_app.logger.exception(
                "Device upload checkpoint failed [%s]", upload_id
            )
            release_upload_io(upload_id, device_id, token, "append")
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not checkpointed and descriptor >= 0 and not lease_lost:
            return error(
                "upload_changed",
                "The upload changed while this chunk was being stored.",
                409,
            )
        if failure == "offset_mismatch":
            response = error("offset_mismatch", "Resume from the server offset.", 409)
            response.headers["Upload-Offset"] = str(new_offset)
            return response
        if failure == "upload_too_long":
            return error("upload_too_long", "This chunk exceeds the expected file size.", 409)
        if failure == "chunk_incomplete":
            return error("chunk_incomplete", "The full chunk was not received.", 400)
        if failure == "upload_session_missing":
            return error(
                "upload_session_missing",
                "The incomplete upload staging file is missing.",
                404,
            )
        if failure == "upload_busy":
            return error(
                "upload_busy",
                "This upload is being updated. Try again shortly.",
                429,
                2,
            )
        if failure == "upload_changed":
            return error(
                "upload_changed",
                "The upload changed while this chunk was being stored.",
                409,
            )
        if failure == "staging_write_failed":
            return error(
                "staging_write_failed",
                "The upload chunk could not be stored safely.",
                500,
            )
        response = current_app.response_class(status=204)
        response.headers["Upload-Offset"] = str(new_offset)
        return response

    @blueprint.post("/api/v1/device-backup/uploads/<upload_id>/complete")
    def complete_upload(upload_id):
        upload_data = None
        device_data = None
        ingest_intent = None
        actual_hash = None
        verification_token = None
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            device, upload = owned_upload(connection, upload_id)
            if not upload:
                return error("upload_not_found", "This upload is not active.", 404)
            if upload["state"] == "primary_verified":
                record = connection.execute(
                    """SELECT media_id,primary_verification_state,secondary_verification_state
                       FROM device_media_records
                       WHERE device_id=? AND client_item_id=? AND media_id=?
                       LIMIT 1""",
                    (device["id"], upload["client_item_id"], upload["media_id"]),
                ).fetchone()
                if not record:
                    return error(
                        "completed_upload_inconsistent",
                        "This completed upload needs administrator attention.",
                        409,
                    )
                return jsonify(
                    state="primary_verified",
                    media_id=record["media_id"],
                    primary_verification_state=record["primary_verification_state"],
                    secondary_verification_state=record["secondary_verification_state"],
                )
            if upload["state"] == "permanent_error":
                return error(
                    upload["error_code"] or "invalid_media",
                    "Android returned an unreadable media item. It was skipped safely.",
                    422,
                )
            if upload["state"] == "ingesting":
                ingest_intent = connection.execute(
                    "SELECT * FROM device_ingest_intents WHERE id=? AND upload_id=?",
                    (upload["media_id"], upload["id"]),
                ).fetchone()
                if not ingest_intent:
                    return error(
                        "ingesting_upload_inconsistent",
                        "This upload needs administrator attention.",
                        409,
                    )
                upload_data, device_data = dict(upload), dict(device)
                actual_hash = upload["expected_sha256"]
            elif upload["state"] != "uploading":
                return error("upload_not_found", "This upload is not active.", 404)
            else:
                verification_token = claim_upload_io(connection, upload, "complete")
                if not verification_token:
                    return error(
                        "upload_busy",
                        "This upload is being updated. Try again shortly.",
                        429,
                        2,
                    )
                upload_data, device_data = dict(upload), dict(device)

        if verification_token is not None:
            device_id = device_data["id"]
            if not primary_storage_ready(data_root):
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                return error(
                    "primary_storage_unavailable",
                    "Primary storage is unavailable.",
                    503,
                    60,
                )
            try:
                part = _confined_upload_part(
                    incoming_root,
                    device_id,
                    upload_id,
                    upload_data["part_path"],
                )
            except (OSError, ValueError):
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                return error(
                    "unsafe_upload_path", "The upload staging path is invalid.", 500
                )

            descriptor = -1
            next_renewal = time.monotonic() + max(
                10.0, max(30, UPLOAD_IO_LEASE_SECONDS) / 3
            )

            def renew_if_due():
                nonlocal next_renewal
                if time.monotonic() < next_renewal:
                    return
                if not renew_upload_io(
                    upload_id, device_id, verification_token, "complete"
                ):
                    raise UploadLeaseLost(
                        "The completion lease changed during verification."
                    )
                next_renewal = time.monotonic() + max(
                    10.0, max(30, UPLOAD_IO_LEASE_SECONDS) / 3
                )

            try:
                descriptor, initial_metadata = _open_locked_upload_part(
                    part, append=False
                )
                matches, initial_metadata = _descriptor_matches_path(
                    descriptor, part, initial_metadata
                )
                if not matches:
                    raise OSError("The upload staging inode changed.")
                if initial_metadata.st_size != upload_data["expected_size"]:
                    release_upload_io(
                        upload_id, device_id, verification_token, "complete"
                    )
                    return error(
                        "size_mismatch", "The uploaded size does not match.", 409
                    )
                actual_hash = _sha256_upload_descriptor(descriptor, renew_if_due)
                matches, final_metadata = _descriptor_matches_path(
                    descriptor, part, initial_metadata
                )
                if (
                    not matches
                    or final_metadata.st_size != upload_data["expected_size"]
                ):
                    raise UploadLeaseLost(
                        "The upload staging file changed during verification."
                    )

                with db_context() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        "SELECT * FROM device_uploads WHERE id=? AND device_id=?",
                        (upload_id, device_id),
                    ).fetchone()
                    row_matches = (
                        current
                        and current["state"] == "uploading"
                        and hmac.compare_digest(
                            current["io_lease_token"] or "", verification_token
                        )
                        and current["io_lease_kind"] == "complete"
                        and current["part_path"] == upload_data["part_path"]
                        and current["expected_size"] == upload_data["expected_size"]
                        and current["client_item_id"] == upload_data["client_item_id"]
                        and current["original_filename"]
                        == upload_data["original_filename"]
                        and current["mime_type"] == upload_data["mime_type"]
                        and current["capture_timestamp"]
                        == upload_data["capture_timestamp"]
                        and hmac.compare_digest(
                            current["expected_sha256"],
                            upload_data["expected_sha256"],
                        )
                    )
                    if not row_matches:
                        raise UploadLeaseLost(
                            "The upload changed before verification committed."
                        )
                    if not hmac.compare_digest(
                        actual_hash, current["expected_sha256"]
                    ):
                        changed = connection.execute(
                            """UPDATE device_uploads
                               SET state='retryable_error',error_code=?,updated_at=?,
                                   io_lease_token=NULL,io_lease_kind=NULL,
                                   io_lease_expires_at=NULL
                               WHERE id=? AND device_id=? AND state='uploading'
                                 AND io_lease_token=? AND io_lease_kind='complete'""",
                            (
                                "sha256_mismatch",
                                utcnow(),
                                upload_id,
                                device_id,
                                verification_token,
                            ),
                        )
                        if changed.rowcount != 1:
                            raise UploadLeaseLost(
                                "The upload changed before hash failure committed."
                            )
                    else:
                        now = utcnow()
                        ingest_intent = prepare_device_ingest_intent(
                            connection,
                            device_id=device_id,
                            client_item_id=current["client_item_id"],
                            upload_id=current["id"],
                            original_filename=current["original_filename"],
                            content_sha256=actual_hash,
                            byte_size=current["expected_size"],
                            mime_type=current["mime_type"],
                            capture_timestamp=current["capture_timestamp"],
                            ingestion_source="android_backup",
                            now=now,
                        )
                        updated = connection.execute(
                            """UPDATE device_uploads
                               SET state='ingesting',media_id=?,updated_at=?,error_code=NULL,
                                   io_lease_token=NULL,io_lease_kind=NULL,
                                   io_lease_expires_at=NULL
                               WHERE id=? AND device_id=? AND state='uploading'
                                 AND io_lease_token=? AND io_lease_kind='complete'""",
                            (
                                ingest_intent["id"],
                                now,
                                current["id"],
                                device_id,
                                verification_token,
                            ),
                        )
                        if updated.rowcount != 1:
                            raise UploadLeaseLost(
                                "The upload changed before ingestion could start."
                            )
            except FileNotFoundError:
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                return error(
                    "upload_session_missing",
                    "The incomplete upload staging file is missing.",
                    404,
                )
            except UploadPartBusy:
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                return error(
                    "upload_busy",
                    "This upload is being updated. Try again shortly.",
                    429,
                    2,
                )
            except UploadLeaseLost:
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                return error(
                    "upload_changed",
                    "The upload changed during verification. Try again.",
                    409,
                )
            except OSError:
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                current_app.logger.exception(
                    "Device upload verification failed [%s]", upload_id
                )
                return error(
                    "staging_read_failed",
                    "The uploaded file could not be verified safely.",
                    500,
                )
            except Exception:
                release_upload_io(
                    upload_id, device_id, verification_token, "complete"
                )
                current_app.logger.exception(
                    "Device upload verification transaction failed [%s]", upload_id
                )
                return error(
                    "verification_failed",
                    "The uploaded file could not be verified safely.",
                    500,
                )
            finally:
                if descriptor >= 0:
                    os.close(descriptor)

            if not hmac.compare_digest(
                actual_hash, upload_data["expected_sha256"]
            ):
                return error(
                    "sha256_mismatch", "The received file did not verify.", 422
                )
        try:
            result = canonical_ingest(
                staged_path=Path(upload_data["part_path"]),
                original_filename=upload_data["original_filename"],
                mime_type=upload_data["mime_type"],
                owner_user_id=device_data["owner_user_id"],
                owner_name=device_data["owner_name"],
                visibility="shared",
                capture_timestamp=upload_data["capture_timestamp"],
                source_device_id=device_data["id"],
                ingestion_source="android_backup",
                authoritative_sha256=actual_hash,
                authoritative_size=upload_data["expected_size"],
                media_id=ingest_intent["id"],
                precommit_validator=publication_precommit_validator(
                    ingest_intent["id"]
                ),
                publication_callback=publication_callback(ingest_intent["id"]),
            )
        except (UnidentifiedImageError, ValueError) as exc:
            permanent_conversion = isinstance(exc, UnidentifiedImageError) or (
                "conversion failed" in str(exc).casefold()
                or "invalid data found when processing input" in str(exc).casefold()
            )
            if not permanent_conversion:
                current_app.logger.exception(
                    "Device backup validation failed [%s]", upload_id
                )
                return error(
                    "ingestion_failed",
                    "The file verified but could not be added safely.",
                    500,
                )
            # A MediaStore row can occasionally return placeholder bytes rather
            # than an actual image. This is a permanent item-level failure: keep
            # no staged copy and let the phone continue with the next item.
            Path(upload_data["part_path"]).unlink(missing_ok=True)
            with db_context() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """UPDATE device_uploads
                       SET state='permanent_error',error_code=?,updated_at=?,part_path=NULL
                       WHERE id=?""",
                    ("invalid_media", utcnow(), upload_id),
                )
                connection.execute(
                    """DELETE FROM device_ingest_intents
                       WHERE id=? AND state='prepared'
                         AND NOT EXISTS (
                             SELECT 1 FROM media_publish_intents WHERE id=?
                         )""",
                    (ingest_intent["id"], ingest_intent["id"]),
                )
            return error(
                "invalid_media",
                "Android returned an unreadable media item. It was skipped safely.",
                422,
            )
        except Exception:
            current_app.logger.exception("Device backup ingestion failed [%s]", upload_id)
            return error("ingestion_failed", "The file verified but could not be added safely.", 500)
        return jsonify(
            state="primary_verified", media_id=result["id"],
            primary_verification_state="primary_verified",
            secondary_verification_state="secondary_pending",
        )

    @blueprint.get("/api/v1/device-backup/status")
    def device_status():
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device:
                return error("device_unauthorized", "This phone is not paired.", 401)
            counts = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state,COUNT(*) count FROM device_uploads WHERE device_id=? GROUP BY state",
                    (device["id"],),
                )
            }
            protection = {
                row["secondary_verification_state"]: row["count"]
                for row in connection.execute(
                    """SELECT secondary_verification_state,COUNT(*) count
                       FROM device_media_records WHERE device_id=?
                       GROUP BY secondary_verification_state""",
                    (device["id"],),
                )
            }
        return jsonify(
            device={
                "id": device["id"], "display_name": device["display_name"],
                "owner_name": device["owner_name"], "last_contact_at": device["last_contact_at"],
                "last_reconciliation_at": device["last_reconciliation_at"],
            },
            installation_id=(get_installation() or {}).get("instance_id", ""),
            enabled_modules=[item["id"] for item in module_catalog() if item["mode"] != "disabled"],
            member_id=device["owner_user_id"],
            display_name=installation_display_name(), server_url=public_url(),
            uploads=counts, protection=protection,
            stalled_ingests=counts.get("ingesting", 0),
            fully_protected=protection.get("fully_protected", 0),
            secondary_configured=bool(SECONDARY_ROOT),
            incremental_cursor=json.loads(device["settings_json"] or "{}").get(
                "ios_capture_cursor", ""
            ),
            capture_after=(
                json.loads(device["settings_json"] or "{}").get("ios_capture_cursor")
                or "1970-01-01T00:00:00+00:00"
            ),
            recommended_batch_size=200,

        )

    def receive_ios_item(
        device_data, stream, original_name, mime_type, capture_timestamp, client_hint
    ):
        """Verify, deduplicate, ingest, and checkpoint one iPhone media item."""
        if not primary_storage_ready(data_root):
            return error("primary_storage_unavailable", "Primary storage is unavailable.", 503, 60)
        if shutil.disk_usage(data_root).free < MIN_FREE_BYTES:
            return error("storage_low", "David-Pi needs more free storage.", 507)

        original_name = safe_filename(original_name)
        capture_timestamp = normalize_capture_timestamp(capture_timestamp)
        client_hint = str(client_hint or "")[:200]
        device_root = incoming_root / device_data["id"]
        ensure_restricted_directory(device_root)
        part = device_root / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        byte_size = 0
        try:
            with part.open("xb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > IOS_MAX_FILE_BYTES:
                        raise ValueError("file_too_large")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if byte_size == 0:
                raise ValueError("empty_file")
            content_hash = digest.hexdigest()
            client_item_id = client_hint or f"ios-sha256:{content_hash}"
            with db_context() as connection:
                existing = connection.execute(
                    """SELECT media_id FROM device_media_records
                       WHERE device_id=? AND client_item_id=?""",
                    (device_data["id"], client_item_id),
                ).fetchone()
            if existing:
                part.unlink(missing_ok=True)
                now = utcnow()
                with db_context() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    cursor = advance_ios_cursor(
                        connection, device_data["id"], capture_timestamp, now
                    )
                return jsonify(
                    state="duplicate", media_id=existing["media_id"],
                    capture_cursor=cursor,
                )

            now = utcnow()
            with db_context() as connection:
                connection.execute("BEGIN IMMEDIATE")
                prior_intent = connection.execute(
                    """SELECT id FROM device_ingest_intents
                       WHERE device_id=? AND client_item_id=?""",
                    (device_data["id"], client_item_id),
                ).fetchone()
                ingest_intent = prepare_device_ingest_intent(
                    connection,
                    device_id=device_data["id"],
                    client_item_id=client_item_id,
                    upload_id=None,
                    original_filename=original_name,
                    content_sha256=content_hash,
                    byte_size=byte_size,
                    mime_type=mime_type or "application/octet-stream",
                    capture_timestamp=capture_timestamp or None,
                    ingestion_source="ios_shortcut_backup",
                    now=now,
                )
            result = canonical_ingest(
                staged_path=part,
                original_filename=original_name,
                mime_type=mime_type or "application/octet-stream",
                owner_user_id=device_data["owner_user_id"],
                owner_name=device_data["owner_name"],
                source_device_id=device_data["id"],
                ingestion_source="ios_shortcut_backup",
                capture_timestamp=capture_timestamp or None,
                authoritative_sha256=content_hash,
                authoritative_size=byte_size,
                media_id=ingest_intent["id"],
                publication_callback=publication_callback(ingest_intent["id"]),
            )
            if result.get("recovered_intent"):
                part.unlink(missing_ok=True)
            with db_context() as connection:
                row = connection.execute(
                    "SELECT settings_json FROM backup_devices WHERE id=?",
                    (device_data["id"],),
                ).fetchone()
                cursor = normalize_capture_timestamp(
                    json.loads((row["settings_json"] if row else "{}") or "{}").get(
                        "ios_capture_cursor", ""
                    )
                )
            return jsonify(
                state="primary_verified", media_id=result["id"], byte_size=byte_size,
                content_sha256=content_hash, capture_cursor=cursor,
            ), 201
        except ValueError as exc:
            part.unlink(missing_ok=True)
            if "ingest_intent" in locals() and not prior_intent:
                with db_context() as connection:
                    connection.execute(
                        """DELETE FROM device_ingest_intents
                           WHERE id=? AND state='prepared'
                             AND NOT EXISTS (
                                 SELECT 1 FROM media_publish_intents WHERE id=?
                             )""",
                        (ingest_intent["id"], ingest_intent["id"]),
                    )
            if str(exc) == "file_too_large":
                return error("file_too_large", "That item exceeds the iPhone backup limit.", 413)
            return error("invalid_media", "That item could not be backed up.", 422)
        except (OSError, UnidentifiedImageError):
            part.unlink(missing_ok=True)
            current_app.logger.exception("iPhone backup upload failed")
            return error("upload_failed", "That item could not be backed up safely.", 500)
        except Exception as exc:
            if not getattr(exc, "preserve_staged_media", False):
                part.unlink(missing_ok=True)
            current_app.logger.exception("iPhone backup ingestion failed")
            return error("ingestion_failed", "David-Pi could not finish that backup item.", 500)

    def ios_upload():
        """Receive one multipart Shortcut item through the canonical library."""
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device or device["platform"] != "ios_shortcut":
                return error("device_unauthorized", "This iPhone is not paired.", 401)
            device_data = dict(device)
        forbidden = {
            "owner_user_id", "owner_id", "owner", "username", "visibility",
            "destination_path", "access_control", "collection_id", "album_id",
        }
        if forbidden.intersection(request.form):
            return error(
                "server_controls_ownership",
                "David-Pi assigns ownership and access from the paired phone.", 422,
            )
        upload = request.files.get("file")
        if not upload or not upload.filename:
            return error("file_required", "Choose one photo or video.", 400)
        return receive_ios_item(
            device_data, upload.stream, upload.filename,
            upload.mimetype or "application/octet-stream",
            request.form.get("capture_timestamp"), request.form.get("client_item_id"),
        )

    def ios_upload_file():
        """Receive the raw File request body produced by Apple Shortcuts."""
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device or device["platform"] != "ios_shortcut":
                return error("device_unauthorized", "This iPhone is not paired.", 401)
            device_data = dict(device)
        forbidden_headers = {
            "x-david-pi-owner", "x-david-pi-owner-id", "x-david-pi-visibility",
            "x-david-pi-destination", "x-david-pi-collection",
        }
        if forbidden_headers.intersection(name.lower() for name in request.headers.keys()):
            return error(
                "server_controls_ownership",
                "David-Pi assigns ownership and access from the paired phone.", 422,
            )
        if request.content_length == 0:
            return error("file_required", "Choose one photo or video.", 400)
        filename = request.headers.get("X-David-Pi-Filename", "iphone-media")
        captured_at = request.headers.get("X-David-Pi-Captured-At", "")
        client_item_id = request.headers.get("X-David-Pi-Item-Id", "")
        return receive_ios_item(
            device_data, request.stream, filename, request.mimetype,
            captured_at, client_item_id,
        )

    def ios_checkpoint():
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device or device["platform"] != "ios_shortcut":
                return error("device_unauthorized", "This iPhone is not paired.", 401)
            payload = request.get_json(silent=True) or {}
            cursor = normalize_capture_timestamp(payload.get("capture_cursor"))
            if not cursor:
                return error("cursor_required", "A valid completed backup cursor is required.", 400)
            settings = json.loads(device["settings_json"] or "{}")
            previous = normalize_capture_timestamp(settings.get("ios_capture_cursor"))
            if previous and cursor < previous:
                return error("cursor_regression", "The backup checkpoint cannot move backward.", 409)
            now = utcnow()
            settings["ios_capture_cursor"] = cursor
            settings["ios_checkpoint_at"] = now
            connection.execute(
                "UPDATE backup_devices SET settings_json=?,last_contact_at=? WHERE id=?",
                (json.dumps(settings, separators=(",", ":")), now, device["id"]),
            )
        return jsonify(ok=True, capture_cursor=cursor)

    @blueprint.get("/api/v1/device-backup/manifest")
    def device_manifest():
        since = request.args.get("since", "")
        limit = min(max(request.args.get("limit", 500, type=int), 1), 1000)
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device:
                return error("device_unauthorized", "This phone is not paired.", 401)
            rows = connection.execute(
                """SELECT d.client_item_id,d.media_id,d.content_sha256,d.byte_size,
                          d.capture_timestamp,
                          d.primary_verification_state,d.secondary_verification_state,d.ingested_at,
                          d.local_source_visible,p.stored_path AS canonical_relative_path,
                          p.content_type AS mime_type,p.original_name AS original_filename
                   FROM device_media_records d JOIN photos p ON p.id=d.media_id
                   WHERE d.device_id=? AND d.ingested_at>? ORDER BY d.ingested_at LIMIT ?""",
                (device["id"], since, limit),
            ).fetchall()
        return jsonify(items=[dict(row) for row in rows])

    @blueprint.post("/api/v1/device-backup/reconcile")
    def reconcile():
        # Authenticate without side effects before reading or decoding this
        # route's potentially large JSON body. The accepted path reauthenticates
        # under its serialized transaction so revocation cannot race the write.
        with db_context() as connection:
            authenticated = authenticate_device(connection, touch_last_contact=False)
            if not authenticated:
                return error("device_unauthorized", "This phone is not paired.", 401)
            if authenticated["platform"] != "android":
                return error(
                    "android_complete_scan_required",
                    "Only a fully authorized Android MediaStore scan can reconcile.",
                    403,
                )
            authenticated_device_id = authenticated["id"]

        if (
            request.content_length is not None
            and request.content_length > RECONCILIATION_MAX_BODY_BYTES
        ):
            return error(
                "scan_body_too_large",
                "The complete scan request exceeds the bounded receipt size.",
                413,
            )
        if not request.is_json:
            return error("invalid_scan", "A JSON complete scan receipt is required.", 415)
        raw_payload = request.stream.read(RECONCILIATION_MAX_BODY_BYTES + 1)
        if len(raw_payload) > RECONCILIATION_MAX_BODY_BYTES:
            return error(
                "scan_body_too_large",
                "The complete scan request exceeds the bounded receipt size.",
                413,
            )
        try:
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error("invalid_scan", "A valid JSON complete scan receipt is required.", 422)
        if not isinstance(payload, dict) or set(payload) != RECONCILIATION_FIELDS:
            return error(
                "complete_scan_required",
                "Submit exactly one complete Android media scan receipt.",
                422,
            )
        if payload.get("complete") is not True:
            return error(
                "complete_scan_required",
                "Partial, selected, or cancelled scans cannot be reconciled.",
                422,
            )
        try:
            scan_id = canonical_uuid7(payload.get("scan_id"))
        except ValueError as validation_error:
            return error("invalid_scan", str(validation_error), 422)
        server_time_ms = int(time.time() * 1000)
        if uuid7_timestamp_ms(scan_id) > (
            server_time_ms + RECONCILIATION_MAX_FUTURE_SKEW_MS
        ):
            # This is deliberately distinguishable from a malformed receipt.
            # The authenticated Android client can retire only this rejected ID
            # and anchor its replacement to server time; no reconciliation
            # transaction or canonical-media mutation has started yet.
            return error(
                "scan_clock_skew",
                "The phone clock is too far ahead of David-Pi.",
                422,
                details={
                    "scan_id": scan_id,
                    "server_time_ms": server_time_ms,
                    "max_future_skew_ms": RECONCILIATION_MAX_FUTURE_SKEW_MS,
                },
            )
        try:
            visible_ids = canonical_reconciliation_ids(
                payload.get("visible_client_item_ids")
            )
        except OverflowError:
            return error(
                "scan_too_large",
                f"A complete scan supports at most {RECONCILIATION_MAX_ITEMS} items.",
                413,
            )
        except ValueError as validation_error:
            return error("invalid_scan", str(validation_error), 422)
        item_count = payload.get("item_count")
        supplied_digest = payload.get("ids_sha256")
        expected_digest = reconciliation_ids_sha256(visible_ids)
        if (
            isinstance(item_count, bool)
            or not isinstance(item_count, int)
            or item_count != len(visible_ids)
            or not isinstance(supplied_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", supplied_digest)
            or not hmac.compare_digest(supplied_digest, expected_digest)
        ):
            return error(
                "scan_integrity_mismatch",
                "The complete scan count or digest does not match its canonical IDs.",
                422,
            )

        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Rejected receipts do not update last_contact_at. Successful
            # reconciliation records both timestamps in this transaction.
            device = authenticate_device(connection, touch_last_contact=False)
            if not device or device["id"] != authenticated_device_id:
                return error("device_unauthorized", "This phone is not paired.", 401)
            if device["platform"] != "android":
                return error(
                    "android_complete_scan_required",
                    "Only a fully authorized Android MediaStore scan can reconcile.",
                    403,
                )

            existing = connection.execute(
                """SELECT item_count,ids_sha256,accepted_at,known_visible_count,changed_count
                   FROM device_reconciliation_receipts
                   WHERE device_id=? AND scan_id=?""",
                (device["id"], scan_id),
            ).fetchone()
            if existing:
                if (
                    int(existing["item_count"]) != item_count
                    or not hmac.compare_digest(existing["ids_sha256"], supplied_digest)
                ):
                    return error(
                        "scan_replay_conflict",
                        "That scan receipt ID was already used for different content.",
                        409,
                    )
                return jsonify(
                    ok=True,
                    state="replayed",
                    scan_id=scan_id,
                    reconciled_at=existing["accepted_at"],
                    item_count=item_count,
                    known_visible_count=int(existing["known_visible_count"]),
                    changed_count=int(existing["changed_count"]),
                )

            latest = connection.execute(
                """SELECT scan_id,accepted_at FROM device_reconciliation_receipts
                   WHERE device_id=? ORDER BY scan_id DESC LIMIT 1""",
                (device["id"],),
            ).fetchone()
            if latest and scan_id < latest["scan_id"]:
                return error(
                    "stale_scan",
                    "A newer complete scan was already accepted for this device.",
                    409,
                )
            now = utcnow()
            if latest and RECONCILIATION_MIN_INTERVAL_SECONDS > 0:
                try:
                    elapsed = (
                        datetime.fromisoformat(now)
                        - datetime.fromisoformat(latest["accepted_at"])
                    ).total_seconds()
                except (TypeError, ValueError):
                    elapsed = 0
                if elapsed < RECONCILIATION_MIN_INTERVAL_SECONDS:
                    retry_after = max(
                        1, int(RECONCILIATION_MIN_INTERVAL_SECONDS - elapsed + 0.999)
                    )
                    return error(
                        "reconciliation_throttled",
                        "A recent complete scan was already accepted for this device.",
                        429,
                        retry_after,
                    )

            # A temporary table avoids SQLite parameter limits and preserves
            # every ID in large complete scans. The only durable observation
            # changed here is this authenticated device's provenance row.
            connection.execute("DROP TABLE IF EXISTS temp.reconciliation_visible_ids")
            connection.execute(
                "CREATE TEMP TABLE reconciliation_visible_ids (client_item_id TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO reconciliation_visible_ids(client_item_id) VALUES (?)",
                ((item_id,) for item_id in visible_ids),
            )
            changed_count = connection.execute(
                """SELECT COUNT(*) FROM device_media_records AS record
                   WHERE record.device_id=? AND record.owner_user_id=?
                       AND record.local_source_visible !=
                       CASE WHEN EXISTS (
                           SELECT 1 FROM reconciliation_visible_ids AS visible
                           WHERE visible.client_item_id=record.client_item_id
                       ) THEN 1 ELSE 0 END""",
                (device["id"], device["owner_user_id"]),
            ).fetchone()[0]
            connection.execute(
                """UPDATE device_media_records AS record
                   SET local_source_visible=CASE WHEN EXISTS (
                       SELECT 1 FROM reconciliation_visible_ids AS visible
                       WHERE visible.client_item_id=record.client_item_id
                   ) THEN 1 ELSE 0 END
                   WHERE record.device_id=? AND record.owner_user_id=?""",
                (device["id"], device["owner_user_id"]),
            )
            known_visible_count = connection.execute(
                """SELECT COUNT(*) FROM device_media_records
                   WHERE device_id=? AND owner_user_id=? AND local_source_visible=1""",
                (device["id"], device["owner_user_id"]),
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO device_reconciliation_receipts
                   (device_id,scan_id,item_count,ids_sha256,accepted_at,
                    known_visible_count,changed_count)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    device["id"], scan_id, item_count, supplied_digest, now,
                    known_visible_count, changed_count,
                ),
            )
            connection.execute(
                "UPDATE backup_devices SET last_reconciliation_at=?,last_contact_at=? WHERE id=?",
                (now, now, device["id"]),
            )
            connection.execute(
                """INSERT INTO device_backup_events
                   (device_id,level,event_code,message,created_at)
                   VALUES (?,'info','reconciliation_accepted',
                           'Complete device visibility scan accepted.',?)""",
                (device["id"], now),
            )
            try:
                retired_upload_ids, staged_parts = stage_absent_incomplete_uploads(
                    connection,
                    incoming_root,
                    device["id"],
                    scan_id,
                )
            except UploadRetirementError as cleanup_error:
                connection.rollback()
                return error(
                    "upload_cleanup_failed",
                    str(cleanup_error),
                    500,
                )
            if retired_upload_ids:
                connection.executemany(
                    """DELETE FROM device_uploads
                       WHERE id=? AND device_id=?
                         AND state IN ('uploading','retryable_error')
                         AND io_lease_token IS NULL""",
                    ((upload_id, device["id"]) for upload_id in retired_upload_ids),
                )
                try:
                    # Commit the accepted receipt and same-device session
                    # retirement before erasing staged names. If commit fails,
                    # restore the old paths so the DB rollback remains usable.
                    connection.commit()
                except BaseException:
                    try:
                        restore_staged_uploads(staged_parts)
                    except OSError:
                        current_app.logger.exception(
                            "Could not restore staged upload after reconciliation rollback"
                        )
                    raise
                for _, retiring in staged_parts:
                    try:
                        retiring.unlink(missing_ok=True)
                    except OSError:
                        current_app.logger.warning(
                            "A retired upload staging file awaits housekeeping cleanup"
                        )
        return jsonify(
            ok=True,
            state="accepted",
            scan_id=scan_id,
            reconciled_at=now,
            item_count=item_count,
            known_visible_count=known_visible_count,
            changed_count=changed_count,
        )

    @blueprint.post("/api/device-backup/devices/<device_id>/revoke")
    def revoke(device_id):
        actor = require_portal_identity()
        if not actor:
            return error("tailscale_identity_required", "Open through private Tailscale HTTPS.", 403)
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """UPDATE backup_devices SET revoked_at=?
                   WHERE id=? AND owner_user_id=? AND revoked_at IS NULL""",
                (utcnow(), device_id, actor["owner_id"]),
            )
            if changed.rowcount != 1:
                return error("device_not_found", "That paired phone is no longer active.", 404)
            # Empty/abandoned pairings have no provenance to preserve and can be
            # removed completely.  A device that contributed media remains as a
            # hidden, revoked tombstone so source-device metadata and audit links
            # are not destroyed.  In both cases its credential stops working now.
            media_count = connection.execute(
                "SELECT COUNT(*) FROM device_media_records WHERE device_id=?",
                (device_id,),
            ).fetchone()[0]
            deleted = False
            if media_count == 0:
                connection.execute("DELETE FROM backup_devices WHERE id=?", (device_id,))
                deleted = True
        return jsonify(ok=True, removed=True, deleted=deleted)

    @blueprint.get("/device-backup/apk")
    def apk_download():
        actor = require_portal_identity()
        if not actor:
            return "Not found", 404
        android_release = current_android_release()
        if not android_release:
            return "Not found", 404
        apk = app_root / android_release["artifact"]["path"]
        try:
            payload = apk.read_bytes()
        except OSError:
            return "Not found", 404
        if not hmac.compare_digest(
            hashlib.sha256(payload).hexdigest(),
            android_release["artifact"]["sha256"],
        ):
            app.logger.error("Android release APK changed during download")
            return "Not found", 404
        response = send_file(
            io.BytesIO(payload), as_attachment=True, download_name="David-Pi.apk",
            mimetype="application/vnd.android.package-archive",
        )
        response.set_etag(android_release["artifact"]["sha256"])
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    app.register_blueprint(blueprint)
