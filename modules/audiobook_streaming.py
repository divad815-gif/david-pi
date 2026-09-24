"""Safe, bounded generation of rebuildable audiobook streaming derivatives."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("DAVID_PI_AUDIOBOOKS_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "audiobooks"))
ORIGINALS, STREAMING, STAGING = ROOT / "originals", ROOT / "streaming", ROOT / "incoming" / "streaming"
DERIVATIVE_STATE = Path(os.environ.get("DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE", ROOT / "derivative-state"))
QUEUE_DB, WORKER_LOCK = DERIVATIVE_STATE / "playback-queue.db", DERIVATIVE_STATE / "prepare-worker.lock"
# V3 forces derivatives produced under the former shallow validator to rebuild.
# Range playback remains available while this rebuildable cache is regenerated.
FORMAT_VERSION = 3
SEGMENT_SECONDS = int(os.environ.get("DAVID_PI_AUDIOBOOK_SEGMENT_SECONDS", "20"))
MONO_BITRATE = os.environ.get("DAVID_PI_AUDIOBOOK_MONO_BITRATE", "64k")
STEREO_BITRATE = os.environ.get("DAVID_PI_AUDIOBOOK_STEREO_BITRATE", "96k")
MIN_FREE_BYTES = int(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MIN_FREE", str(100 * 1024**3)))
MAX_TEMP_C = float(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MAX_TEMP", "75"))
MAX_LOAD = float(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MAX_LOAD", "3.0"))
LEASE_SECONDS = int(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_LEASE_SECONDS", "600"))
HEARTBEAT_SECONDS = max(5, min(60, LEASE_SECONDS // 3))
RETENTION_DAYS = int(os.environ.get("DAVID_PI_AUDIOBOOK_DERIVATIVE_RETENTION_DAYS", "14"))
CLEANUP_INTERVAL_SECONDS = max(300, int(os.environ.get("DAVID_PI_AUDIOBOOK_CLEANUP_INTERVAL", "3600")))
MAX_MONO_BITRATE = 72_000
MAX_STEREO_BITRATE = 108_000
MAX_PLAYLIST_BYTES = 8 * 1024**2
# Account for fMP4 boxes, HLS byte-range metadata, and encoder variation above
# the validated AAC stream bitrate. The same ceiling is enforced after encode.
CONTAINER_OVERHEAD_NUMERATOR = 11
CONTAINER_OVERHEAD_DENOMINATOR = 10
MAX_FORECAST_BYTES = (1 << 63) - 1
ID = re.compile(r"^[0-9a-f]{32}$")
for directory in (ROOT, ORIGINALS, STREAMING, STAGING, DERIVATIVE_STATE): directory.mkdir(parents=True, exist_ok=True)
_last_cleanup_monotonic = None

def now(): return datetime.now(timezone.utc).isoformat()
def _future(seconds=LEASE_SECONDS): return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()

@contextmanager
def queue_connection(timeout_seconds=30):
    timeout_seconds = max(0.0, float(timeout_seconds))
    connection=sqlite3.connect(QUEUE_DB,timeout=timeout_seconds);connection.row_factory=sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={int(timeout_seconds * 1000)}")
    try: yield connection;connection.commit()
    except Exception: connection.rollback();raise
    finally: connection.close()

def initialize_queue():
    """Create or upgrade the shared queue safely under concurrent imports."""
    for attempt in range(20):
        connection = sqlite3.connect(QUEUE_DB, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            # Gunicorn and the preparer can import this module together during
            # rollout. Serialize both schema inspection and ALTER TABLE work.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_playback_jobs(
              book_id TEXT PRIMARY KEY,stored_name TEXT NOT NULL,source_size INTEGER NOT NULL DEFAULT 0,source_sha256 TEXT,
              state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','preparing','ready','failed')),
              format_version INTEGER NOT NULL DEFAULT 1,derivative_bytes INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
              available_at TEXT NOT NULL,updated_at TEXT NOT NULL,generated_at TEXT,error_code TEXT,
              generation INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_expires_at TEXT)""")
            columns={row[1] for row in connection.execute("PRAGMA table_info(audiobook_playback_jobs)")}
            for name,definition in (("generation","INTEGER NOT NULL DEFAULT 0"),("lease_token","TEXT"),("lease_expires_at","TEXT")):
                if name not in columns:
                    connection.execute(f"ALTER TABLE audiobook_playback_jobs ADD COLUMN {name} {definition}")
                    columns.add(name)
            connection.execute("CREATE INDEX IF NOT EXISTS audiobook_playback_jobs_next_idx ON audiobook_playback_jobs(state,available_at,source_size)")
            # This is the only catalog surface available to the derivative
            # worker. It intentionally excludes owners, titles, and all other
            # platform databases while retaining enough metadata to validate
            # the source and forecast rebuild storage.
            connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_catalog_snapshot(
              book_id TEXT PRIMARY KEY,stored_name TEXT NOT NULL,source_size INTEGER NOT NULL DEFAULT 0,
              source_sha256 TEXT,duration_seconds REAL NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_queue_metadata(
              key TEXT PRIMARY KEY,value TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_import_reservations(
              reservation_token TEXT PRIMARY KEY,book_id TEXT NOT NULL UNIQUE,stored_name TEXT NOT NULL,
              source_size INTEGER NOT NULL,source_sha256 TEXT NOT NULL,duration_seconds REAL NOT NULL,
              staging_name TEXT NOT NULL,source_device TEXT NOT NULL,source_inode TEXT NOT NULL,
              cover_staging_name TEXT,cover_name TEXT,cover_device TEXT,cover_inode TEXT,
              state TEXT NOT NULL CHECK(state IN ('reserved','published','queued','activated','aborting')),
              created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            connection.execute("CREATE INDEX IF NOT EXISTS audiobook_import_reservations_state_idx ON audiobook_import_reservations(state,updated_at)")
            connection.commit()
            return
        except sqlite3.OperationalError as error:
            connection.rollback()
            transient = "locked" in str(error).lower() or "duplicate column" in str(error).lower()
            if not transient or attempt == 19:
                raise
            time.sleep(min(0.05 * (attempt + 1), 0.5))
        finally:
            connection.close()
initialize_queue()

def _valid_id(book_id): return bool(ID.fullmatch(str(book_id or "").lower()))


def _safe_basename(value, code):
    name = str(value or "")
    if not name or Path(name).name != name or name in {".", ".."} or "\x00" in name:
        raise ValueError(code)
    return name


def _nonnegative_integer(value, code):
    """Normalize storage metadata without silently truncating or overflowing."""
    if isinstance(value, bool):
        raise ValueError(code)
    candidate = 0 if value is None or value == "" else value
    if isinstance(candidate, float):
        if not math.isfinite(candidate) or not candidate.is_integer():
            raise ValueError(code)
        number = int(candidate)
    elif isinstance(candidate, int):
        number = candidate
    elif isinstance(candidate, str):
        if not re.fullmatch(r"\+?\d+", candidate.strip()):
            raise ValueError(code)
        number = int(candidate)
    else:
        try:
            number = int(candidate)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(code) from error
        if candidate != number:
            raise ValueError(code)
    if number < 0 or number > MAX_FORECAST_BYTES:
        raise ValueError(code)
    return number


def _finite_duration(value, *, allow_zero=True, code="duration_invalid"):
    try:
        duration = float(value or 0)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(code) from error
    if not math.isfinite(duration) or duration < 0 or (not allow_zero and duration <= 0):
        raise ValueError(code)
    return duration


def _positive_duration(
    value,
    *,
    invalid_code="duration_invalid",
    unavailable_code="duration_unavailable",
):
    """Distinguish corrupt timing from a finite but unresolved duration."""
    duration = _finite_duration(value, code=invalid_code)
    if duration <= 0:
        raise ValueError(unavailable_code)
    return duration


def _checked_add(*values, code="forecast_overflow"):
    total = 0
    for value in values:
        number = _nonnegative_integer(value, code)
        if total > MAX_FORECAST_BYTES - number:
            raise ValueError(code)
        total += number
    return total


def _derivative_byte_ceiling(duration_seconds, channels=2):
    """Return the largest derivative accepted by the post-encode validator."""
    duration = _positive_duration(duration_seconds)
    bitrate = MAX_MONO_BITRATE if channels == 1 else MAX_STEREO_BITRATE
    raw_bytes = duration * bitrate / 8
    if not math.isfinite(raw_bytes) or raw_bytes > MAX_FORECAST_BYTES:
        raise ValueError("forecast_overflow")
    encoded_bytes = math.ceil(raw_bytes)
    media_bytes = (
        encoded_bytes * CONTAINER_OVERHEAD_NUMERATOR
        + CONTAINER_OVERHEAD_DENOMINATOR - 1
    ) // CONTAINER_OVERHEAD_DENOMINATOR
    if media_bytes > MAX_FORECAST_BYTES - MAX_PLAYLIST_BYTES:
        raise ValueError("forecast_overflow")
    return _checked_add(media_bytes, MAX_PLAYLIST_BYTES)


def _forecast_duration_ceiling(duration_seconds):
    """Include the full duration tolerance accepted by derivative validation."""
    duration = _positive_duration(duration_seconds)
    ceiling = duration + max(2.0, duration * 0.02)
    if not math.isfinite(ceiling):
        raise ValueError("forecast_overflow")
    return ceiling


def _normalize_catalog_row(item):
    row = dict(item)
    book_id = str(row.get("id") or "")
    source_size = _nonnegative_integer(row.get("byte_size"), "source_size_invalid")
    try:
        duration_seconds = float(row.get("duration_seconds") or 0)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("duration_invalid") from error
    if not math.isfinite(duration_seconds):
        raise ValueError("duration_invalid")
    # Old catalogs may contain absent, zero, or negative timing. Preserve the
    # book as range-only and let later reconciliation activate it only after a
    # trusted positive duration has been written by the web tier.
    duration_seconds = max(0.0, duration_seconds)
    if not _valid_id(book_id):
        return None
    row.update(
        id=book_id,
        byte_size=source_size,
        duration_seconds=duration_seconds,
    )
    return row


def _safe_source(stored_name):
    if Path(stored_name).name!=stored_name: raise ValueError("invalid_source_name")
    source=ORIGINALS/stored_name
    if source.is_symlink() or not source.is_file(): raise ValueError("source_unavailable")
    resolved=source.resolve()
    if ORIGINALS.resolve() not in resolved.parents: raise ValueError("source_outside_library")
    return resolved

def _remove_tree(path):
    """Remove only the named rebuildable object; never follow a link."""
    if path.is_symlink() or path.is_file(): path.unlink(missing_ok=True)
    elif path.is_dir(): shutil.rmtree(path)

def _upsert_job(connection,book_id,stored_name,source_size,source_sha256,duration_seconds):
    duration_seconds = _positive_duration(duration_seconds)
    timestamp=now()
    current=connection.execute("SELECT * FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()
    catalog=connection.execute("SELECT duration_seconds,active FROM audiobook_catalog_snapshot WHERE book_id=?",(book_id,)).fetchone()
    try:
        stored_duration = (
            _positive_duration(catalog["duration_seconds"])
            if catalog and catalog["active"] == 1
            else None
        )
    except (TypeError, ValueError, OverflowError):
        stored_duration = None
    duration_unchanged=stored_duration == duration_seconds
    unchanged=current and duration_unchanged and current["stored_name"]==stored_name and current["source_size"]==source_size and current["source_sha256"]==source_sha256
    if unchanged and current["format_version"]==FORMAT_VERSION and current["state"] in {"pending","preparing"}: return current["state"]
    if unchanged and current["state"]=="ready" and derivative_paths(book_id,False): return "ready"
    connection.execute("""INSERT INTO audiobook_playback_jobs(book_id,stored_name,source_size,source_sha256,state,format_version,available_at,updated_at)
      VALUES(?,?,?,?,'pending',?,?,?) ON CONFLICT(book_id) DO UPDATE SET stored_name=excluded.stored_name,source_size=excluded.source_size,
      source_sha256=excluded.source_sha256,state='pending',format_version=excluded.format_version,
      available_at=excluded.available_at,updated_at=excluded.updated_at,error_code=NULL,attempts=0,generation=audiobook_playback_jobs.generation+1,
      lease_token=NULL,lease_expires_at=NULL""",(book_id,stored_name,source_size,source_sha256,FORMAT_VERSION,timestamp,timestamp))
    return "pending"


def _mark_duration_unavailable(connection,row):
    """Keep a legacy book range-playable without making it derivative-eligible."""
    timestamp=now()
    connection.execute("""INSERT INTO audiobook_playback_jobs(
      book_id,stored_name,source_size,source_sha256,state,format_version,derivative_bytes,
      attempts,available_at,updated_at,generated_at,error_code,generation,lease_token,lease_expires_at)
      VALUES(?,?,?,?,'failed',?,0,0,?,?,NULL,'duration_unavailable',0,NULL,NULL)
      ON CONFLICT(book_id) DO UPDATE SET stored_name=excluded.stored_name,source_size=excluded.source_size,
      source_sha256=excluded.source_sha256,state='failed',format_version=excluded.format_version,
      derivative_bytes=0,attempts=0,available_at=excluded.available_at,updated_at=excluded.updated_at,
      generated_at=NULL,error_code='duration_unavailable',generation=audiobook_playback_jobs.generation+1,
      lease_token=NULL,lease_expires_at=NULL""",
      (row["id"],row["stored_name"],row["byte_size"],row.get("sha256"),FORMAT_VERSION,timestamp,timestamp))

def _upsert_catalog_row(connection,book_id,stored_name,source_size,source_sha256,duration_seconds,active=True):
    source_size = _nonnegative_integer(source_size, "source_size_invalid")
    duration_seconds = _finite_duration(duration_seconds, code="duration_invalid")
    connection.execute("""INSERT INTO audiobook_catalog_snapshot(book_id,stored_name,source_size,source_sha256,duration_seconds,active,updated_at)
      VALUES(?,?,?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET stored_name=excluded.stored_name,source_size=excluded.source_size,
      source_sha256=excluded.source_sha256,duration_seconds=excluded.duration_seconds,active=excluded.active,updated_at=excluded.updated_at""",
      (book_id,stored_name,source_size,source_sha256,duration_seconds,1 if active else 0,now()))


def _reservation_forecast_rows(connection, *, exclude_token=None):
    conditions = ["state IN ('reserved','published','queued')"]
    arguments = []
    if exclude_token is not None:
        conditions.append("reservation_token<>?")
        arguments.append(exclude_token)
    return [
        {
            "id": row["book_id"],
            "stored_name": row["stored_name"],
            "byte_size": row["source_size"],
            "sha256": row["source_sha256"],
            "duration_seconds": row["duration_seconds"],
        }
        for row in connection.execute(
            "SELECT * FROM audiobook_import_reservations WHERE " + " AND ".join(conditions),
            arguments,
        )
    ]


def _active_catalog_rows(connection, *, exclude_book_id=None):
    statement = (
        "SELECT book_id AS id,stored_name,source_size AS byte_size,"
        "source_sha256 AS sha256,duration_seconds FROM audiobook_catalog_snapshot "
        "WHERE active=1"
    )
    arguments = []
    if exclude_book_id is not None:
        statement += " AND book_id<>?"
        arguments.append(exclude_book_id)
    return [dict(row) for row in connection.execute(statement, arguments)]


def reserve_import(
    reservation_token,
    book_id,
    stored_name,
    source_size,
    source_sha256,
    duration_seconds,
    staging_name,
    source_device,
    source_inode,
    *,
    cover_staging_name=None,
    cover_name=None,
    cover_device=None,
    cover_inode=None,
):
    """Reserve derivative capacity without exposing an original or queue job.

    The web tier writes the upload only to its exclusive incoming name before
    this call. The serialized forecast includes every other unfinished import,
    preventing concurrent requests from each spending the same free bytes.
    """
    reservation_token = str(reservation_token or "").lower()
    book_id = str(book_id or "").lower()
    if not _valid_id(reservation_token): raise ValueError("invalid_reservation_token")
    if not _valid_id(book_id): raise ValueError("invalid_book_id")
    stored_name = _safe_basename(stored_name, "invalid_source_name")
    staging_name = _safe_basename(staging_name, "invalid_staging_name")
    source_size = _nonnegative_integer(source_size, "source_size_invalid")
    duration_seconds = _positive_duration(duration_seconds)
    source_sha256 = str(source_sha256 or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source_sha256_invalid")
    source_device = str(_nonnegative_integer(source_device, "source_identity_invalid"))
    source_inode = str(_nonnegative_integer(source_inode, "source_identity_invalid"))
    if cover_staging_name is None:
        if any(value is not None for value in (cover_name, cover_device, cover_inode)):
            raise ValueError("cover_identity_invalid")
    else:
        cover_staging_name = _safe_basename(cover_staging_name, "invalid_cover_staging_name")
        cover_name = _safe_basename(cover_name, "invalid_cover_name")
        cover_device = str(_nonnegative_integer(cover_device, "cover_identity_invalid"))
        cover_inode = str(_nonnegative_integer(cover_inode, "cover_identity_invalid"))
    queued_row = {
        "id": book_id,
        "stored_name": stored_name,
        "byte_size": source_size,
        "sha256": source_sha256,
        "duration_seconds": duration_seconds,
    }
    timestamp = now()
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        collision = connection.execute(
            """SELECT 1 FROM audiobook_playback_jobs WHERE book_id=?
               UNION ALL SELECT 1 FROM audiobook_catalog_snapshot WHERE book_id=?
               UNION ALL SELECT 1 FROM audiobook_import_reservations WHERE book_id=? OR reservation_token=?
               LIMIT 1""",
            (book_id, book_id, book_id, reservation_token),
        ).fetchone()
        derivative_root = STREAMING / book_id
        if collision or derivative_root.exists() or derivative_root.is_symlink():
            raise ValueError("import_id_conflict")
        rows = _active_catalog_rows(connection)
        rows.extend(_reservation_forecast_rows(connection))
        forecast = storage_forecast([*rows, queued_row])
        if not forecast["fits"]:
            raise ValueError("forecast_low_storage")
        connection.execute(
            """INSERT INTO audiobook_import_reservations(
              reservation_token,book_id,stored_name,source_size,source_sha256,duration_seconds,
              staging_name,source_device,source_inode,cover_staging_name,cover_name,cover_device,
              cover_inode,state,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)""",
            (
                reservation_token,book_id,stored_name,source_size,source_sha256,duration_seconds,
                staging_name,source_device,source_inode,cover_staging_name,cover_name,cover_device,
                cover_inode,timestamp,timestamp,
            ),
        )
    return queued_row


def import_reservation(reservation_token, book_id=None):
    reservation_token = str(reservation_token or "").lower()
    if not _valid_id(reservation_token): return None
    with queue_connection() as connection:
        if book_id is None:
            row = connection.execute(
                "SELECT * FROM audiobook_import_reservations WHERE reservation_token=?",
                (reservation_token,),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM audiobook_import_reservations WHERE reservation_token=? AND book_id=?",
                (reservation_token, str(book_id or "").lower()),
            ).fetchone()
    return dict(row) if row else None


def mark_import_published(reservation_token, book_id):
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """UPDATE audiobook_import_reservations SET state='published',updated_at=?
               WHERE reservation_token=? AND book_id=? AND state='reserved'""",
            (now(), reservation_token, book_id),
        )
        if changed.rowcount != 1:
            raise ValueError("import_reservation_conflict")


def commit_reserved_import(reservation_token, book_id):
    """Create an inert queue/catalog pair while the library row is uncommitted."""
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            """SELECT * FROM audiobook_import_reservations
               WHERE reservation_token=? AND book_id=? AND state='published'""",
            (reservation_token, book_id),
        ).fetchone()
        if not reservation:
            raise ValueError("import_reservation_conflict")
        if connection.execute(
            """SELECT 1 FROM audiobook_playback_jobs WHERE book_id=?
               UNION ALL SELECT 1 FROM audiobook_catalog_snapshot WHERE book_id=? LIMIT 1""",
            (book_id, book_id),
        ).fetchone():
            raise ValueError("import_id_conflict")
        rows = _active_catalog_rows(connection)
        rows.extend(_reservation_forecast_rows(connection))
        forecast = storage_forecast(rows)
        if not forecast["fits"]:
            raise ValueError("forecast_low_storage")
        timestamp = now()
        connection.execute(
            """INSERT INTO audiobook_playback_jobs(
              book_id,stored_name,source_size,source_sha256,state,format_version,derivative_bytes,
              attempts,available_at,updated_at,generated_at,error_code,generation,lease_token,lease_expires_at)
              VALUES(?,?,?,?,'failed',?,0,0,?,?,NULL,'import_pending',0,NULL,NULL)""",
            (
                reservation["book_id"],reservation["stored_name"],reservation["source_size"],
                reservation["source_sha256"],FORMAT_VERSION,timestamp,timestamp,
            ),
        )
        connection.execute(
            """INSERT INTO audiobook_catalog_snapshot(
              book_id,stored_name,source_size,source_sha256,duration_seconds,active,updated_at)
              VALUES(?,?,?,?,?,0,?)""",
            (
                reservation["book_id"],reservation["stored_name"],reservation["source_size"],
                reservation["source_sha256"],reservation["duration_seconds"],timestamp,
            ),
        )
        connection.execute(
            """UPDATE audiobook_import_reservations SET state='queued',updated_at=?
               WHERE reservation_token=? AND book_id=? AND state='published'""",
            (timestamp,reservation_token,book_id),
        )


def activate_reserved_import(reservation_token, book_id):
    """Expose a reserved job only after the authoritative library commit."""
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            """SELECT * FROM audiobook_import_reservations
               WHERE reservation_token=? AND book_id=? AND state='queued'""",
            (reservation_token, book_id),
        ).fetchone()
        if not reservation:
            raise ValueError("import_reservation_conflict")
        rows = _active_catalog_rows(connection, exclude_book_id=book_id)
        rows.extend(_reservation_forecast_rows(connection))
        forecast = storage_forecast(rows)
        if not forecast["fits"]:
            raise ValueError("forecast_low_storage")
        timestamp = now()
        job = connection.execute(
            """UPDATE audiobook_playback_jobs
               SET state='pending',available_at=?,updated_at=?,error_code=NULL,attempts=0,
                   generation=generation+1,lease_token=NULL,lease_expires_at=NULL
               WHERE book_id=? AND stored_name=? AND source_size=? AND source_sha256=?
                 AND state='failed' AND error_code='import_pending'""",
            (
                timestamp,timestamp,reservation["book_id"],reservation["stored_name"],
                reservation["source_size"],reservation["source_sha256"],
            ),
        )
        catalog = connection.execute(
            """UPDATE audiobook_catalog_snapshot SET active=1,updated_at=?
               WHERE book_id=? AND stored_name=? AND source_size=? AND source_sha256=?
                 AND duration_seconds=? AND active=0""",
            (
                timestamp,reservation["book_id"],reservation["stored_name"],
                reservation["source_size"],reservation["source_sha256"],
                reservation["duration_seconds"],
            ),
        )
        if job.rowcount != 1 or catalog.rowcount != 1:
            raise ValueError("import_queue_conflict")
        connection.execute(
            """UPDATE audiobook_import_reservations SET state='activated',updated_at=?
               WHERE reservation_token=? AND book_id=? AND state='queued'""",
            (timestamp,reservation_token,book_id),
        )


def reactivate_aborting_import(reservation_token, book_id):
    """Rebuild an aborted queue pair after recovery proves the library won.

    The caller must hold the authoritative library ``BEGIN IMMEDIATE`` lock
    and must have verified the exact library row and original inode.  This
    queue transaction then restores claimability before the cleanup journal is
    eligible for removal, including recovery from an older cross-database
    abort race.
    """
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            """SELECT * FROM audiobook_import_reservations
               WHERE reservation_token=? AND book_id=? AND state='aborting'""",
            (reservation_token, book_id),
        ).fetchone()
        if not reservation:
            raise ValueError("import_reservation_conflict")
        if connection.execute(
            """SELECT 1 FROM audiobook_playback_jobs WHERE book_id=?
               UNION ALL SELECT 1 FROM audiobook_catalog_snapshot WHERE book_id=? LIMIT 1""",
            (book_id, book_id),
        ).fetchone():
            raise ValueError("import_cleanup_conflict")
        queued_row = {
            "id": reservation["book_id"],
            "stored_name": reservation["stored_name"],
            "byte_size": reservation["source_size"],
            "sha256": reservation["source_sha256"],
            "duration_seconds": reservation["duration_seconds"],
        }
        forecast = storage_forecast([*_active_catalog_rows(connection), queued_row])
        if not forecast["fits"]:
            raise ValueError("forecast_low_storage")
        timestamp = now()
        connection.execute(
            """INSERT INTO audiobook_playback_jobs(
              book_id,stored_name,source_size,source_sha256,state,format_version,derivative_bytes,
              attempts,available_at,updated_at,generated_at,error_code,generation,lease_token,lease_expires_at)
              VALUES(?,?,?,?,'pending',?,0,0,?,?,NULL,NULL,0,NULL,NULL)""",
            (
                reservation["book_id"],reservation["stored_name"],reservation["source_size"],
                reservation["source_sha256"],FORMAT_VERSION,timestamp,timestamp,
            ),
        )
        connection.execute(
            """INSERT INTO audiobook_catalog_snapshot(
              book_id,stored_name,source_size,source_sha256,duration_seconds,active,updated_at)
              VALUES(?,?,?,?,?,1,?)""",
            (
                reservation["book_id"],reservation["stored_name"],reservation["source_size"],
                reservation["source_sha256"],reservation["duration_seconds"],timestamp,
            ),
        )
        changed = connection.execute(
            """UPDATE audiobook_import_reservations SET state='activated',updated_at=?
               WHERE reservation_token=? AND book_id=? AND state='aborting'""",
            (timestamp,reservation_token,book_id),
        )
        if changed.rowcount != 1:
            raise ValueError("import_reservation_conflict")


def begin_abort_import(reservation_token, book_id):
    """Remove only rows proven to belong to this import; retain its cleanup journal."""
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservation = connection.execute(
            "SELECT * FROM audiobook_import_reservations WHERE reservation_token=? AND book_id=?",
            (reservation_token, book_id),
        ).fetchone()
        if not reservation:
            return None
        job = connection.execute(
            "SELECT * FROM audiobook_playback_jobs WHERE book_id=?", (book_id,)
        ).fetchone()
        catalog = connection.execute(
            "SELECT * FROM audiobook_catalog_snapshot WHERE book_id=?", (book_id,)
        ).fetchone()
        queue_expected = reservation["state"] in {"queued", "activated", "aborting"}
        if job and not (
            queue_expected
            and job["stored_name"] == reservation["stored_name"]
            and job["source_size"] == reservation["source_size"]
            and job["source_sha256"] == reservation["source_sha256"]
        ):
            raise ValueError("import_cleanup_conflict")
        if catalog and not (
            queue_expected
            and catalog["stored_name"] == reservation["stored_name"]
            and catalog["source_size"] == reservation["source_size"]
            and catalog["source_sha256"] == reservation["source_sha256"]
            and catalog["duration_seconds"] == reservation["duration_seconds"]
        ):
            raise ValueError("import_cleanup_conflict")
        if job: connection.execute("DELETE FROM audiobook_playback_jobs WHERE book_id=?", (book_id,))
        if catalog: connection.execute("DELETE FROM audiobook_catalog_snapshot WHERE book_id=?", (book_id,))
        connection.execute(
            "UPDATE audiobook_import_reservations SET state='aborting',updated_at=? WHERE reservation_token=? AND book_id=?",
            (now(),reservation_token,book_id),
        )
        result = dict(reservation)
        result["state"] = "aborting"
        return result


def finish_abort_import(reservation_token, book_id):
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "DELETE FROM audiobook_import_reservations WHERE reservation_token=? AND book_id=? AND state='aborting'",
            (reservation_token, book_id),
        )
        if changed.rowcount != 1:
            raise ValueError("import_reservation_conflict")


def finish_import(reservation_token, book_id):
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "DELETE FROM audiobook_import_reservations WHERE reservation_token=? AND book_id=? AND state='activated'",
            (reservation_token, book_id),
        )
        if changed.rowcount != 1:
            raise ValueError("import_reservation_conflict")


def stale_import_reservations(updated_before):
    with queue_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM audiobook_import_reservations WHERE updated_at<=? ORDER BY created_at,reservation_token",
            (str(updated_before),),
        ).fetchall()
    return [dict(row) for row in rows]


def pending_import_reservations():
    with queue_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM audiobook_import_reservations ORDER BY created_at,reservation_token"
        ).fetchall()
    return [dict(row) for row in rows]


def claim_import_recovery(interval_seconds, *, current_epoch=None):
    """Elect one web process to run the bounded import recovery pass.

    Gunicorn workers all execute the same request hook.  The queue database is
    the shared serialization point, so this lease prevents each process from
    running the cross-database recovery pass on every health check.  A process
    crash can delay the next pass by at most ``interval_seconds``; it cannot
    strand a reservation until another process restart.
    """
    interval_seconds = max(1, _nonnegative_integer(
        interval_seconds, "import_recovery_interval_invalid"
    ))
    timestamp = time.time() if current_epoch is None else float(current_epoch)
    if not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("import_recovery_time_invalid")
    # This runs in the request path (normally the health probe), so an active
    # importer wins quickly rather than letting a maintenance election delay
    # an otherwise healthy response.
    with queue_connection(timeout_seconds=0.25) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM audiobook_queue_metadata WHERE key='import_recovery_next_epoch'"
        ).fetchone()
        try:
            next_epoch = float(row["value"]) if row else 0.0
        except (TypeError, ValueError, OverflowError):
            next_epoch = 0.0
        if math.isfinite(next_epoch) and next_epoch > timestamp:
            return False
        connection.execute(
            """INSERT INTO audiobook_queue_metadata(key,value)
               VALUES('import_recovery_next_epoch',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (str(timestamp + interval_seconds),),
        )
    return True

def enqueue(book_id,stored_name,source_size=0,source_sha256=None,duration_seconds=0):
    if not _valid_id(book_id): raise ValueError("invalid_book_id")
    source_size=_nonnegative_integer(source_size,"source_size_invalid")
    duration_seconds=_positive_duration(duration_seconds)
    queued_row={
        "id":book_id,
        "stored_name":stored_name,
        "byte_size":source_size,
        "sha256":source_sha256,
        "duration_seconds":duration_seconds,
    }
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM audiobook_import_reservations WHERE book_id=?", (book_id,)
        ).fetchone():
            raise ValueError("import_reservation_conflict")
        rows=_active_catalog_rows(connection,exclude_book_id=book_id)
        rows.extend(_reservation_forecast_rows(connection))
        # Forecast the exact catalog state this transaction is about to make
        # claimable. A failure therefore cannot leave an unforecast queue row.
        storage_forecast([*rows,queued_row])
        state=_upsert_job(connection,book_id,stored_name,source_size,source_sha256,duration_seconds)
        _upsert_catalog_row(connection,book_id,stored_name,source_size,source_sha256,duration_seconds,True)
    return state

def suspend(book_id):
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE audiobook_playback_jobs SET state='failed',updated_at=?,error_code='inactive',generation=generation+1,lease_token=NULL,lease_expires_at=NULL WHERE book_id=?",(now(),book_id))
        connection.execute("UPDATE audiobook_catalog_snapshot SET active=0,updated_at=? WHERE book_id=?",(now(),book_id))

def reconcile_catalog(rows):
    """Make the queue reflect authoritative active catalog rows; never inspect orphan originals."""
    present=set();eligible=[];range_only=[]
    for item in rows:
        row=_normalize_catalog_row(item)
        if row is None: continue
        present.add(row["id"])
        if row["duration_seconds"] > 0:
            eligible.append(row)
        else:
            range_only.append(row)
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reservations=_reservation_forecast_rows(connection)
        overlap={row["id"] for row in eligible}.intersection(row["id"] for row in reservations)
        if overlap:
            raise ValueError("import_reservation_conflict")
        # Validate the serialized state that will exist after this transaction,
        # including capacity promised to uploads that have not been published.
        forecast=storage_forecast([*eligible,*reservations])
        for row in eligible:
            _upsert_job(connection,row["id"],row["stored_name"],row["byte_size"],row.get("sha256"),row["duration_seconds"])
            _upsert_catalog_row(connection,row["id"],row["stored_name"],row["byte_size"],row.get("sha256"),row["duration_seconds"],True)
        for row in range_only:
            _mark_duration_unavailable(connection,row)
            _upsert_catalog_row(connection,row["id"],row["stored_name"],row["byte_size"],row.get("sha256"),row["duration_seconds"],False)
        queued=connection.execute(
            """SELECT jobs.book_id FROM audiobook_playback_jobs AS jobs
               LEFT JOIN audiobook_import_reservations AS imports ON imports.book_id=jobs.book_id
               WHERE imports.book_id IS NULL"""
        ).fetchall()
        for row in queued:
            if row["book_id"] not in present:
                connection.execute("UPDATE audiobook_playback_jobs SET state='failed',error_code='inactive',updated_at=?,generation=generation+1,lease_token=NULL,lease_expires_at=NULL WHERE book_id=?",(now(),row["book_id"]))
        if present:
            placeholders=','.join('?' for _ in present)
            connection.execute(f"UPDATE audiobook_catalog_snapshot SET active=0,updated_at=? WHERE book_id NOT IN ({placeholders})",(now(),*present))
        else:
            connection.execute("UPDATE audiobook_catalog_snapshot SET active=0,updated_at=?",(now(),))
        connection.execute("INSERT INTO audiobook_queue_metadata(key,value) VALUES('catalog_reconciled_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(now(),))
    return {**forecast,"range_only_books":len(range_only)}

def _tree_bytes(path):
    total=0
    if path.is_symlink() or not path.is_dir(): return 0
    for candidate in path.rglob('*'):
        try:
            if candidate.is_file() and not candidate.is_symlink(): total+=candidate.stat().st_size
        except OSError: pass
    return total

def storage_forecast(rows):
    """Forecast final growth plus one full atomic staging replacement."""
    estimated=existing=legacy=0;count=0;staging_high_water=0
    for item in rows:
        row=_normalize_catalog_row(item)
        if row is None:continue
        book_id=row['id']
        if row['duration_seconds'] <= 0:
            raise ValueError('duration_unavailable')
        # The validator permits up to the stereo ceiling. Include bounded HLS
        # and fMP4 overhead even when the source itself is unusually compact.
        duration_ceiling=_forecast_duration_ceiling(row['duration_seconds'])
        target=max(row['byte_size'],_derivative_byte_ceiling(duration_ceiling,2))
        estimated=_checked_add(estimated,target)
        staging_high_water=max(staging_high_water,target)
        count+=1
        parent=STREAMING/book_id;current=_tree_bytes(parent/f'v{FORMAT_VERSION}')
        existing=_checked_add(existing,min(target,current))
        legacy_bytes=sum(_tree_bytes(path) for path in parent.glob('v*') if path.name!=f'v{FORMAT_VERSION}') if parent.is_dir() and not parent.is_symlink() else 0
        legacy=_checked_add(legacy,legacy_bytes)
    additional=max(0,estimated-existing)
    try:free=_nonnegative_integer(shutil.disk_usage(ROOT).free,"forecast_overflow")
    except OSError:free=0
    required=_checked_add(MIN_FREE_BYTES,additional,staging_high_water)
    return {'active_books':count,'estimated_derivative_bytes':estimated,'existing_current_bytes':existing,'retained_legacy_bytes':legacy,'additional_bytes':additional,'staging_high_water_bytes':staging_high_water,'required_free_bytes':required,'fits':free>=required}

def reconcile_catalog_from_database():
    """Read the web tier's authoritative metadata-only queue snapshot."""
    try:
        with queue_connection() as connection:
            ready=connection.execute("SELECT value FROM audiobook_queue_metadata WHERE key='catalog_reconciled_at'").fetchone()
            if not ready:return False
            rows=_active_catalog_rows(connection)
            rows.extend(_reservation_forecast_rows(connection))
        return storage_forecast(rows)
    except (sqlite3.Error,ValueError,OverflowError):return False

def derivative_paths(book_id,require_ready=True):
    if not _valid_id(book_id): return None
    directory=STREAMING/book_id/f"v{FORMAT_VERSION}";playlist,media=directory/"index.m3u8",directory/"index.m4s";root=STREAMING.resolve()
    for path in (directory,playlist,media):
        if path.is_symlink(): return None
        resolved=path.resolve(strict=False)
        if resolved!=root and root not in resolved.parents: return None
    if not playlist.is_file() or not media.is_file() or not playlist.stat().st_size or not media.stat().st_size:return None
    if require_ready and playback_status(book_id)["state"]!="ready":return None
    return playlist,media

def _status(book_id,row):
    if not row:return {"state":"pending","mode":"range","error_code":None}
    ready=row["state"]=="ready" and row["format_version"]==FORMAT_VERSION and derivative_paths(book_id,False)
    return {"state":"ready" if ready else ("pending" if row["state"]=="ready" else row["state"]),"mode":"segmented" if ready else "range","derivative_bytes":row["derivative_bytes"],"generated_at":row["generated_at"],"error_code":row["error_code"]}
def playback_status(book_id):
    with queue_connection() as connection: row=connection.execute("SELECT state,format_version,derivative_bytes,generated_at,error_code FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()
    return _status(book_id,row)
def playback_statuses(book_ids):
    identifiers=list(dict.fromkeys(str(value) for value in book_ids if value));
    if not identifiers:return {}
    placeholders=','.join('?' for _ in identifiers)
    with queue_connection() as connection: rows=connection.execute(f"SELECT book_id,state,format_version,derivative_bytes,generated_at,error_code FROM audiobook_playback_jobs WHERE book_id IN ({placeholders})",identifiers).fetchall()
    indexed={row["book_id"]:row for row in rows};return {book_id:_status(book_id,indexed.get(book_id)) for book_id in identifiers}

def _source_sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(4*1024**2),b''):digest.update(block)
    return digest.hexdigest()
def _probe(path):
    result=subprocess.run(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=codec_name,channels,bit_rate:format=duration,bit_rate","-of","json",str(path)],capture_output=True,text=True,timeout=45,check=True)
    try:payload=json.loads(result.stdout or '{}')
    except json.JSONDecodeError as error:raise ValueError('audio_probe_invalid') from error
    if not isinstance(payload,dict):raise ValueError('audio_probe_invalid')
    streams=payload.get('streams') or []
    if not streams:raise ValueError('audio_stream_missing')
    stream=streams[0];format_data=payload.get('format') or {}
    if not isinstance(stream,dict) or not isinstance(format_data,dict):raise ValueError('audio_probe_invalid')
    try:
        channels=int(stream.get('channels') or 0);duration=float(format_data.get('duration') or 0);bit_rate=int(stream.get('bit_rate') or format_data.get('bit_rate') or 0)
    except (TypeError,ValueError,OverflowError) as error:raise ValueError('audio_probe_invalid') from error
    if channels<0 or not math.isfinite(duration) or duration<0 or bit_rate<0:raise ValueError('audio_probe_invalid')
    return {"codec":str(stream.get('codec_name') or ''),"channels":channels,"duration":duration,"bit_rate":bit_rate}
def _audio_profile(source):
    profile=_probe(source);return profile["codec"],profile["channels"]
def _ranges(text,media_size):
    ranges=[];pending=None;pending_duration=False;maps=0;segments=0
    for line in text.splitlines():
        if line.startswith('#EXT-X-MAP:'):
            if pending is not None or maps:raise ValueError('playlist_invalid_map')
            match=re.fullmatch(r'#EXT-X-MAP:URI="index\.m4s",BYTERANGE="(\d+)(?:@(\d+))?"',line)
            if not match:raise ValueError('playlist_invalid_map')
            length=int(match.group(1));start=int(match.group(2) or 0);ranges.append((start,start+length));maps+=1
        elif line.startswith('#EXTINF:'):
            if pending_duration:raise ValueError('playlist_invalid_duration')
            match=re.fullmatch(r'#EXTINF:([^,]+),.*',line)
            try:duration=float(match.group(1)) if match else 0
            except (TypeError,ValueError,OverflowError) as error:raise ValueError('playlist_invalid_duration') from error
            if not math.isfinite(duration) or duration<=0:raise ValueError('playlist_invalid_duration')
            pending_duration=True
        elif line.startswith('#EXT-X-BYTERANGE:'):
            if pending is not None:raise ValueError('playlist_invalid_range')
            match=re.match(r'#EXT-X-BYTERANGE:(\d+)(?:@(\d+))?$',line)
            if not match:raise ValueError('playlist_invalid_range')
            length=int(match.group(1));start=int(match.group(2)) if match.group(2) else (ranges[-1][1] if ranges else 0);pending=(start,start+length)
        elif line.startswith('#') and re.search(r'\bURI\s*=',line,re.IGNORECASE):
            raise ValueError('playlist_external_uri')
        elif line and not line.startswith('#'):
            if line!='index.m4s' or pending is None or not pending_duration:raise ValueError('playlist_external_uri')
            ranges.append(pending);pending=None;pending_duration=False;segments+=1
    if pending is not None or pending_duration or maps!=1 or not segments:raise ValueError('playlist_invalid_range')
    previous=0
    for start,end in ranges:
        if end<=start or start<previous or end>media_size:raise ValueError('playlist_range_out_of_bounds')
        previous=end
    return ranges
def _validate_derivative(directory,expected_duration=None,target_channels=None):
    expected_duration=_positive_duration(
        expected_duration,
        invalid_code='derivative_duration_invalid',
        unavailable_code='derivative_duration_unavailable',
    )
    playlist,media=directory/'index.m3u8',directory/'index.m4s'
    if not playlist.is_file() or not media.is_file() or media.stat().st_size<1024:raise ValueError('derivative_incomplete')
    if playlist.stat().st_size>MAX_PLAYLIST_BYTES:raise ValueError('playlist_oversized')
    text=playlist.read_text(encoding='utf-8')
    if not all(token in text for token in ('#EXTM3U','#EXT-X-MAP','#EXT-X-BYTERANGE','#EXTINF:','#EXT-X-ENDLIST')):raise ValueError('playlist_invalid')
    _ranges(text,media.stat().st_size);profile=_probe(playlist)
    if profile['codec']!='aac' or profile['channels'] not in {1,2}:raise ValueError('derivative_codec_invalid')
    if target_channels and profile['channels']!=target_channels:raise ValueError('derivative_channels_invalid')
    if not math.isfinite(profile['duration']) or profile['duration']<=0:raise ValueError('derivative_duration_invalid')
    ceiling=MAX_MONO_BITRATE if profile['channels']==1 else MAX_STEREO_BITRATE
    measured_bitrate=profile['bit_rate'] or int(media.stat().st_size*8/profile['duration'])
    if measured_bitrate<=0 or measured_bitrate>ceiling:raise ValueError('derivative_bitrate_invalid')
    if playlist.stat().st_size+media.stat().st_size>_derivative_byte_ceiling(profile['duration'],profile['channels']):
        raise ValueError('derivative_size_invalid')
    if abs(profile['duration']-expected_duration)>max(2,expected_duration*.02):raise ValueError('derivative_duration_invalid')
    return playlist.stat().st_size+media.stat().st_size

def system_pause_reason():
    try:
        if shutil.disk_usage(ROOT).free<MIN_FREE_BYTES:return 'low_storage'
    except OSError:return 'storage_unavailable'
    try:
        thermal=Path('/sys/class/thermal/thermal_zone0/temp')
        if thermal.is_file() and float(thermal.read_text().strip())/1000>=MAX_TEMP_C:return 'temperature_high'
    except (OSError,ValueError):pass
    try:
        if os.getloadavg()[0]>=MAX_LOAD:return 'load_high'
    except (AttributeError,OSError):pass
    return None

@contextmanager
def worker_lock(blocking=False):
    handle=WORKER_LOCK.open('a+');flags=fcntl.LOCK_EX|(0 if blocking else fcntl.LOCK_NB)
    try:fcntl.flock(handle.fileno(),flags);yield
    except BlockingIOError:raise RuntimeError('worker_already_running')
    finally:handle.close()

def claim_next_job():
    timestamp=now();token=uuid.uuid4().hex
    with queue_connection() as connection:
        connection.execute('BEGIN IMMEDIATE')
        rows=connection.execute("SELECT book_id AS id,stored_name,source_size AS byte_size,source_sha256 AS sha256,duration_seconds FROM audiobook_catalog_snapshot WHERE active=1").fetchall()
        forecast=storage_forecast(rows)
        if not forecast['fits']:
            raise ValueError('forecast_low_storage')
        connection.execute("UPDATE audiobook_playback_jobs SET state='pending',available_at=?,updated_at=?,error_code='lease_expired',generation=generation+1,lease_token=NULL,lease_expires_at=NULL WHERE state='preparing' AND lease_expires_at IS NOT NULL AND lease_expires_at<?",(timestamp,timestamp,timestamp))
        row=connection.execute("""SELECT jobs.*,catalog.duration_seconds AS forecast_duration_seconds
          FROM audiobook_playback_jobs AS jobs
          JOIN audiobook_catalog_snapshot AS catalog ON catalog.book_id=jobs.book_id
          WHERE jobs.state='pending' AND jobs.available_at<=? AND catalog.active=1
            AND catalog.duration_seconds>0 AND catalog.stored_name=jobs.stored_name
            AND catalog.source_size=jobs.source_size AND catalog.source_sha256 IS jobs.source_sha256
          ORDER BY jobs.source_size,jobs.updated_at LIMIT 1""",(timestamp,)).fetchone()
        if not row:return None
        generation=int(row['generation'])+1
        changed=connection.execute("UPDATE audiobook_playback_jobs SET state='preparing',attempts=attempts+1,updated_at=?,error_code=NULL,generation=?,lease_token=?,lease_expires_at=? WHERE book_id=? AND state='pending' AND generation=?",(timestamp,generation,token,_future(),row['book_id'],row['generation'])).rowcount
        if changed!=1:return None
        job=dict(row);job.update(generation=generation,lease_token=token);return job

def _heartbeat(job,stop):
    while not stop.wait(HEARTBEAT_SECONDS):
        with queue_connection() as connection:
            changed=connection.execute("UPDATE audiobook_playback_jobs SET updated_at=?,lease_expires_at=? WHERE book_id=? AND state='preparing' AND generation=? AND lease_token=?",(now(),_future(),job['book_id'],job['generation'],job['lease_token'])).rowcount
        if changed!=1:return

def _job_current(connection,job):
    return connection.execute("""SELECT 1 FROM audiobook_playback_jobs AS jobs
      JOIN audiobook_catalog_snapshot AS catalog ON catalog.book_id=jobs.book_id
      WHERE jobs.book_id=? AND jobs.state='preparing' AND jobs.stored_name=?
        AND jobs.format_version=? AND jobs.generation=? AND jobs.lease_token=?
        AND catalog.active=1 AND catalog.duration_seconds=?
        AND catalog.stored_name=jobs.stored_name AND catalog.source_size=jobs.source_size
        AND catalog.source_sha256 IS jobs.source_sha256""",
      (job['book_id'],job['stored_name'],FORMAT_VERSION,job['generation'],job['lease_token'],job['forecast_duration_seconds'])).fetchone()

def recover_interrupted_worker():
    STAGING.mkdir(mode=0o750,parents=True,exist_ok=True)
    STREAMING.mkdir(mode=0o750,parents=True,exist_ok=True)
    for path in STAGING.iterdir():
        try:
            _remove_tree(path)
        except OSError:pass
    # A previous publication is authoritative only after the queue CAS reached ready.
    with queue_connection() as connection:
        for parent in STREAMING.iterdir():
            if parent.is_symlink() or not parent.is_dir() or not _valid_id(parent.name):continue
            row=connection.execute("SELECT state FROM audiobook_playback_jobs WHERE book_id=?",(parent.name,)).fetchone()
            previous=sorted(path for path in parent.glob('.previous-*') if re.fullmatch(r'\.previous-[0-9a-f]{32}',path.name) and path.is_dir() and not path.is_symlink())
            if previous:
                old=previous[-1];final=parent/f'v{FORMAT_VERSION}'
                if not row or row['state']!='ready':
                    if final.exists() or final.is_symlink():_remove_tree(final)
                    old.replace(final)
                else:_remove_tree(old)
                for extra in previous[:-1]:_remove_tree(extra)
        result=connection.execute("UPDATE audiobook_playback_jobs SET state='pending',available_at=?,updated_at=?,error_code='worker_restarted',generation=generation+1,lease_token=NULL,lease_expires_at=NULL WHERE state='preparing'",(now(),now()))
    return int(result.rowcount or 0)

def _fail_job(job,error):
    code=str(error) if isinstance(error,ValueError) else 'prepare_failed'
    with queue_connection() as connection:
        row=connection.execute("SELECT attempts FROM audiobook_playback_jobs WHERE book_id=? AND generation=? AND lease_token=?",(job['book_id'],job['generation'],job['lease_token'])).fetchone()
        if not row:return
        attempts=int(row['attempts']);state='failed' if code=='job_inactive' or attempts>=3 else 'pending';delay=(datetime.now(timezone.utc)+timedelta(minutes=min(30,attempts*5))).isoformat()
        connection.execute("UPDATE audiobook_playback_jobs SET state=?,available_at=?,updated_at=?,error_code=?,lease_token=NULL,lease_expires_at=NULL WHERE book_id=? AND state='preparing' AND generation=? AND lease_token=?",(state,delay,now(),code[:64],job['book_id'],job['generation'],job['lease_token']))

def prepare_job(job):
    temporary=None;previous=None;final=None;stop=threading.Event();heart=threading.Thread(target=_heartbeat,args=(job,stop),daemon=True);heart.start()
    try:
        forecast_duration=_positive_duration(
            job.get('forecast_duration_seconds'),
            invalid_code='duration_invalid',
            unavailable_code='duration_unavailable',
        )
        source=_safe_source(job['stored_name']);source_stat=source.stat();source_hash=_source_sha256(source)
        if int(job.get('source_size') or 0) and source_stat.st_size!=int(job['source_size']):
            raise ValueError('source_size_mismatch')
        if job.get('source_sha256') and source_hash!=job['source_sha256']:
            raise ValueError('source_checksum_mismatch')
        profile=_probe(source)
        if profile['channels'] < 1:
            raise ValueError('audio_channels_invalid')
        probed_duration=_positive_duration(
            profile.get('duration'),
            invalid_code='audio_probe_invalid',
            unavailable_code='audio_duration_invalid',
        )
        if abs(probed_duration-forecast_duration)>max(2,forecast_duration*.02):
            raise ValueError('source_duration_mismatch')
        target_channels=1 if profile['channels']==1 else 2
        temporary=STAGING/f"{job['book_id']}-{uuid.uuid4().hex}";temporary.mkdir(mode=0o750,parents=True)
        command=['ffmpeg','-nostdin','-v','error','-i',str(source),'-map','0:a:0','-vn','-c:a','aac','-ac',str(target_channels),'-b:a',MONO_BITRATE if target_channels==1 else STEREO_BITRATE,'-f','hls','-hls_segment_type','fmp4','-hls_time',str(SEGMENT_SECONDS),'-hls_list_size','0','-hls_flags','independent_segments+single_file','-hls_segment_filename',str(temporary/'index.m4s'),str(temporary/'index.m3u8')]
        subprocess.run(command,capture_output=True,timeout=6*60*60,check=True)
        derivative_bytes=_validate_derivative(temporary,forecast_duration,target_channels)
        if source.stat().st_size!=source_stat.st_size or _source_sha256(source)!=source_hash:raise ValueError('source_changed_during_prepare')
        with queue_connection() as connection:
            if not _job_current(connection,job):raise ValueError('job_inactive')
        for path in temporary.iterdir():path.chmod(0o640)
        parent=STREAMING/job['book_id']
        if parent.is_symlink():raise ValueError('derivative_parent_invalid')
        parent.mkdir(mode=0o750,parents=True,exist_ok=True)
        if parent.resolve().parent!=STREAMING.resolve():raise ValueError('derivative_parent_invalid')
        final=parent/f'v{FORMAT_VERSION}';previous=parent/f'.previous-{job["lease_token"]}'
        if previous.exists() or previous.is_symlink():raise ValueError('rollback_path_exists')
        if final.is_symlink():raise ValueError('derivative_path_invalid')
        if final.exists():final.replace(previous)
        temporary.replace(final);temporary=None
        with queue_connection() as connection:
            changed=connection.execute("UPDATE audiobook_playback_jobs SET state='ready',source_size=?,source_sha256=?,derivative_bytes=?,generated_at=?,updated_at=?,error_code=NULL,lease_token=NULL,lease_expires_at=NULL WHERE book_id=? AND state='preparing' AND generation=? AND lease_token=?",(source_stat.st_size,source_hash,derivative_bytes,now(),now(),job['book_id'],job['generation'],job['lease_token'])).rowcount
        if changed!=1:
            if final.exists() or final.is_symlink():_remove_tree(final)
            if previous and previous.exists():previous.replace(final)
            raise ValueError('job_inactive')
        if previous and previous.exists():_remove_tree(previous)
        return derivative_bytes
    except Exception as error:
        if temporary is not None and (temporary.exists() or temporary.is_symlink()):_remove_tree(temporary)
        _fail_job(job,error);raise
    finally:stop.set();heart.join(timeout=1)

def cleanup_rebuildable_derivatives():
    cutoff=datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS);removed=0
    with queue_connection() as connection:
        ready=connection.execute("SELECT 1 FROM audiobook_queue_metadata WHERE key='catalog_reconciled_at'").fetchone()
        if not ready:return 0
        active={row['book_id'] for row in connection.execute("SELECT book_id FROM audiobook_catalog_snapshot WHERE active=1")}
    for parent in STREAMING.iterdir():
        if parent.is_symlink() or not parent.is_dir() or not _valid_id(parent.name):continue
        for path in parent.iterdir():
            if path.is_symlink() or not path.is_dir():continue
            old=datetime.fromtimestamp(path.stat().st_mtime,timezone.utc)<cutoff
            current=parent/f'v{FORMAT_VERSION}'
            obsolete_version=path.name.startswith('v') and path.name!=f'v{FORMAT_VERSION}' and current.is_dir() and datetime.fromtimestamp(current.stat().st_mtime,timezone.utc)<cutoff
            if old and (obsolete_version or parent.name not in active or path.name.startswith('.previous-')):_remove_tree(path);removed+=1
    return removed

def maybe_cleanup_rebuildable_derivatives(force=False):
    global _last_cleanup_monotonic
    timestamp=time.monotonic()
    if not force and _last_cleanup_monotonic is not None and timestamp-_last_cleanup_monotonic<CLEANUP_INTERVAL_SECONDS:return 0
    try:removed=cleanup_rebuildable_derivatives()
    except (OSError,sqlite3.Error):return 0
    _last_cleanup_monotonic=timestamp
    return removed

def work_once():
    forecast=reconcile_catalog_from_database()
    if not forecast:return {'state':'paused','reason':'catalog_unavailable'}
    if maybe_cleanup_rebuildable_derivatives():forecast=reconcile_catalog_from_database()
    if not forecast['fits']:return {'state':'paused','reason':'forecast_low_storage'}
    reason=system_pause_reason()
    if reason:return {'state':'paused','reason':reason}
    try:job=claim_next_job()
    except ValueError as error:
        if str(error)=='forecast_low_storage':return {'state':'paused','reason':'forecast_low_storage'}
        return {'state':'paused','reason':'catalog_unavailable'}
    if not job:return {'state':'idle'}
    try:
        result={'state':'ready','book_id':job['book_id'],'bytes':prepare_job(job)}
        maybe_cleanup_rebuildable_derivatives(force=True)
        return result
    except Exception:return {'state':'failed','book_id':job['book_id']}
def run_forever():
    while True:
        result=work_once();time.sleep(2 if result['state'] in {'ready','failed'} else 30)
