"""Bounded Files background work; HTTP requests never wait for a PDF renderer."""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

LOGGER = logging.getLogger(__name__)
PDF_QUEUE_LIMIT = 32
PDF_PREFETCH_LIMIT = 8
PDF_PREFETCH_SECONDS = 15
PDF_VISIBLE_SECONDS = 180
_start_lock = threading.Lock()
_started_pid = None


def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS file_upload_batches (
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, owner_role TEXT NOT NULL,
        owner_name TEXT NOT NULL, request_digest TEXT NOT NULL, staged_json TEXT NOT NULL,
        folder_id TEXT, visibility TEXT NOT NULL, state TEXT NOT NULL,
        added_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    connection.execute("CREATE INDEX IF NOT EXISTS file_upload_batches_pending_idx ON file_upload_batches(state,next_attempt)")
    connection.execute("CREATE INDEX IF NOT EXISTS file_upload_batches_owner_idx ON file_upload_batches(owner_id,created_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS file_pdf_jobs (
        key TEXT PRIMARY KEY, file_id TEXT NOT NULL, sha256 TEXT NOT NULL,
        page INTEGER NOT NULL, width INTEGER NOT NULL, state TEXT NOT NULL,
        priority INTEGER NOT NULL, wanted_at REAL NOT NULL, updated_at REAL NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', pages INTEGER)""")
    connection.execute("CREATE INDEX IF NOT EXISTS file_pdf_jobs_pending_idx ON file_pdf_jobs(state,priority,wanted_at)")
    connection.execute("CREATE TABLE IF NOT EXISTS file_job_state (key TEXT PRIMARY KEY, value REAL NOT NULL)")


@contextmanager
def connection():
    from . import files
    db = sqlite3.connect(files.DB_PATH, timeout=0.15)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        except BaseException:
            try:
                db.rollback()
            except Exception:
                LOGGER.exception("Files job rollback failed while preserving an earlier error")
            raise
        else:
            try:
                db.commit()
            except BaseException:
                try:
                    db.rollback()
                except Exception:
                    LOGGER.exception("Files job rollback failed after commit failure")
                raise
    finally:
        try:
            db.close()
        except Exception:
            # A close error cannot undo a successful commit. In particular it
            # must not turn a durable receipt into a reported failed upload.
            LOGGER.exception("Files job connection close failed after transaction outcome was decided")


@contextmanager
def worker_lock(name):
    """A nonblocking OS lock spans the whole operation, across worker processes."""
    import fcntl
    from . import files
    descriptor = -1
    locked = False
    try:
        try:
            descriptor, _ = files.INCOMING_STORAGE.create_regular(f".files-{name}.lock", mode=0o600)
        except FileExistsError:
            descriptor, _ = files.INCOMING_STORAGE.open_regular(f".files-{name}.lock", expected_size=0)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            pass
        yield locked
    finally:
        if descriptor >= 0:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def upload_id(owner, key):
    return hashlib.sha256((owner + "\0" + key).encode()).hexdigest()


def upload_status(batch_id, owner):
    with connection() as db:
        row = db.execute("SELECT * FROM file_upload_batches WHERE id=? AND owner_id=?", (batch_id, owner)).fetchone()
    if not row:
        return None
    return {"upload_id": row["id"], "state": row["state"], "queued": row["state"] in {"queued", "processing"},
            "added": json.loads(row["added_json"]), "error": row["error"],
            "status_url": f"/api/files/uploads/{row['id']}", "retry_after": 3}


def register_upload(batch_id, actor, owner_name, staged, folder_id, visibility):
    manifest = [{key: value for key, value in item.items() if key != "identity"} for item in staged]
    definition = {"files": [{key: item[key] for key in ("display_name", "size", "sha256", "mimetype")} for item in staged],
                  "folder_id": folder_id, "visibility": visibility}
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM file_upload_batches WHERE id=?", (batch_id,)).fetchone()
        if existing:
            if existing["owner_id"] != actor.principal_id or existing["request_digest"] != digest:
                raise ValueError("This upload retry belongs to different files or sharing settings.")
            return False
        timestamp = time.time()
        db.execute("""INSERT INTO file_upload_batches
            (id,owner_id,owner_role,owner_name,request_digest,staged_json,folder_id,visibility,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'queued',?,?)""",
            (batch_id, actor.principal_id, actor.role, owner_name, digest, json.dumps(manifest), folder_id, visibility, timestamp, timestamp))
    return True


def retry_upload(batch_id, owner):
    with connection() as db:
        db.execute("UPDATE file_upload_batches SET state='queued',error='',attempts=0,next_attempt=0 WHERE id=? AND owner_id=? AND state='failed'", (batch_id, owner))
    return upload_status(batch_id, owner)


def process_upload_once(batch_id=None):
    from . import files
    from .content_policy import Actor
    with worker_lock("uploads") as locked:
        if not locked:
            return False
        with connection() as db:
            # Holding the process lock proves a prior 'processing' owner is gone.
            db.execute("UPDATE file_upload_batches SET state='queued' WHERE state='processing'")
            row = db.execute("SELECT * FROM file_upload_batches WHERE state='queued' AND next_attempt<=?"
                             + (" AND id=?" if batch_id else "") + " ORDER BY created_at LIMIT 1",
                             (time.time(), batch_id) if batch_id else (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE file_upload_batches SET state='processing',attempts=attempts+1,updated_at=? WHERE id=?", (time.time(), row["id"]))
        if not row:
            # Legacy durable intents predate upload receipts; recover a bounded
            # batch too, without needing an application restart.
            if batch_id is None:
                with connection() as db:
                    pending = db.execute("""SELECT batch_id FROM file_upload_intents AS intent
                        WHERE state='prepared' AND NOT EXISTS
                        (SELECT 1 FROM file_upload_batches AS batch WHERE batch.id=intent.batch_id)
                        ORDER BY created_at LIMIT 1""").fetchone()
                if pending:
                    try:
                        files._recover_upload_batch(pending["batch_id"])
                    except (OSError, ValueError, sqlite3.Error):
                        LOGGER.warning("Legacy Files upload is awaiting another recovery pass")
            return False
        try:
            if not files._intent_rows(row["id"]):
                files._prepare_upload_batch(json.loads(row["staged_json"]), Actor(row["owner_id"], role=row["owner_role"]),
                                            row["owner_name"], row["folder_id"], row["visibility"], row["id"])
            added = files._recover_upload_batch(row["id"])
            with connection() as db:
                db.execute("UPDATE file_upload_batches SET state='completed',added_json=?,error='',updated_at=? WHERE id=?",
                           (json.dumps(added), time.time(), row["id"]))
        except (OSError, ValueError, sqlite3.Error) as error:
            attempts = int(row["attempts"]) + 1
            permanent = isinstance(error, files.FolderAccessError) or attempts >= 8
            message = ("This upload needs another attempt. Its staged files are retained." if permanent else
                       "Files are safely queued. Finishing will retry automatically.")
            with connection() as db:
                db.execute("UPDATE file_upload_batches SET state=?,error=?,next_attempt=?,updated_at=? WHERE id=?",
                           ("failed" if permanent else "queued", message, time.time() + min(120, 3 * 2 ** attempts), time.time(), row["id"]))
            LOGGER.warning("Files upload completion deferred (%s)", type(error).__name__)
        return True


def pdf_key(row, page, width):
    return f"{row['id']}:{row['sha256']}:{page}:{width}"


def cached_pages(row):
    from . import files
    if row["id"] in files.pdf_page_counts:
        return files.pdf_page_counts[row["id"]]
    with connection() as db:
        item = db.execute("SELECT pages FROM file_pdf_jobs WHERE file_id=? AND sha256=? AND pages IS NOT NULL ORDER BY updated_at DESC LIMIT 1",
                          (row["id"], row["sha256"])).fetchone()
    return item[0] if item else None


def request_pdf(row, page=0, width=0, *, prefetch=False, retry=False):
    timestamp = time.time()
    key = pdf_key(row, page, width)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM file_pdf_jobs WHERE state='queued' AND wanted_at<? AND priority=1", (timestamp - PDF_PREFETCH_SECONDS,))
        db.execute("DELETE FROM file_pdf_jobs WHERE state='queued' AND wanted_at<? AND priority=0", (timestamp - PDF_VISIBLE_SECONDS,))
        existing = db.execute("SELECT * FROM file_pdf_jobs WHERE key=?", (key,)).fetchone()
        if existing and existing["state"] == "failed" and not retry:
            return dict(existing)
        if existing and existing["state"] in {"queued", "processing"}:
            db.execute("UPDATE file_pdf_jobs SET wanted_at=?,priority=MIN(priority,?) WHERE key=?", (timestamp, int(prefetch), key))
            return dict(existing)
        if not prefetch:
            # Adjacent pages are speculative. Do not make a new visible page
            # wait behind stale speculation for this document.
            db.execute("DELETE FROM file_pdf_jobs WHERE file_id=? AND state='queued' AND priority=1 AND key!=?", (row["id"], key))
        counts = db.execute("SELECT COUNT(*),COALESCE(SUM(priority),0) FROM file_pdf_jobs WHERE state IN ('queued','processing')").fetchone()
        if not prefetch and counts[0] >= PDF_QUEUE_LIMIT:
            evicted = db.execute("DELETE FROM file_pdf_jobs WHERE key IN (SELECT key FROM file_pdf_jobs WHERE state='queued' AND priority=1 ORDER BY wanted_at LIMIT 1)").rowcount
            if evicted:
                counts = (counts[0] - evicted, counts[1] - evicted)
        if counts[0] >= PDF_QUEUE_LIMIT or (prefetch and counts[1] >= PDF_PREFETCH_LIMIT):
            return {"state": "busy", "error": "Preview queue is busy. Please try again shortly."}
        db.execute("""INSERT INTO file_pdf_jobs(key,file_id,sha256,page,width,state,priority,wanted_at,updated_at)
            VALUES(?,?,?,?,?,'queued',?,?,?) ON CONFLICT(key) DO UPDATE SET
            state='queued',priority=excluded.priority,wanted_at=excluded.wanted_at,updated_at=excluded.updated_at,error=''
            """, (key, row["id"], row["sha256"], page, width, int(prefetch), timestamp, timestamp))
    return {"state": "queued", "error": ""}


def _maybe_trim(rendered_bytes):
    from . import files
    timestamp = time.time()
    with connection() as db:
        values = dict(db.execute("SELECT key,value FROM file_job_state"))
        accrued = values.get("pdf_trim_bytes", 0) + rendered_bytes
        trim = accrued >= min(32 * 1024 * 1024, max(1, files.PDF_CACHE_BYTES // 10)) or timestamp - values.get("pdf_trim_time", 0) > 600
    if trim:
        files.trim_pdf_cache()
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO file_job_state VALUES('pdf_trim_bytes',?)", (0 if trim else accrued,))
        if trim:
            db.execute("INSERT OR REPLACE INTO file_job_state VALUES('pdf_trim_time',?)", (timestamp,))


def process_pdf_once():
    from . import files
    with worker_lock("pdf") as locked:
        if not locked:
            return False
        timestamp = time.time()
        with connection() as db:
            db.execute("UPDATE file_pdf_jobs SET state='queued' WHERE state='processing'")
            db.execute("DELETE FROM file_pdf_jobs WHERE state='queued' AND wanted_at<? AND priority=1", (timestamp - PDF_PREFETCH_SECONDS,))
            db.execute("DELETE FROM file_pdf_jobs WHERE state='queued' AND wanted_at<? AND priority=0", (timestamp - PDF_VISIBLE_SECONDS,))
            job = db.execute("SELECT * FROM file_pdf_jobs WHERE state='queued' ORDER BY priority,wanted_at DESC,key LIMIT 1").fetchone()
            if not job:
                return False
            db.execute("UPDATE file_pdf_jobs SET state='processing',attempts=attempts+1,updated_at=? WHERE key=?", (timestamp, job["key"]))
            row = db.execute("SELECT * FROM stored_files WHERE id=? AND sha256=? AND deleted_at IS NULL", (job["file_id"], job["sha256"])).fetchone()
        try:
            if row is None:
                raise ValueError("file_unavailable")
            pages = cached_pages(row) or files.pdf_page_count(row)
            if job["page"]:
                if not 1 <= job["page"] <= pages:
                    raise ValueError("page_unavailable")
                cache_name = f"{row['id']}-{job['page']}-{job['width']}.jpg"
                try:
                    descriptor, metadata = files._open_pdf_cache(cache_name)
                    os.close(descriptor)
                except FileNotFoundError:
                    files._render_pdf_cache(row, job["page"], job["width"], cache_name)
                    descriptor, metadata = files._open_pdf_cache(cache_name)
                    os.close(descriptor)
                    _maybe_trim(metadata.st_size)
            with connection() as db:
                db.execute("UPDATE file_pdf_jobs SET state='ready',pages=?,error='',updated_at=? WHERE key=?", (pages, time.time(), job["key"]))
                db.execute("DELETE FROM file_pdf_jobs WHERE key IN (SELECT key FROM file_pdf_jobs WHERE state IN ('ready','failed') ORDER BY updated_at DESC LIMIT -1 OFFSET 512)")
        except Exception as error:
            with connection() as db:
                db.execute("UPDATE file_pdf_jobs SET state='failed',error=?,updated_at=? WHERE key=?",
                           ("This preview could not be prepared. Try again or download the original.", time.time(), job["key"]))
            LOGGER.warning("Files PDF preview failed (%s)", type(error).__name__)
        return True


def start_workers():
    global _started_pid
    with _start_lock:
        if _started_pid == os.getpid():
            return
        _started_pid = os.getpid()
        def loop(operation, idle):
            while True:
                try:
                    active = operation()
                except Exception:
                    LOGGER.exception("Files background pass deferred")
                    active = False
                time.sleep(0.25 if active else idle)
        threading.Thread(target=loop, args=(process_pdf_once, 1), name="files-pdf", daemon=True).start()
        threading.Thread(target=loop, args=(process_upload_once, 5), name="files-recovery", daemon=True).start()
