"""Bounded, derived audiobook streaming preparation.

Original audiobook files are never modified.  A single worker creates a
validated fMP4 HLS derivative and publishes it with an atomic directory rename.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(os.environ.get("DAVID_PI_AUDIOBOOKS_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "audiobooks"))
ORIGINALS = ROOT / "originals"
STREAMING = ROOT / "streaming"
STAGING = ROOT / "incoming" / "streaming"
QUEUE_DB = ROOT / "playback-queue.db"
FORMAT_VERSION = 1
SEGMENT_SECONDS = int(os.environ.get("DAVID_PI_AUDIOBOOK_SEGMENT_SECONDS", "20"))
MIN_FREE_BYTES = int(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MIN_FREE", str(100 * 1024**3)))
MAX_TEMP_C = float(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MAX_TEMP", "75"))
MAX_LOAD = float(os.environ.get("DAVID_PI_AUDIOBOOK_PREPARE_MAX_LOAD", "3.0"))

for directory in (ROOT, ORIGINALS, STREAMING, STAGING):
    directory.mkdir(parents=True, exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def queue_connection():
    connection = sqlite3.connect(QUEUE_DB, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_queue():
    with queue_connection() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS audiobook_playback_jobs(
               book_id TEXT PRIMARY KEY,
               stored_name TEXT NOT NULL,
               source_size INTEGER NOT NULL DEFAULT 0,
               source_sha256 TEXT,
               state TEXT NOT NULL DEFAULT 'pending'
                 CHECK(state IN ('pending','preparing','ready','failed')),
               format_version INTEGER NOT NULL DEFAULT 1,
               derivative_bytes INTEGER NOT NULL DEFAULT 0,
               attempts INTEGER NOT NULL DEFAULT 0,
               available_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               generated_at TEXT,
               error_code TEXT
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS audiobook_playback_jobs_next_idx "
            "ON audiobook_playback_jobs(state,available_at,source_size)"
        )


initialize_queue()


def _safe_source(stored_name):
    if Path(stored_name).name != stored_name:
        raise ValueError("invalid_source_name")
    source = ORIGINALS / stored_name
    if source.is_symlink() or not source.is_file():
        raise ValueError("source_unavailable")
    resolved = source.resolve()
    if ORIGINALS.resolve() not in resolved.parents:
        raise ValueError("source_outside_library")
    return resolved


def enqueue(book_id, stored_name, source_size=0):
    """Queue a missing or changed derivative without disturbing a ready one."""
    source_size = max(0, int(source_size or 0))
    timestamp = now()
    with queue_connection() as connection:
        current = connection.execute(
            "SELECT stored_name,source_size,state,format_version FROM audiobook_playback_jobs WHERE book_id=?",
            (book_id,),
        ).fetchone()
        if current and current["stored_name"] == stored_name and current["source_size"] == source_size \
                and current["format_version"] == FORMAT_VERSION and current["state"] in {"pending", "preparing", "ready"}:
            return current["state"]
        connection.execute(
            """INSERT INTO audiobook_playback_jobs
               (book_id,stored_name,source_size,state,format_version,available_at,updated_at,error_code)
               VALUES(?,?,?,'pending',?,?,?,NULL)
               ON CONFLICT(book_id) DO UPDATE SET
                 stored_name=excluded.stored_name,source_size=excluded.source_size,
                 state='pending',format_version=excluded.format_version,
                 available_at=excluded.available_at,updated_at=excluded.updated_at,
                 error_code=NULL,attempts=0""",
            (book_id, stored_name, source_size, FORMAT_VERSION, timestamp, timestamp),
        )
    return "pending"


def enqueue_library():
    count = 0
    for source in ORIGINALS.iterdir():
        if source.is_file() and not source.is_symlink():
            enqueue(source.stem, source.name, source.stat().st_size)
            count += 1
    return count


def playback_status(book_id):
    with queue_connection() as connection:
        row = connection.execute(
            "SELECT state,format_version,derivative_bytes,generated_at,error_code FROM audiobook_playback_jobs WHERE book_id=?",
            (book_id,),
        ).fetchone()
    if not row:
        return {"state": "pending", "mode": "range", "error_code": None}
    ready = row["state"] == "ready" and derivative_paths(book_id, require_ready=False) is not None
    return {
        "state": "ready" if ready else row["state"],
        "mode": "segmented" if ready else "range",
        "derivative_bytes": row["derivative_bytes"],
        "generated_at": row["generated_at"],
        "error_code": row["error_code"],
    }


def derivative_paths(book_id, require_ready=True):
    if not book_id or any(character not in "0123456789abcdef" for character in book_id.lower()):
        return None
    directory = STREAMING / book_id / f"v{FORMAT_VERSION}"
    playlist, media = directory / "index.m3u8", directory / "index.m4s"
    root = STREAMING.resolve()
    for path in (directory, playlist, media):
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            return None
    if not playlist.is_file() or not media.is_file() or not playlist.stat().st_size or not media.stat().st_size:
        return None
    if require_ready and playback_status(book_id)["state"] != "ready":
        return None
    return playlist, media


def _source_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_profile(source):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,channels", "-of", "json", str(source)],
        capture_output=True, text=True, timeout=45, check=True,
    )
    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams:
        raise ValueError("audio_stream_missing")
    return str(streams[0].get("codec_name") or ""), int(streams[0].get("channels") or 2)


def _validate_derivative(directory):
    playlist, media = directory / "index.m3u8", directory / "index.m4s"
    if not playlist.is_file() or not media.is_file() or media.stat().st_size < 1024:
        raise ValueError("derivative_incomplete")
    if playlist.stat().st_size > 8 * 1024**2:
        raise ValueError("playlist_oversized")
    text = playlist.read_text(encoding="utf-8")
    required = ("#EXTM3U", "#EXT-X-MAP", "#EXT-X-BYTERANGE", "#EXT-X-ENDLIST")
    if not all(value in text for value in required):
        raise ValueError("playlist_invalid")
    for line in text.splitlines():
        if line and not line.startswith("#") and line != "index.m4s":
            raise ValueError("playlist_external_uri")
    return playlist.stat().st_size + media.stat().st_size


def system_pause_reason():
    try:
        if shutil.disk_usage(ROOT).free < MIN_FREE_BYTES:
            return "low_storage"
    except OSError:
        return "storage_unavailable"
    try:
        thermal = Path("/sys/class/thermal/thermal_zone0/temp")
        if thermal.is_file() and float(thermal.read_text().strip()) / 1000 >= MAX_TEMP_C:
            return "temperature_high"
    except (OSError, ValueError):
        pass
    try:
        if os.getloadavg()[0] >= MAX_LOAD:
            return "load_high"
    except (AttributeError, OSError):
        pass
    return None


def claim_next_job():
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    timestamp = now()
    with queue_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE audiobook_playback_jobs SET state='pending',available_at=?,updated_at=?,error_code='worker_interrupted' "
            "WHERE state='preparing' AND updated_at<?",
            (timestamp, timestamp, stale),
        )
        row = connection.execute(
            """SELECT * FROM audiobook_playback_jobs
               WHERE state='pending' AND available_at<=?
               ORDER BY source_size ASC, updated_at ASC LIMIT 1""",
            (timestamp,),
        ).fetchone()
        if not row:
            return None
        connection.execute(
            "UPDATE audiobook_playback_jobs SET state='preparing',attempts=attempts+1,updated_at=?,error_code=NULL WHERE book_id=?",
            (timestamp, row["book_id"]),
        )
        return dict(row)


def recover_interrupted_worker():
    """Recover queue state after the sole preparation worker is restarted.

    This is intentionally called only by the long-running worker at process
    startup.  The portal imports this module too, so recovery must never run
    as an import side effect or from an ad-hoc ``--once`` process while the
    real worker is active.
    """
    for path in STAGING.iterdir():
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            # A later preparation attempt will use a fresh UUID directory;
            # an unremovable stale directory is therefore not authoritative.
            pass
    timestamp = now()
    with queue_connection() as connection:
        result = connection.execute(
            """UPDATE audiobook_playback_jobs
               SET state='pending',available_at=?,updated_at=?,error_code='worker_restarted'
               WHERE state='preparing'""",
            (timestamp, timestamp),
        )
    return int(result.rowcount or 0)


def prepare_job(job):
    book_id, stored_name = job["book_id"], job["stored_name"]
    temporary = None
    try:
        # Source validation, hashing and probing are part of the job. Keep
        # them inside the guarded section so a missing or malformed original
        # cannot leave the durable queue row stuck in ``preparing``.
        source = _safe_source(stored_name)
        source_stat = source.stat()
        source_hash = _source_sha256(source)
        codec, channels = _audio_profile(source)
        temporary = STAGING / f"{book_id}-{uuid.uuid4().hex}"
        temporary.mkdir(mode=0o750, parents=True)
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:a:0", "-vn"]
        if codec == "aac":
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "96k" if channels == 1 else "128k"]
        command += [
            "-f", "hls", "-hls_segment_type", "fmp4", "-hls_time", str(SEGMENT_SECONDS),
            "-hls_list_size", "0", "-hls_flags", "independent_segments+single_file",
            "-hls_segment_filename", str(temporary / "index.m4s"),
            str(temporary / "index.m3u8"),
        ]
        subprocess.run(command, capture_output=True, timeout=6 * 60 * 60, check=True)
        derivative_bytes = _validate_derivative(temporary)
        if source.stat().st_size != source_stat.st_size or _source_sha256(source) != source_hash:
            raise ValueError("source_changed_during_prepare")
        for path in temporary.iterdir():
            path.chmod(0o640)
        final_parent = STREAMING / book_id
        final_parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        final = final_parent / f"v{FORMAT_VERSION}"
        previous = final_parent / f".previous-{uuid.uuid4().hex}"
        if final.exists():
            final.replace(previous)
        temporary.replace(final)
        shutil.rmtree(previous, ignore_errors=True)
        with queue_connection() as connection:
            connection.execute(
                """UPDATE audiobook_playback_jobs SET state='ready',source_size=?,source_sha256=?,
                   derivative_bytes=?,generated_at=?,updated_at=?,error_code=NULL WHERE book_id=?""",
                (source_stat.st_size, source_hash, derivative_bytes, now(), now(), book_id),
            )
        return derivative_bytes
    except Exception as error:
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        code = str(error) if isinstance(error, ValueError) else "prepare_failed"
        with queue_connection() as connection:
            row = connection.execute("SELECT attempts FROM audiobook_playback_jobs WHERE book_id=?", (book_id,)).fetchone()
            attempts = int(row["attempts"] if row else 1)
            state = "failed" if attempts >= 3 else "pending"
            delay = datetime.now(timezone.utc) + timedelta(minutes=min(30, attempts * 5))
            connection.execute(
                "UPDATE audiobook_playback_jobs SET state=?,available_at=?,updated_at=?,error_code=? WHERE book_id=?",
                (state, delay.isoformat(), now(), code[:64], book_id),
            )
        raise


def work_once():
    reason = system_pause_reason()
    if reason:
        return {"state": "paused", "reason": reason}
    enqueue_library()
    job = claim_next_job()
    if not job:
        return {"state": "idle"}
    try:
        size = prepare_job(job)
        return {"state": "ready", "book_id": job["book_id"], "bytes": size}
    except Exception:
        return {"state": "failed", "book_id": job["book_id"]}


def run_forever():
    while True:
        result = work_once()
        time.sleep(2 if result["state"] in {"ready", "failed"} else 30)
