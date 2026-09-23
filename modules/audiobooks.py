"""Private DRM-free audiobook library for David-Pi."""
import ctypes, errno, fcntl, hashlib, json, math, os, shutil, sqlite3, stat, subprocess, sys, threading, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename
from .identity import current_device, require_profile
from .platform import PLATFORM_DATA, connect, migrate, utcnow
from .audiobook_streaming import (
    activate_reserved_import, begin_abort_import, commit_reserved_import,
    DERIVATIVE_STATE, derivative_paths, enqueue, finish_abort_import, finish_import,
    import_reservation, mark_import_published, pending_import_reservations,
    playback_status, playback_statuses, reconcile_catalog, reserve_import,
    claim_import_recovery,
    reactivate_aborting_import,
    suspend,
)

bp = Blueprint("audiobooks", __name__)
DB_PATH = PLATFORM_DATA / "audiobooks.db"
ROOT = Path(os.environ.get("DAVID_PI_AUDIOBOOKS_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "audiobooks"))
ORIGINALS, COVERS, INCOMING, TRASH, PLAYBACK = (ROOT / name for name in ("originals", "covers", "incoming", "trash", "playback"))
ALLOWED = {".mp3", ".m4b", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}
MIMES = {".mp3":"audio/mpeg", ".m4b":"audio/mp4", ".m4a":"audio/mp4", ".aac":"audio/aac", ".ogg":"audio/ogg", ".opus":"audio/ogg", ".flac":"audio/flac", ".wav":"audio/wav"}
MAX_BYTES = int(os.environ.get("DAVID_PI_MAX_AUDIOBOOK_BYTES", 4 * 1024**3))
RESERVE = int(os.environ.get("DAVID_PI_AUDIOBOOK_RESERVE", 2 * 1024**3))
IMPORT_RECOVERY_INTERVAL_SECONDS = max(30, int(os.environ.get(
    "DAVID_PI_AUDIOBOOK_IMPORT_RECOVERY_INTERVAL_SECONDS", "60"
)))
IMPORT_RECOVERY_POLL_SECONDS = max(5, min(30, IMPORT_RECOVERY_INTERVAL_SECONDS // 4))
IMPORT_RECOVERY_LOCK = DERIVATIVE_STATE / "import-recovery.lock"
for directory in (ROOT, ORIGINALS, COVERS, INCOMING, TRASH, PLAYBACK): directory.mkdir(parents=True, exist_ok=True)
_next_import_recovery_poll = 0.0
_import_recovery_poll_lock = threading.Lock()

def initialize(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS audiobooks(
      id TEXT PRIMARY KEY,title TEXT NOT NULL,author TEXT NOT NULL DEFAULT '',narrator TEXT NOT NULL DEFAULT '',series TEXT NOT NULL DEFAULT '',
      original_name TEXT NOT NULL,stored_name TEXT NOT NULL UNIQUE,cover_name TEXT,content_type TEXT NOT NULL,byte_size INTEGER NOT NULL,
      sha256 TEXT NOT NULL,duration_seconds REAL NOT NULL DEFAULT 0,chapters_json TEXT NOT NULL DEFAULT '[]',owner_id TEXT,owner_name TEXT,
      visibility TEXT NOT NULL DEFAULT 'shared',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT)""")
    connection.execute("CREATE INDEX IF NOT EXISTS audiobooks_library_idx ON audiobooks(deleted_at,updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS audiobooks_owner_idx ON audiobooks(visibility,owner_id)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS audiobooks_owner_hash_idx ON audiobooks(owner_id,sha256) WHERE deleted_at IS NULL")
    connection.execute("""CREATE TABLE IF NOT EXISTS audiobook_progress(
      book_id TEXT NOT NULL REFERENCES audiobooks(id) ON DELETE CASCADE,owner_id TEXT NOT NULL,position_seconds REAL NOT NULL DEFAULT 0,
      completed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(book_id,owner_id))""")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(audiobook_progress)")}
    for name, definition in (
        ("revision", "INTEGER NOT NULL DEFAULT 0"),
        ("session_id", "TEXT NOT NULL DEFAULT ''"),
        ("sequence", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE audiobook_progress ADD COLUMN {name} {definition}")
migrate(DB_PATH, initialize)

def clean(value, limit=180): return " ".join(str(value or "").replace("\x00", "").split())[:limit]


class AudiobookDurationError(ValueError):
    """Raised when external or stored audiobook timing is not finite."""


def finite_duration(value, *, require_positive=False):
    try:
        duration = float(value or 0)
    except (TypeError, ValueError, OverflowError) as error:
        raise AudiobookDurationError("Audiobook duration is invalid.") from error
    if not math.isfinite(duration) or duration < 0 or (require_positive and duration <= 0):
        raise AudiobookDurationError("Audiobook duration is invalid.")
    return duration


def visible(identity): return ("(visibility='shared' OR owner_id=?)", [identity["owner_id"]]) if identity["owner_id"] else ("visibility='shared'", [])
def managed(root, name):
    root = root.resolve(); candidate = root / str(name)
    if candidate.is_symlink(): raise ValueError("Symbolic links are not accepted.")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents: raise ValueError("Audiobook path escaped storage.")
    return resolved

def probe(path):
    try:
        result = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration:format_tags=title,artist,album_artist,composer,album","-show_chapters","-of","json",str(path)], capture_output=True,text=True,timeout=30,check=True)
        payload=json.loads(result.stdout or "{}")
        if not isinstance(payload,dict) or not isinstance(payload.get("format",{}),dict): raise ValueError("invalid probe")
        tags=payload.get("format",{}).get("tags",{}) or {}; duration=finite_duration(payload.get("format",{}).get("duration"),require_positive=True)
        if not isinstance(tags,dict): tags={}
        chapters=[]
        for chapter in (payload.get("chapters") or [])[:500]:
            if not isinstance(chapter,dict): continue
            start=finite_duration(chapter.get("start_time")); end=finite_duration(chapter.get("end_time") or start)
            if end < start: continue
            chapter_tags=chapter.get("tags") or {}
            if not isinstance(chapter_tags,dict): chapter_tags={}
            chapters.append({"title":clean(chapter_tags.get("title") or f"Chapter {len(chapters)+1}",120),"start":start,"end":end})
        return tags,duration,chapters
    except AudiobookDurationError:
        # A non-finite value must not be normalized into a catalog row. The
        # upload caller removes its staging file and commits no library/queue
        # writes; stored corruption likewise remains excluded from the queue.
        raise
    except (OSError,subprocess.SubprocessError,json.JSONDecodeError,TypeError,ValueError,OverflowError): return {},0.0,[]

def cover(source, destination):
    descriptor = None
    identity = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        details = os.fstat(descriptor)
        identity = (str(details.st_dev), str(details.st_ino))
        subprocess.run(
            ["ffmpeg","-nostdin","-v","error","-i",str(source),"-map","0:v:0","-frames:v","1","-vf","scale=600:600:force_original_aspect_ratio=decrease","-f","image2pipe",f"pipe:{descriptor}"],
            capture_output=True,timeout=45,check=True,pass_fds=(descriptor,),
        )
        os.fsync(descriptor)
        output_size = os.fstat(descriptor).st_size
        os.close(descriptor); descriptor = None
        if _identity_matches(destination,*identity) is True and output_size:
            return destination.name
    except (OSError,subprocess.SubprocessError): pass
    finally:
        if descriptor is not None:
            try: os.close(descriptor)
            except OSError: pass
    if identity is not None:
        _unlink_matching(destination, *identity)
    return None


class DuplicateAudiobookError(ValueError):
    """A concurrent import committed the same owner/content pair first."""


def _file_identity(path):
    details = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(details.st_mode):
        raise ValueError("Audiobook staging is not a regular file.")
    return str(details.st_dev), str(details.st_ino)


def _identity_matches(path, device, inode):
    try:
        details = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and str(details.st_dev) == str(device)
        and str(details.st_ino) == str(inode)
    )


_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2_FUNCTION = getattr(_LIBC, "renameat2", None)
_SYSCALL_FUNCTION = getattr(_LIBC, "syscall", None)
# renameat2 is a Linux kernel interface, but musl intentionally does not expose
# a libc wrapper for it.  Production is ARM64; x86_64 is retained so the exact
# fail-closed fallback can be exercised by development and CI hosts.
_RENAMEAT2_SYSCALL = (
    {
        "aarch64": 276,
        "arm64": 276,
        "x86_64": 316,
        "amd64": 316,
    }.get(os.uname().machine.lower())
    if sys.platform.startswith("linux")
    else None
)
if _RENAMEAT2_FUNCTION is not None:
    _RENAMEAT2_FUNCTION.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2_FUNCTION.restype = ctypes.c_int
if _SYSCALL_FUNCTION is not None:
    # syscall(2) is variadic. Declare its fixed ABI and pass every variadic
    # numeric argument at machine-word width below; libc's default c_int
    # return type is incorrect on 64-bit Linux.
    _SYSCALL_FUNCTION.argtypes = [ctypes.c_long]
    _SYSCALL_FUNCTION.restype = ctypes.c_long


def _rename_noreplace_at(source_directory, source_name, target_directory, target_name):
    """Linux renameat2(NOREPLACE), used as the cleanup namespace boundary."""
    source_bytes = os.fsencode(source_name)
    target_bytes = os.fsencode(target_name)
    if _RENAMEAT2_FUNCTION is not None:
        result = _RENAMEAT2_FUNCTION(
            source_directory,
            source_bytes,
            target_directory,
            target_bytes,
            _RENAME_NOREPLACE,
        )
    elif _SYSCALL_FUNCTION is not None and _RENAMEAT2_SYSCALL is not None:
        result = _SYSCALL_FUNCTION(
            ctypes.c_long(_RENAMEAT2_SYSCALL),
            ctypes.c_long(source_directory),
            ctypes.c_char_p(source_bytes),
            ctypes.c_long(target_directory),
            ctypes.c_char_p(target_bytes),
            ctypes.c_ulong(_RENAME_NOREPLACE),
        )
    else:
        raise OSError(errno.ENOSYS, "renameat2 is required for safe audiobook cleanup")
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), source_name)


def _unlink_matching(path, device, inode):
    """Remove only the recorded inode through a pinned, private namespace.

    A public-name lstat followed by unlink is inherently racy.  Instead this
    opens and verifies the parent directory, atomically moves whatever is at
    the public name into a private recovery directory without replacement, then
    validates the moved inode.  A replacement is restored (or retained in the
    quarantine if its public name was concurrently occupied) and is never
    unlinked.  Parent-path rebinding cannot redirect any operation after the
    directory descriptor has been opened.
    """
    path = Path(path)
    name = path.name
    if not name or name in {".", ".."} or path.parent == path:
        return False
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_fd = quarantine_fd = None
    quarantine_name = None
    quarantine_verified = False
    moved = False
    outcome = False
    try:
        try:
            parent_fd = os.open(path.parent, directory_flags)
        except FileNotFoundError:
            return True
        parent_details = os.fstat(parent_fd)
        try:
            bound_details = os.stat(path.parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISDIR(parent_details.st_mode)
            or not stat.S_ISDIR(bound_details.st_mode)
            or (parent_details.st_dev, parent_details.st_ino)
                != (bound_details.st_dev, bound_details.st_ino)
        ):
            return False
        # The private name is deterministic for this inode, so a crash after
        # the atomic move but before unlink is itself recoverable on retry.
        # The digest keeps the on-disk name bounded and prevents path syntax
        # from ever being derived directly from journal metadata.
        identity_key = hashlib.sha256(
            f"{device}:{inode}".encode("ascii", "strict")
        ).hexdigest()
        quarantine_name = f".audiobook-cleanup-{identity_key}"
        try:
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        quarantine_fd = os.open(quarantine_name, directory_flags, dir_fd=parent_fd)
        quarantine_details = os.fstat(quarantine_fd)
        quarantine_binding = os.stat(
            quarantine_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(quarantine_details.st_mode)
            or (quarantine_details.st_dev, quarantine_details.st_ino)
                != (quarantine_binding.st_dev, quarantine_binding.st_ino)
            or quarantine_details.st_uid != os.geteuid()
            or stat.S_IMODE(quarantine_details.st_mode) & 0o077
        ):
            return False
        quarantine_verified = True
        try:
            os.stat("held", dir_fd=quarantine_fd, follow_symlinks=False)
            moved = True
        except FileNotFoundError:
            try:
                _rename_noreplace_at(parent_fd, name, quarantine_fd, "held")
                moved = True
            except FileNotFoundError:
                outcome = True
        if moved:
            details = os.stat("held", dir_fd=quarantine_fd, follow_symlinks=False)
            matches = (
                stat.S_ISREG(details.st_mode)
                and str(details.st_dev) == str(device)
                and str(details.st_ino) == str(inode)
            )
            if not matches:
                try:
                    _rename_noreplace_at(quarantine_fd, "held", parent_fd, name)
                    moved = False
                except OSError:
                    # Fail closed.  The replacement remains intact in the
                    # private directory rather than being deleted or used as
                    # proof that this request's inode was cleaned.
                    pass
                outcome = False
            else:
                # Only the now-private name can be removed.  A concurrent
                # replacement installed at the public name is unaffected.
                os.unlink("held", dir_fd=quarantine_fd)
                moved = False
                os.fsync(quarantine_fd)
                os.fsync(parent_fd)
                outcome = True
    except (OSError, ValueError):
        outcome = False
    finally:
        if quarantine_fd is not None:
            try: os.close(quarantine_fd)
            except OSError: outcome = False
        if quarantine_verified and quarantine_name is not None and parent_fd is not None and not moved:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                outcome = False
        if parent_fd is not None:
            try: os.close(parent_fd)
            except OSError: outcome = False
    return outcome


def _reservation_path(root, name):
    name = str(name or "")
    if not name or Path(name).name != name or name in {".", ".."}:
        return None
    return root / name


def _cleanup_reservation_files(reservation, *, remove_published):
    targets = [
        (INCOMING, reservation.get("staging_name"), reservation.get("source_device"), reservation.get("source_inode")),
        (INCOMING, reservation.get("cover_staging_name"), reservation.get("cover_device"), reservation.get("cover_inode")),
    ]
    if remove_published:
        targets.extend([
            (ORIGINALS, reservation.get("stored_name"), reservation.get("source_device"), reservation.get("source_inode")),
            (COVERS, reservation.get("cover_name"), reservation.get("cover_device"), reservation.get("cover_inode")),
        ])
    complete = True
    for root, name, device, inode in targets:
        if name is None:
            continue
        path = _reservation_path(root, name)
        if path is None or device is None or inode is None:
            complete = False
            continue
        complete = _unlink_matching(path, device, inode) and complete
    return complete


def _exclusive_upload_staging():
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _attempt in range(32):
        upload_token = uuid.uuid4().hex
        path = managed(INCOMING, f"{upload_token}.upload.part")
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            continue
        details = os.fstat(descriptor)
        return upload_token, path, descriptor, (str(details.st_dev), str(details.st_ino))
    raise OSError("Could not allocate an exclusive audiobook staging file.")


def _publish_noreplace(source, destination, device, inode):
    if _identity_matches(source, device, inode) is not True:
        raise ValueError("Audiobook staging changed during import.")
    os.link(source, destination, follow_symlinks=False)
    if _identity_matches(destination, device, inode) is not True:
        raise ValueError("Audiobook publication identity changed.")


def _library_row_matches_import(row, reservation):
    if not row:
        return False
    return (
        row["stored_name"] == reservation["stored_name"]
        and row["byte_size"] == reservation["source_size"]
        and row["sha256"] == reservation["source_sha256"]
        and row["duration_seconds"] == reservation["duration_seconds"]
        and row["cover_name"] == reservation["cover_name"]
    )


def _final_files_match_import(reservation):
    source = _reservation_path(ORIGINALS, reservation.get("stored_name"))
    if source is None or _identity_matches(
        source, reservation.get("source_device"), reservation.get("source_inode")
    ) is not True:
        return False
    if reservation.get("cover_name") is not None:
        cover_path = _reservation_path(COVERS, reservation.get("cover_name"))
        if cover_path is None or _identity_matches(
            cover_path, reservation.get("cover_device"), reservation.get("cover_inode")
        ) is not True:
            return False
    return True


def _abort_reserved_files(reservation):
    try:
        aborting = begin_abort_import(
            reservation["reservation_token"], reservation["book_id"]
        )
    except (OSError, sqlite3.Error, ValueError):
        current_app.logger.exception("Could not safely deactivate an incomplete audiobook import")
        return False
    if aborting is None:
        return False
    cleaned = _cleanup_reservation_files(aborting, remove_published=True)
    if cleaned:
        try:
            finish_abort_import(aborting["reservation_token"], aborting["book_id"])
        except (OSError, sqlite3.Error, ValueError):
            current_app.logger.exception("Audiobook cleanup journal will be retried")
            return False
    return cleaned


def _import_error_reason(error):
    if isinstance(error, AudiobookDurationError):
        return str(error)
    if isinstance(error, ValueError):
        mapped = {
            "forecast_overflow": "This audiobook has invalid or unsupported timing metadata.",
            "forecast_low_storage": "David-Pi needs more free space before importing this audiobook.",
            "import_id_conflict": "The import could not reserve a unique storage identity. Try again.",
        }.get(str(error))
        if mapped:
            return mapped
        message = str(error)
        if message.startswith(("David-Pi needs", "This audiobook exceeds", "The selected audiobook is empty")):
            return message
    return "This audiobook could not be imported safely."

def progress_scope(identity):
    """Return a stable local namespace, never an authorization credential.

    Progress markers need to survive CSRF-cookie rotation. The resulting digest
    is used only to partition browser-local records; APIs still authenticate
    every write from the current Tailscale identity.
    """
    owner_id = str(identity.get("owner_id") or "").strip().casefold()
    if not owner_id:
        return ""
    return hashlib.sha256(f"david-pi:audiobook-progress:v3\0{owner_id}".encode()).hexdigest()[:32]


def find_book(book_id, deleted_owner_only=False):
    identity=current_device()
    if deleted_owner_only:
        clause,args="deleted_at IS NOT NULL AND owner_id=?",[identity["owner_id"] or ""]
    else:
        visible_clause,args=visible(identity);clause=f"deleted_at IS NULL AND {visible_clause}"
    with connect(DB_PATH) as connection:
        return connection.execute(f"SELECT * FROM audiobooks WHERE id=? AND {clause}",(book_id,*args)).fetchone()

def serialize(row, identity, progress=None, playback=None, compact=False):
    source=dict(row);book_id=source["id"]
    item={key:source.get(key) for key in (
        "id","title","author","narrator","series","content_type","byte_size",
        "duration_seconds","visibility","sha256",
    )}
    try: item["duration_seconds"]=finite_duration(item.get("duration_seconds"))
    except ValueError: item["duration_seconds"]=0.0
    item["is_mine"]=bool(identity["owner_id"] and source.get("owner_id")==identity["owner_id"])
    item["stream_url"]=f"/api/audiobooks/{book_id}/stream"
    item["download_url"]=f"/api/audiobooks/{book_id}/download"
    item["cover_url"]=f"/api/audiobooks/{book_id}/cover" if source.get("cover_name") else None
    playback=playback or playback_status(book_id); item["playback_state"]=playback["state"]; item["playback_mode"]=playback["mode"]
    item["hls_url"]=f"/api/audiobooks/{book_id}/hls/index.m3u8" if playback["mode"]=="segmented" else None
    item["position_seconds"]=(progress or {}).get("position_seconds",0); item["completed"]=bool((progress or {}).get("completed",0)); item["progress_updated_at"]=(progress or {}).get("updated_at")
    item["progress_revision"] = int((progress or {}).get("revision", 0))
    item["progress_session"] = (progress or {}).get("session_id", "")
    item["progress_sequence"] = int((progress or {}).get("sequence", 0))
    if not compact:
        item["chapters"]=json.loads(source.get("chapters_json") or "[]")
    return item

def ranged_file(path, **options):
    """Serve a private file with an explicit, resumable byte-range contract."""
    response=send_file(path,conditional=True,etag=True,**options)
    response.headers["Accept-Ranges"]="bytes"
    response.headers["Vary"]="Cookie"
    return response

@bp.get("/audiobooks")
@require_profile(api=False)
def page():
    return render_template("audiobooks.html", audiobook_progress_scope=progress_scope(current_device()))

@bp.get("/api/audiobooks")
@require_profile()
def listing():
    identity=current_device(); query=clean(request.args.get("q"),120).casefold(); deleted=request.args.get("view")=="deleted"; mine=request.args.get("owner")=="mine"; compact=request.args.get("compact")=="1"
    try:
        limit=max(1,min(int(request.args.get("limit",60)),60)); offset=max(0,min(int(request.args.get("offset",0)),100000))
    except (TypeError,ValueError):
        return jsonify(error="Audiobook page is invalid."),422
    conditions=["deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"]; args=[]
    if deleted or mine: conditions.append("owner_id=?"); args.append(identity["owner_id"] or "")
    else: conditions.append("visibility='shared'")
    if query: conditions.append("(LOWER(title) LIKE ? OR LOWER(author) LIKE ? OR LOWER(series) LIKE ?)"); args.extend([f"%{query}%"]*3)
    with connect(DB_PATH) as connection:
        where=" AND ".join(conditions)
        total=connection.execute(f"SELECT COUNT(*) FROM audiobooks WHERE {where}",args).fetchone()[0]
        rows=connection.execute(f"SELECT * FROM audiobooks WHERE {where} ORDER BY LOWER(author),LOWER(series),LOWER(title),id LIMIT ? OFFSET ?",(*args,limit,offset)).fetchall()
        ids=[row["id"] for row in rows]
        states={}
        if ids:
            placeholders=",".join("?" for _ in ids)
            states={row["book_id"]:dict(row) for row in connection.execute(f"SELECT * FROM audiobook_progress WHERE owner_id=? AND book_id IN ({placeholders})",(identity["owner_id"] or "",*ids)).fetchall()}
    playback=playback_statuses(ids)
    next_offset=offset+len(rows)
    return jsonify(books=[serialize(row,identity,states.get(row["id"]),playback.get(row["id"]),compact) for row in rows],deleted=deleted,current_user=identity["name"],progress_scope=progress_scope(identity),total=total,has_more=next_offset<total,next_offset=next_offset if next_offset<total else None)

@bp.get("/api/audiobooks/<book_id>")
@require_profile()
def detail(book_id):
    identity=current_device(); row=find_book(book_id,deleted_owner_only=request.args.get("view")=="deleted")
    if not row: return jsonify(error="Audiobook not found."),404
    with connect(DB_PATH) as connection:
        progress=connection.execute("SELECT * FROM audiobook_progress WHERE owner_id=? AND book_id=?",(identity["owner_id"] or "",book_id)).fetchone()
    return jsonify(book=serialize(row,identity,dict(progress) if progress else None))

@bp.post("/api/audiobooks/upload")
@require_profile()
def upload():
    identity=current_device(); visibility=request.form.get("visibility","shared").lower(); uploads=request.files.getlist("books")[:20]
    if not identity["owner_id"]: return jsonify(error="Open David-Pi through private Tailscale HTTPS."),401
    if visibility not in {"shared","private"}: return jsonify(error="Choose Shared or Only me."),400
    if not uploads: return jsonify(error="Choose at least one audiobook."),400
    added=[]; duplicates=[]; errors=[]
    for item in uploads:
        original=clean(item.filename,240) or "Untitled audiobook"; suffix=Path(secure_filename(original)).suffix.lower()
        if suffix not in ALLOWED: errors.append({"name":original,"reason":"Use a DRM-free MP3, M4B, M4A, AAC, OGG, Opus, FLAC, or WAV file."}); continue
        temporary=None; staging_identity=None; cover_temporary=None; cover_identity=None
        reservation=None; library_committed=False; digest=hashlib.sha256(); size=0
        try:
            if shutil.disk_usage(ROOT).free < RESERVE: raise ValueError("David-Pi needs more free space before importing audiobooks.")
            upload_token,temporary,descriptor,staging_identity=_exclusive_upload_staging()
            with os.fdopen(descriptor,"wb") as target:
                while chunk:=item.stream.read(1024**2):
                    size+=len(chunk)
                    if size>MAX_BYTES: raise ValueError("This audiobook exceeds the 4 GB file limit.")
                    digest.update(chunk); target.write(chunk)
                target.flush(); os.fsync(target.fileno())
            if not size: raise ValueError("The selected audiobook is empty.")
            if shutil.disk_usage(ROOT).free < RESERVE:
                raise ValueError("David-Pi needs more free space before importing audiobooks.")
            sha=digest.hexdigest()
            with connect(DB_PATH) as connection: duplicate=connection.execute("SELECT title FROM audiobooks WHERE owner_id=? AND sha256=? AND deleted_at IS NULL",(identity["owner_id"],sha)).fetchone()
            if duplicate: raise DuplicateAudiobookError(original)
            tags,duration,chapters=probe(temporary); duration=finite_duration(duration,require_positive=True); title=clean(tags.get("title"),180) or Path(original).stem[:180]; author=clean(tags.get("album_artist") or tags.get("artist") or tags.get("composer"),160); series=clean(tags.get("album"),160)
            cover_temporary=managed(INCOMING,f"{upload_token}.cover.part")
            extracted_stage=cover(temporary,cover_temporary)
            if extracted_stage:
                cover_identity=_file_identity(cover_temporary)

            # UUIDs are not treated as ownership proof. The queue transaction
            # must reserve an ID that is absent from queue/catalog/journal;
            # NOREPLACE publication below independently protects the files.
            for _attempt in range(32):
                reservation_token=uuid.uuid4().hex
                book_id=uuid.uuid4().hex
                final=managed(ORIGINALS,f"{book_id}{suffix}")
                final_cover=managed(COVERS,f"{book_id}.jpg") if extracted_stage else None
                if final.exists() or final.is_symlink() or (final_cover and (final_cover.exists() or final_cover.is_symlink())):
                    continue
                try:
                    reserve_import(
                        reservation_token,book_id,final.name,size,sha,duration,
                        temporary.name,*staging_identity,
                        cover_staging_name=cover_temporary.name if extracted_stage else None,
                        cover_name=final_cover.name if final_cover else None,
                        cover_device=cover_identity[0] if cover_identity else None,
                        cover_inode=cover_identity[1] if cover_identity else None,
                    )
                except ValueError as error:
                    if str(error)=="import_id_conflict": continue
                    raise
                reservation=import_reservation(reservation_token,book_id)
                if not reservation: raise ValueError("Audiobook import reservation was not durable.")
                break
            else:
                raise ValueError("import_id_conflict")

            _publish_noreplace(temporary,final,*staging_identity)
            if final_cover:
                _publish_noreplace(cover_temporary,final_cover,*cover_identity)
            mark_import_published(reservation_token,book_id)
            reservation=import_reservation(reservation_token,book_id)
            now=utcnow()
            with connect(DB_PATH) as connection:
                connection.execute("BEGIN IMMEDIATE")
                duplicate=connection.execute(
                    "SELECT title FROM audiobooks WHERE owner_id=? AND sha256=? AND deleted_at IS NULL",
                    (identity["owner_id"],sha),
                ).fetchone()
                if duplicate: raise DuplicateAudiobookError(original)
                connection.execute("""INSERT INTO audiobooks(id,title,author,narrator,series,original_name,stored_name,cover_name,content_type,byte_size,sha256,duration_seconds,chapters_json,owner_id,owner_name,visibility,created_at,updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",(book_id,title,author,"",series,original,final.name,final_cover.name if final_cover else None,MIMES[suffix],size,sha,duration,json.dumps(chapters),identity["owner_id"],identity["name"],visibility,now,now))
                # Queue/catalog rows are inert until the authoritative library
                # transaction commits, so a worker can never claim a partial
                # import. Any exception rolls the library transaction back.
                commit_reserved_import(reservation_token,book_id)
            library_committed=True
            try:
                activate_reserved_import(reservation_token,book_id)
            except (OSError,sqlite3.Error,ValueError):
                # The authoritative library row and immutable original are
                # already committed and range-playable.  Never roll that row
                # back here: a user may concurrently edit metadata or save
                # progress after commit.  The durable reservation remains in
                # `queued` state so periodic recovery can activate derivatives
                # without deleting any saved content.
                current_app.logger.exception("Audiobook queue activation will be retried")
            if _cleanup_reservation_files(reservation,remove_published=False):
                try: finish_import(reservation_token,book_id)
                except (OSError,sqlite3.Error,ValueError): current_app.logger.exception("Audiobook import journal will be finalized later")
            added.append(title)
        except DuplicateAudiobookError:
            if reservation:
                _abort_reserved_files(reservation)
            else:
                if temporary and staging_identity: _unlink_matching(temporary,*staging_identity)
                if cover_temporary and cover_identity: _unlink_matching(cover_temporary,*cover_identity)
            duplicates.append(original)
        except (OSError,ValueError,sqlite3.Error) as error:
            if reservation and not library_committed:
                _abort_reserved_files(reservation)
            elif not reservation:
                if temporary and staging_identity: _unlink_matching(temporary,*staging_identity)
                if cover_temporary and cover_identity: _unlink_matching(cover_temporary,*cover_identity)
            errors.append({"name":original,"reason":_import_error_reason(error)})
    return jsonify(added=added,duplicates=duplicates,errors=errors),201 if added else (409 if duplicates and not errors else 422)

@bp.get("/api/audiobooks/<book_id>/stream")
def stream(book_id):
    row=find_book(book_id)
    if not row: return jsonify(error="Audiobook not found."),404
    optimized=managed(PLAYBACK,row["stored_name"])
    source=optimized if optimized.is_file() and optimized.stat().st_size else managed(ORIGINALS,row["stored_name"])
    return ranged_file(source,mimetype=row["content_type"])

@bp.get("/api/audiobooks/<book_id>/hls/index.m3u8")
def hls_playlist(book_id):
    if not find_book(book_id): return jsonify(error="Audiobook not found."),404
    paths=derivative_paths(book_id)
    if not paths: return jsonify(error="Segmented playback is not ready."),404
    response=send_file(paths[0],mimetype="application/vnd.apple.mpegurl",conditional=True)
    response.headers["Cache-Control"]="private, no-store"
    response.headers["Vary"]="Cookie"
    return response

@bp.get("/api/audiobooks/<book_id>/hls/index.m4s")
def hls_media(book_id):
    if not find_book(book_id): return jsonify(error="Audiobook not found."),404
    paths=derivative_paths(book_id)
    if not paths: return jsonify(error="Segmented playback is not ready."),404
    response=ranged_file(paths[1],mimetype="video/iso.segment")
    response.headers["Cache-Control"]="private, no-store"
    response.headers["Vary"]="Cookie"
    return response
@bp.get("/api/audiobooks/<book_id>/download")
def download(book_id):
    row=find_book(book_id)
    return ranged_file(managed(ORIGINALS,row["stored_name"]),mimetype="application/octet-stream",as_attachment=True,download_name=row["original_name"]) if row else (jsonify(error="Audiobook not found."),404)
@bp.get("/api/audiobooks/<book_id>/cover")
def get_cover(book_id):
    row=find_book(book_id)
    return send_file(managed(COVERS,row["cover_name"]),mimetype="image/jpeg",conditional=True,max_age=604800) if row and row["cover_name"] else ("",404)

def progress_snapshot(row):
    state = dict(row) if row else {}
    return {
        "position_seconds": state.get("position_seconds", 0.0),
        "completed": bool(state.get("completed", False)),
        "progress_revision": int(state.get("revision", 0)),
        "progress_session": state.get("session_id", ""),
        "progress_sequence": int(state.get("sequence", 0)),
    }


def ordered_progress(connection, book_id, owner_id, payload, position, completed):
    """Compare and advance one listening revision atomically; positions may rewind.

    Old clients and unattributed queues are candidates, never implicit writes.
    An exact retry of the most recent event is idempotent. A late event from an
    old session cannot reclaim the record after another device has advanced it.
    The caller owns commit/rollback (including failed commits).
    """
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute(
        "SELECT * FROM audiobook_progress WHERE book_id=? AND owner_id=?",
        (book_id, owner_id),
    ).fetchone()
    current = progress_snapshot(row)
    session = payload.get("session_id")
    sequence = payload.get("sequence")
    base = payload.get("base_revision")
    ordered = (
        isinstance(session, str) and len(session) == 32
        and all(char in "0123456789abcdef" for char in session)
        and type(sequence) is int and 0 < sequence <= 9007199254740991
        and type(base) is int and 0 <= base < 9007199254740991
    )
    same_session = ordered and session == current["progress_session"]
    if (same_session and sequence == current["progress_sequence"]
            and position == current["position_seconds"] and completed == current["completed"]):
        return {"ok": True, "duplicate": True, **current}, 200
    if (not ordered or base != current["progress_revision"]
            or (same_session and sequence <= current["progress_sequence"])):
        return {
            "error": "Listening progress changed elsewhere. Choose which position to keep.",
            "conflict": True, "reason": "stale_progress" if ordered else "legacy_progress",
            "current": current,
            "candidate": {"position_seconds": position, "completed": completed},
        }, 409
    revision = current["progress_revision"] + 1
    connection.execute(
        """INSERT INTO audiobook_progress
        (book_id,owner_id,position_seconds,completed,updated_at,revision,session_id,sequence)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(book_id,owner_id) DO UPDATE SET
        position_seconds=excluded.position_seconds,completed=excluded.completed,
        updated_at=excluded.updated_at,revision=excluded.revision,
        session_id=excluded.session_id,sequence=excluded.sequence""",
        (book_id, owner_id, position, int(completed), utcnow(), revision, session, sequence),
    )
    return {"ok": True, "position_seconds": position, "completed": completed,
            "progress_revision": revision, "progress_session": session,
            "progress_sequence": sequence}, 200


@bp.put("/api/audiobooks/<book_id>/progress")
def progress(book_id):
    identity=current_device(); row=find_book(book_id); payload=request.get_json(silent=True) or {}
    if not row or not identity["owner_id"]: return jsonify(error="Audiobook not found."),404
    if not isinstance(payload, dict): return jsonify(error="Playback progress is invalid."),422
    if str(payload.get("progress_scope") or "") != progress_scope(identity):
        return jsonify(error="Listening identity changed. Reload the audiobook shelf before syncing progress."),409
    try:
        position=float(payload.get("position_seconds",0)); duration=finite_duration(row["duration_seconds"])
        if not math.isfinite(position): raise ValueError("invalid position")
        position=max(0.0,min(position,duration+30))
    except (TypeError,ValueError,OverflowError): return jsonify(error="Playback position is invalid."),422
    completed=bool(payload.get("completed"))
    with connect(DB_PATH) as connection:
        result, status = ordered_progress(connection, book_id, identity["owner_id"], payload, position, completed)
    return jsonify(result), status

@bp.put("/api/audiobooks/<book_id>")
def update(book_id):
    identity=current_device(); payload=request.get_json(silent=True) or {}
    with connect(DB_PATH) as connection: row=connection.execute("SELECT * FROM audiobooks WHERE id=? AND deleted_at IS NULL AND owner_id=?",(book_id,identity["owner_id"] or "")).fetchone()
    if not row: return jsonify(error="Audiobook not found."),404
    visibility=payload.get("visibility",row["visibility"])
    if visibility not in {"shared","private"}: return jsonify(error="Choose Shared or Only me."),422
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET title=?,author=?,visibility=?,updated_at=? WHERE id=?",(clean(payload.get("title"),180) or row["title"],clean(payload.get("author"),160),visibility,utcnow(),book_id))
    return jsonify(ok=True)
@bp.delete("/api/audiobooks/<book_id>")
def trash(book_id):
    identity=current_device()
    with connect(DB_PATH) as connection: row=connection.execute("SELECT * FROM audiobooks WHERE id=? AND deleted_at IS NULL AND owner_id=?",(book_id,identity["owner_id"] or "")).fetchone()
    if not row: return jsonify(error="Audiobook not found."),404
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET deleted_at=?,updated_at=? WHERE id=?",(utcnow(),utcnow(),book_id))
    try: suspend(book_id)
    except (OSError,sqlite3.Error): pass
    return jsonify(ok=True)
@bp.post("/api/audiobooks/<book_id>/restore")
def restore(book_id):
    identity=current_device(); row=find_book(book_id,deleted_owner_only=True)
    if not row or row["owner_id"]!=identity["owner_id"]: return jsonify(error="Audiobook not found."),404
    with connect(DB_PATH) as connection: connection.execute("UPDATE audiobooks SET deleted_at=NULL,updated_at=? WHERE id=?",(utcnow(),book_id))
    try: enqueue(row["id"],row["stored_name"],row["byte_size"],row["sha256"],row["duration_seconds"])
    except (OSError,sqlite3.Error,ValueError): pass
    return jsonify(ok=True)


def reconcile_incomplete_imports(minimum_age_seconds=None):
    """Finish committed imports and retire stale, uncommitted ones safely.

    A reservation is the crash boundary between the two SQLite databases and
    the filesystem. Fresh reservations without a library row may belong to a
    request in another web worker, so only age-expired ones are aborted. A
    matching committed library row is always safe to finish immediately.
    """
    if minimum_age_seconds is None:
        minimum_age_seconds = max(300, int(os.environ.get(
            "DAVID_PI_AUDIOBOOK_IMPORT_RECOVERY_SECONDS", "3600"
        )))
    minimum_age_seconds = max(0, int(minimum_age_seconds))
    cutoff_time = datetime.now(timezone.utc) - timedelta(seconds=minimum_age_seconds)
    cutoff = cutoff_time.isoformat()
    reservations = pending_import_reservations()
    protected_staging = {
        name
        for reservation in reservations
        for name in (reservation.get("staging_name"), reservation.get("cover_staging_name"))
        if name
    }
    result = {"completed": 0, "aborted": 0, "deferred": 0, "staging_removed": 0}
    for reservation in reservations:
        try:
            # The importer always takes the authoritative library write lock
            # before it creates inert queue rows.  Recovery takes the same
            # lock order and holds this lock through its queue claim and file
            # cleanup.  Therefore an in-flight importer either commits first
            # and is observed below, or its later queue transition fails and
            # rolls its still-uncommitted library insert back.  There is no
            # no-row/read gap in which recovery can delete a committed book.
            with connect(DB_PATH) as library:
                library.execute("BEGIN IMMEDIATE")
                row = library.execute(
                    "SELECT * FROM audiobooks WHERE id=?", (reservation["book_id"],)
                ).fetchone()
                if row:
                    if not _library_row_matches_import(row,reservation) or not _final_files_match_import(reservation):
                        result["deferred"] += 1
                        continue
                    state = reservation["state"]
                    if state == "aborting":
                        # Exact library state won an older abort boundary.
                        # Restore its active queue/catalog pair atomically
                        # before removing the durable cleanup journal.
                        reactivate_aborting_import(
                            reservation["reservation_token"],reservation["book_id"]
                        )
                        state = "activated"
                    if state == "reserved":
                        mark_import_published(reservation["reservation_token"],reservation["book_id"])
                        state = "published"
                    if state == "published":
                        commit_reserved_import(reservation["reservation_token"],reservation["book_id"])
                        state = "queued"
                    if state == "queued":
                        activate_reserved_import(reservation["reservation_token"],reservation["book_id"])
                        state = "activated"
                    refreshed = import_reservation(reservation["reservation_token"],reservation["book_id"])
                    if state != "activated" or not refreshed:
                        raise ValueError("import_reservation_conflict")
                    cleaned = _cleanup_reservation_files(refreshed,remove_published=False)
                    if cleaned: finish_import(refreshed["reservation_token"],refreshed["book_id"])
                    result["completed" if cleaned else "deferred"] += 1
                    continue
                if reservation["updated_at"] > cutoff:
                    result["deferred"] += 1
                    continue
                aborting = begin_abort_import(reservation["reservation_token"],reservation["book_id"])
                if not aborting:
                    result["deferred"] += 1
                    continue
                cleaned = _cleanup_reservation_files(aborting,remove_published=True)
                if cleaned: finish_abort_import(aborting["reservation_token"],aborting["book_id"])
                result["aborted" if cleaned else "deferred"] += 1
        except (OSError,sqlite3.Error,ValueError):
            result["deferred"] += 1

    # A process can die while receiving bytes, before duration/capacity is
    # known and therefore before a reservation exists. Only old, exact staging
    # filenames that no active reservation references are eligible here.
    cutoff_epoch = time.time() - minimum_age_seconds
    for candidate in INCOMING.iterdir():
        name = candidate.name
        token = name.split(".",1)[0]
        if (
            name in protected_staging
            or len(token) != 32
            or any(character not in "0123456789abcdef" for character in token)
            or not (name.endswith(".upload.part") or name.endswith(".cover.part"))
        ):
            continue
        try:
            details = candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(details.st_mode) or details.st_mtime > cutoff_epoch:
                continue
            if _unlink_matching(candidate,str(details.st_dev),str(details.st_ino)):
                result["staging_removed"] += 1
        except OSError:
            continue
    return result


def scheduled_import_recovery(*, force=False, current_epoch=None):
    """Run recovery when this process and the shared durable lease say due."""
    global _next_import_recovery_poll
    monotonic = time.monotonic()
    with _import_recovery_poll_lock:
        if not force and monotonic < _next_import_recovery_poll:
            return None
        _next_import_recovery_poll = monotonic + IMPORT_RECOVERY_POLL_SECONDS
    descriptor = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(IMPORT_RECOVERY_LOCK, flags, 0o600)
        details = os.fstat(descriptor)
        binding = os.stat(IMPORT_RECOVERY_LOCK, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_nlink != 1
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or (details.st_dev, details.st_ino) != (binding.st_dev, binding.st_ino)
        ):
            raise ValueError("Audiobook import recovery lock is not trusted.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        if not claim_import_recovery(
            IMPORT_RECOVERY_INTERVAL_SECONDS, current_epoch=current_epoch
        ):
            return None
        return reconcile_incomplete_imports()
    finally:
        if descriptor is not None:
            try: os.close(descriptor)
            except OSError: pass

def init_audiobooks(app):
    try:
        recovery = scheduled_import_recovery(force=True)
        if recovery and recovery["deferred"]:
            app.logger.warning("Some audiobook import cleanup remains deferred")
    except (OSError,sqlite3.Error,ValueError):
        app.logger.warning("Audiobook import recovery is unavailable; saved originals were not changed")
    @app.context_processor
    def _audiobook_browser_context():
        # This stable digest is only a browser-local namespace.  It is not an
        # authorization credential; every API request still authenticates the
        # current Tailscale identity.  Making it available to every rendered
        # module lets same-identity tabs coordinate an active player without
        # exposing its state to another signed-in household identity.
        return {"audiobook_progress_scope": progress_scope(current_device())}
    @app.before_request
    def _periodic_audiobook_import_recovery():
        # The production health probe supplies a request at least every thirty
        # seconds, so this remains periodic even when nobody is using the UI.
        try:
            recovery = scheduled_import_recovery()
            if recovery and recovery["deferred"]:
                app.logger.warning("Some audiobook import cleanup remains deferred")
        except (OSError,sqlite3.Error,ValueError):
            app.logger.warning("Audiobook import recovery is unavailable; saved originals were not changed")
    with connect(DB_PATH) as connection:
        rows=connection.execute("SELECT id,stored_name,byte_size,sha256,duration_seconds FROM audiobooks WHERE deleted_at IS NULL").fetchall()
    try: reconcile_catalog([dict(row) for row in rows])
    except (OSError,sqlite3.Error,ValueError): app.logger.warning("Audiobook derivative queue is unavailable; range playback remains enabled.")
    app.register_blueprint(bp)
