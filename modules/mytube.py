"""Private household video library with resumable, non-destructive ingestion."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, current_app, has_app_context, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .content_ownership import audit_mutation
from .content_policy import Actor
from .household_content import allowed_actor
from .identity import current_device, require_profile
from .mytube_streaming import hls_file, ranged_response, safe_regular_file
from .platform import PLATFORM_DATA, connect, migrate, utcnow


DB_PATH = PLATFORM_DATA / "mytube.db"
ROOT = Path(os.environ.get("DAVID_PI_MYTUBE_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "mytube"))
ORIGINALS = ROOT / "originals"
INCOMING = ROOT / "incoming"
POSTERS = ROOT / "posters"
STREAMING = ROOT / "streaming"
MAX_UPLOAD_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_MAX_UPLOAD_BYTES", str(64 * 1024**3)))
MAX_CHUNK_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_MAX_CHUNK_BYTES", str(16 * 1024**2)))
MAX_ACTIVE_UPLOADS = int(os.environ.get("DAVID_PI_MYTUBE_MAX_ACTIVE_UPLOADS", "2"))
MIN_FREE_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_MIN_FREE_BYTES", str(100 * 1024**3)))
PEAK_STORAGE_MULTIPLIER = max(2, min(int(os.environ.get("DAVID_PI_MYTUBE_PEAK_STORAGE_MULTIPLIER", "3")), 5))
UPLOAD_TTL_HOURS = int(os.environ.get("DAVID_PI_MYTUBE_UPLOAD_TTL_HOURS", "24"))
FFPROBE_TIMEOUT = int(os.environ.get("DAVID_PI_MYTUBE_FFPROBE_TIMEOUT", "30"))
VIDEO_ID = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_ID = VIDEO_ID
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_SUFFIXES = {".mp4", ".m4v"}
VIDEO_MIMES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4",
}

bp = Blueprint("mytube", __name__)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaReference:
    """Projection passed after photos.db has committed the authoritative link."""

    video_id: str
    media_id: str
    title: str
    owner_id: str
    owner_name: str
    visibility: str
    content_type: str
    byte_size: int
    sha256: str
    duration_seconds: float = 0.0


def initialize_mytube(connection):
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_videos(
        id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',
        source_kind TEXT NOT NULL CHECK(source_kind IN ('upload','media')),
        stored_name TEXT,media_id TEXT,content_type TEXT NOT NULL,byte_size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,duration_seconds REAL NOT NULL DEFAULT 0,width INTEGER,height INTEGER,
        video_codec TEXT,audio_codec TEXT,owner_id TEXT NOT NULL,owner_name TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'shared' CHECK(visibility IN ('shared','private')),
        playback_state TEXT NOT NULL DEFAULT 'direct' CHECK(playback_state IN ('direct','pending','preparing','ready','failed')),
        playback_mode TEXT NOT NULL DEFAULT 'direct' CHECK(playback_mode IN ('direct','hls')),
        hls_generation TEXT,prepared_name TEXT,poster_name TEXT,version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT,deleted_by TEXT,purge_after TEXT,
        CHECK((source_kind='upload' AND stored_name IS NOT NULL AND media_id IS NULL)
           OR (source_kind='media' AND media_id IS NOT NULL AND stored_name IS NULL)))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS mytube_visible_idx ON mytube_videos(deleted_at,visibility,owner_id,created_at DESC)"
    )
    video_columns = {row["name"] for row in connection.execute("PRAGMA table_info(mytube_videos)")}
    if "prepared_name" not in video_columns:
        connection.execute("ALTER TABLE mytube_videos ADD COLUMN prepared_name TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS mytube_media_projection_idx ON mytube_videos(media_id) WHERE source_kind='media'"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_progress(
        video_id TEXT NOT NULL REFERENCES mytube_videos(id) ON DELETE CASCADE,
        owner_id TEXT NOT NULL,position_seconds REAL NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,PRIMARY KEY(video_id,owner_id))"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_uploads(
        id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,owner_name TEXT NOT NULL,original_name TEXT NOT NULL,
        staging_name TEXT NOT NULL,expected_size INTEGER NOT NULL,offset INTEGER NOT NULL DEFAULT 0,
        expected_sha256 TEXT,visibility TEXT NOT NULL CHECK(visibility IN ('shared','private')),
        title TEXT NOT NULL,idempotency_key TEXT NOT NULL,request_digest TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('open','finalizing','complete','cancelled','failed')),
        video_id TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,expires_at TEXT NOT NULL,
        UNIQUE(owner_id,idempotency_key))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS mytube_upload_expiry_idx ON mytube_uploads(state,expires_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_prepare_jobs(
        video_id TEXT PRIMARY KEY REFERENCES mytube_videos(id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK(state IN ('pending','preparing','ready','failed','paused')),
        generation INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,
        available_at TEXT NOT NULL,lease_token TEXT,lease_expires_at TEXT,error_code TEXT,updated_at TEXT NOT NULL)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS mytube_prepare_next_idx ON mytube_prepare_jobs(state,available_at,updated_at)"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_collections(
        id TEXT PRIMARY KEY,name TEXT NOT NULL,owner_id TEXT NOT NULL,owner_name TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'shared' CHECK(visibility IN ('shared','private')),
        version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,deleted_at TEXT)"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS mytube_collection_members(
        collection_id TEXT NOT NULL REFERENCES mytube_collections(id) ON DELETE CASCADE,
        video_id TEXT NOT NULL REFERENCES mytube_videos(id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,added_by TEXT NOT NULL,PRIMARY KEY(collection_id,video_id))"""
    )


def ensure_storage():
    for directory in (ROOT, ORIGINALS, INCOMING, POSTERS, STREAMING):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("MyTube storage is unavailable.")
    if INCOMING.stat().st_dev != ORIGINALS.stat().st_dev:
        raise RuntimeError("MyTube incoming and original storage must share one filesystem.")


def init_mytube(app):
    ensure_storage()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    migrate(DB_PATH, initialize_mytube)
    reconcile_upload_storage()
    recover_incomplete_uploads()
    expire_uploads()
    app.register_blueprint(bp)


def _actor():
    actor, name = allowed_actor()
    if actor is None:
        return None, name, (jsonify(error="Open David-Pi through an approved private Tailscale account."), 403)
    return actor, name, None


def _visible(row, actor) -> bool:
    if not row or not (row["visibility"] == "shared" or row["owner_id"] == actor.principal_id):
        return False
    if row["source_kind"] == "media":
        authorizer = current_app.config.get("MYTUBE_MEDIA_AUTHORIZER") if has_app_context() else None
        return bool(callable(authorizer) and authorizer(row["media_id"], actor))
    return True


def _video(video_id: str, actor, *, include_deleted: bool = False):
    if not VIDEO_ID.fullmatch(str(video_id or "")):
        return None
    deleted = "" if include_deleted else "AND deleted_at IS NULL"
    with connect(DB_PATH) as connection:
        row = connection.execute(f"SELECT * FROM mytube_videos WHERE id=? {deleted}", (video_id,)).fetchone()
    return row if _visible(row, actor) else None


def _catalog_visibility(connection, actor):
    """Authorize linked sources in one SQLite query, not one connection per card.

    The attached source is explicitly read-only and still authoritative after
    privacy changes or unlinking, even if the display projection lags behind.
    """
    source = current_app.config.get("MYTUBE_MEDIA_DATABASE")
    if source:
        connection.execute("ATTACH DATABASE ? AS source_media", (Path(source).resolve().as_uri() + "?mode=ro",))
        return ("""(v.source_kind<>'media' OR EXISTS (
            SELECT 1 FROM source_media.photos mp
            JOIN source_media.mytube_media_links ml ON ml.media_id=mp.id
            WHERE mp.id=v.media_id AND ml.video_id=v.id AND mp.deleted_at IS NULL
            AND (mp.visibility='shared' OR mp.owner_id=?)))""", [actor.principal_id])
    # Standalone module tests/embedders must also fail closed for media links.
    authorizer = current_app.config.get("MYTUBE_MEDIA_AUTHORIZER")
    connection.create_function("mytube_media_visible", 1,
        lambda media_id: int(bool(callable(authorizer) and authorizer(media_id, actor))))
    return "(v.source_kind<>'media' OR mytube_media_visible(v.media_id)=1)", []


def _collection(connection, collection_id, actor):
    return connection.execute("""SELECT * FROM mytube_collections
        WHERE id=? AND deleted_at IS NULL AND (visibility='shared' OR owner_id=?)""",
        (collection_id, actor.principal_id)).fetchone()


def _clean(value, maximum):
    value = " ".join(str(value or "").split())
    return value[:maximum]


def _safe_int(value, *, minimum=0, maximum=(1 << 63) - 1):
    if isinstance(value, bool):
        raise ValueError("invalid integer")
    number = int(value)
    if number < minimum or number > maximum:
        raise ValueError("invalid integer")
    return number


def _serialize(row, actor, progress=None):
    item = {key: row[key] for key in (
        "id", "title", "description", "source_kind", "content_type", "byte_size", "duration_seconds",
        "width", "height", "visibility", "playback_state", "playback_mode", "created_at", "version",
    )}
    item.update(
        is_mine=row["owner_id"] == actor.principal_id,
        poster_url=f"/api/mytube/videos/{row['id']}/poster" if row["poster_name"] else None,
        stream_url=f"/api/mytube/videos/{row['id']}/stream",
        hls_url=f"/api/mytube/videos/{row['id']}/hls/master.m3u8" if row["playback_mode"] == "hls" else None,
        watch_url=f"/mytube/watch/{row['id']}",
        position_seconds=float(progress["position_seconds"]) if progress else 0.0,
        completed=bool(progress["completed"]) if progress else False,
        media_id=row["media_id"] if row["source_kind"] == "media" and row["owner_id"] == actor.principal_id else None,
    )
    return item


def _source_path(row) -> Path:
    if row["source_kind"] == "upload":
        return safe_regular_file(ORIGINALS, row["stored_name"])
    resolver = current_app.config.get("MYTUBE_MEDIA_RESOLVER") if has_app_context() else None
    if not callable(resolver):
        resolver = _worker_media_resolver
    if not callable(resolver):
        raise FileNotFoundError("Media source resolver unavailable")
    source = Path(resolver(row["media_id"]))
    if source.is_symlink() or not source.is_file():
        raise FileNotFoundError("Media source unavailable")
    return source


def _worker_media_resolver(media_id: str) -> Path:
    """Resolve a worker source through photos.db without trusting projection paths."""
    photos_db = Path(os.environ.get("DAVID_PI_MYTUBE_MEDIA_DB", "/data/photos.db"))
    originals = Path(os.environ.get("DAVID_PI_MYTUBE_MEDIA_ORIGINALS", "/data/originals"))
    with sqlite3.connect(f"file:{photos_db}?mode=ro", uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT p.stored_path FROM photos p
               JOIN mytube_media_links l ON l.media_id=p.id
               WHERE p.id=? AND p.deleted_at IS NULL AND p.content_type LIKE 'video/%'""",
            (media_id,),
        ).fetchone()
    if not row:
        raise FileNotFoundError("Media source unavailable")
    relative = Path(str(row["stored_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise FileNotFoundError("Media source unavailable")
    root = originals.resolve(strict=True)
    source = (root / relative).resolve(strict=True)
    if source.parent != root and root not in source.parents:
        raise FileNotFoundError("Media source unavailable")
    return source


def _playback_path(row) -> Path:
    prepared_name = str(row["prepared_name"] or "")
    generation = str(row["hls_generation"] or "")
    if prepared_name and generation and Path(generation).name == generation:
        return safe_regular_file(STREAMING / row["id"] / generation, prepared_name)
    return _source_path(row)


def media_duration(path: Path) -> float:
    """Bounded metadata probe only when explicitly linking a new Media video."""
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=min(10, FFPROBE_TIMEOUT), check=True)
        duration = float(json.loads(result.stdout).get("format", {}).get("duration", 0))
        return duration if math.isfinite(duration) and 0 < duration <= 31 * 86400 else 0.0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return 0.0


def _probe(path: Path):
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name,width,height", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT, check=True)
        payload = json.loads(result.stdout)
        streams = payload.get("streams") or []
        stream = next((item for item in streams if item.get("codec_type") == "video"), None) or {}
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        format_name = str((payload.get("format") or {}).get("format_name") or "")
        duration = float((payload.get("format") or {}).get("duration") or 0)
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("duration invalid")
        width = _safe_int(stream.get("width") or 0, maximum=16384)
        height = _safe_int(stream.get("height") or 0, maximum=16384)
        video_codec = _clean(stream.get("codec_name"), 30).casefold()
        audio_codec = _clean(audio.get("codec_name"), 30).casefold() if audio else None
        if (
            path.suffix.casefold() not in ALLOWED_SUFFIXES
            or not {"mov", "mp4"}.intersection(format_name.casefold().split(","))
            or video_codec != "h264"
            or (audio_codec is not None and audio_codec != "aac")
        ):
            raise ValueError("direct-play codec unavailable")
        return {
            "duration_seconds": duration, "width": width, "height": height,
            "video_codec": video_codec, "audio_codec": audio_codec,
        }
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError, ValueError, IndexError) as error:
        raise ValueError("The file is not a supported playable video.") from error


def _stream_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024**2):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _finalize_reserved_file(row, path: Path, metadata: dict, digest: str):
    """Publish a previously journaled finalization and then commit its catalog row."""
    video_id = row["video_id"]
    if not VIDEO_ID.fullmatch(str(video_id or "")):
        raise ValueError("Upload finalization journal is invalid.")
    suffix = Path(row["original_name"]).suffix.casefold()
    stored_name = f"{video_id}{suffix}"
    destination = ORIGINALS / stored_name
    if path != destination:
        if destination.exists():
            raise FileExistsError("Upload destination already exists.")
        with path.open("rb") as source:
            os.fsync(source.fileno())
        os.replace(path, destination)
        _fsync_directory(ORIGINALS)
        _fsync_directory(INCOMING)
    timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM mytube_uploads WHERE id=?", (row["id"],)).fetchone()
        if not current or current["state"] != "finalizing" or current["video_id"] != video_id:
            raise sqlite3.IntegrityError("Upload finalization state changed.")
        connection.execute(
            """INSERT INTO mytube_videos(id,title,source_kind,stored_name,media_id,content_type,byte_size,sha256,
            duration_seconds,width,height,video_codec,audio_codec,owner_id,owner_name,visibility,playback_state,playback_mode,created_at,updated_at)
            VALUES(?,?,'upload',?,NULL,?,?,?,?,?,?,?,?,?,?,?,'direct','direct',?,?)
            ON CONFLICT(id) DO NOTHING""",
            (video_id, row["title"], stored_name, VIDEO_MIMES[suffix], row["expected_size"], digest,
             metadata["duration_seconds"], metadata["width"], metadata["height"], metadata["video_codec"], metadata.get("audio_codec"),
             row["owner_id"], row["owner_name"], row["visibility"], timestamp, timestamp),
        )
        connection.execute("UPDATE mytube_uploads SET state='complete',updated_at=? WHERE id=?", (timestamp, row["id"]))
        saved = connection.execute(
            "SELECT * FROM mytube_videos WHERE id=?", (video_id,)
        ).fetchone()
        audit_mutation(
            connection,
            actor=Actor(principal_id=row["owner_id"], kind="system"),
            domain="mytube_video",
            object_id=video_id,
            action="create",
            before=None,
            after=saved,
        )
    return video_id


def recover_incomplete_uploads():
    """Finish journaled atomic publishes; never remove an original or complete upload."""
    with connect(DB_PATH) as connection:
        rows = connection.execute("SELECT * FROM mytube_uploads WHERE state='finalizing'").fetchall()
    for row in rows:
        suffix = Path(row["original_name"]).suffix.casefold()
        destination = ORIGINALS / f"{row['video_id']}{suffix}"
        try:
            path = destination if destination.is_file() and not destination.is_symlink() else safe_regular_file(INCOMING, row["staging_name"])
            if path.stat().st_size != row["expected_size"]:
                raise ValueError("size mismatch")
            digest = _stream_sha256(path)
            if row["expected_sha256"] and digest != row["expected_sha256"]:
                raise ValueError("checksum mismatch")
            _finalize_reserved_file(row, path, _probe(path), digest)
        except (OSError, ValueError, sqlite3.Error):
            # Leave the durable finalizing journal and every byte in place for
            # a later retry or explicit operator diagnosis.
            LOGGER.warning("A MyTube upload finalization remains pending")


def _safe_staging_name(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}\.part", str(value or "")))


def _remove_staging(path: Path) -> None:
    try:
        safe_regular_file(INCOMING, path.name).unlink()
        _fsync_directory(INCOMING)
    except FileNotFoundError:
        pass


def reconcile_upload_storage():
    """Converge journaled upload rows and unpublished staging files after a crash."""
    with connect(DB_PATH) as connection:
        rows = connection.execute("SELECT * FROM mytube_uploads").fetchall()
    referenced = {row["staging_name"] for row in rows if _safe_staging_name(row["staging_name"])}
    for candidate in INCOMING.iterdir():
        if candidate.name.endswith(".part") and _safe_staging_name(candidate.name) and candidate.name not in referenced:
            _remove_staging(candidate)
    for row in rows:
        path = INCOMING / row["staging_name"]
        if row["state"] in {"cancelled", "complete"}:
            _remove_staging(path)
            continue
        if row["state"] != "open":
            continue
        try:
            actual = safe_regular_file(INCOMING, row["staging_name"]).stat().st_size
        except FileNotFoundError:
            if int(row["offset"]) == 0:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                _fsync_directory(INCOMING)
                continue
            with connect(DB_PATH) as connection:
                connection.execute("UPDATE mytube_uploads SET state='failed',updated_at=? WHERE id=? AND state='open'", (utcnow(), row["id"]))
            continue
        expected_offset = int(row["offset"])
        if actual > expected_offset:
            with path.open("r+b", buffering=0) as destination:
                destination.truncate(expected_offset); os.fsync(destination.fileno())
        elif actual < expected_offset:
            with connect(DB_PATH) as connection:
                connection.execute("UPDATE mytube_uploads SET offset=?,updated_at=? WHERE id=? AND state='open'", (actual, utcnow(), row["id"]))


def expire_uploads():
    """Cancel expired unpublished reservations and remove only their journaled staging file."""
    timestamp = utcnow()
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            "SELECT * FROM mytube_uploads WHERE state IN ('open','failed') AND expires_at<=?", (timestamp,),
        ).fetchall()
    for row in rows:
        with connect(DB_PATH) as connection:
            connection.execute(
                """UPDATE mytube_uploads SET state='cancelled',updated_at=?
                WHERE id=? AND state IN ('open','failed') AND expires_at<=?""",
                (timestamp, row["id"], timestamp),
            )
        _remove_staging(INCOMING / row["staging_name"])


def _upload_digest(filename, size, sha256, visibility, title):
    payload = json.dumps(
        {"filename": filename, "size": size, "sha256": sha256, "visibility": visibility, "title": title},
        separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _free_bytes():
    return shutil.disk_usage(ROOT).free


def _reserved_bytes(connection, timestamp: str, *, excluding: str | None = None) -> int:
    query = """SELECT COALESCE(SUM(expected_size-offset),0) FROM mytube_uploads
               WHERE state IN ('open','finalizing') AND expires_at>?"""
    arguments: list[object] = [timestamp]
    if excluding:
        query += " AND id<>?"; arguments.append(excluding)
    return max(0, int(connection.execute(query, arguments).fetchone()[0]))


@bp.get("/mytube")
@require_profile(api=False)
def page():
    actor, _name, error = _actor()
    return error or render_template("mytube.html")


@bp.get("/mytube/watch/<video_id>")
@require_profile(api=False)
def watch_page(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    if not _video(video_id, actor):
        return render_template("mytube_watch.html", initial_video_id=""), 404
    return render_template("mytube_watch.html", initial_video_id=video_id)


@bp.get("/api/mytube/videos")
@require_profile()
def list_videos():
    actor, _name, error = _actor()
    if error:
        return error
    reconciler = current_app.config.get("MYTUBE_MEDIA_RECONCILER")
    if callable(reconciler):
        try:
            reconciler()
        except (OSError, ValueError, sqlite3.Error):
            LOGGER.exception("MyTube media projection reconciliation failed")
            return jsonify(error="MyTube is safely reconciling Media links. Try again."), 503
    view = request.args.get("view", "library")
    if view not in {"library", "continue", "recent", "mine", "trash"}:
        return jsonify(error="Choose a valid video view."), 422
    conditions = ["(v.visibility='shared' OR v.owner_id=?)"]
    args = [actor.principal_id]
    if view == "trash":
        conditions.extend(["v.deleted_at IS NOT NULL", "v.owner_id=?"]); args.append(actor.principal_id)
    else:
        conditions.append("v.deleted_at IS NULL")
    if view == "mine":
        conditions.append("v.owner_id=?"); args.append(actor.principal_id)
    if view == "continue":
        conditions.append(
            "EXISTS(SELECT 1 FROM mytube_progress cp WHERE cp.video_id=v.id AND cp.owner_id=? "
            "AND cp.position_seconds>0 AND cp.completed=0)"
        )
        args.append(actor.principal_id)
    try:
        limit = _safe_int(request.args.get("limit", 48), minimum=1, maximum=100)
        offset = _safe_int(request.args.get("offset", 0), maximum=100000)
    except (TypeError, ValueError):
        return jsonify(error="Video page is invalid."), 422
    with connect(DB_PATH, uri=True) as connection:
        visible, visibility_args = _catalog_visibility(connection, actor)
        conditions.append(visible); args.extend(visibility_args)
        collection_id = request.args.get("collection")
        collection = _collection(connection, collection_id, actor) if collection_id else None
        if collection_id and not collection:
            return jsonify(error="Collection is unavailable."), 404
        if collection:
            conditions.append("EXISTS (SELECT 1 FROM mytube_collection_members cm WHERE cm.collection_id=? AND cm.video_id=v.id)")
            args.append(collection_id)
        where = " AND ".join(conditions)
        order = "COALESCE(p.updated_at,v.created_at)" if view == "continue" else "v.created_at"
        total = connection.execute(f"SELECT COUNT(*) FROM mytube_videos v WHERE {where}", args).fetchone()[0]
        rows = connection.execute(
            f"""SELECT v.*,p.position_seconds,p.completed,p.updated_at AS progress_updated_at
            FROM mytube_videos v LEFT JOIN mytube_progress p ON p.video_id=v.id AND p.owner_id=?
            WHERE {where} ORDER BY {order} DESC,v.id DESC LIMIT ? OFFSET ?""",
            (actor.principal_id, *args, limit, offset),
        ).fetchall()
    items = [_serialize(row, actor, row if row["progress_updated_at"] else None) for row in rows]
    return jsonify(videos=items, total=total, has_more=offset + len(items) < total, next_offset=offset + len(items),
        collection={"id": collection["id"], "name": collection["name"], "is_mine": collection["owner_id"] == actor.principal_id} if collection else None)


@bp.get("/api/mytube/collections")
@require_profile()
def list_collections():
    actor, _name, error = _actor()
    if error:
        return error
    try:
        offset = _safe_int(request.args.get("offset", 0), maximum=100000)
        limit = _safe_int(request.args.get("limit", 48), minimum=1, maximum=200)
    except (TypeError, ValueError):
        return jsonify(error="Collection page is invalid."), 422
    with connect(DB_PATH, uri=True) as connection:
        visible, args = _catalog_visibility(connection, actor)
        scope = "owner_id=?" if request.args.get("owner") == "mine" else "(visibility='shared' OR owner_id=?)"
        total = connection.execute(f"SELECT COUNT(*) FROM mytube_collections WHERE deleted_at IS NULL AND {scope}", (actor.principal_id,)).fetchone()[0]
        rows = connection.execute(
            f"""SELECT c.*,(SELECT COUNT(*) FROM mytube_collection_members m
                JOIN mytube_videos v ON v.id=m.video_id
                WHERE m.collection_id=c.id AND v.deleted_at IS NULL
                AND (v.visibility='shared' OR v.owner_id=?) AND {visible}) AS video_count
            FROM mytube_collections c
            WHERE c.deleted_at IS NULL AND {scope}
            ORDER BY LOWER(c.name),c.id LIMIT ? OFFSET ?""",
            (actor.principal_id, *args, actor.principal_id, limit, offset),
        ).fetchall()
    return jsonify(collections=[{
        "id": row["id"], "name": row["name"], "visibility": row["visibility"],
        "video_count": row["video_count"], "is_mine": row["owner_id"] == actor.principal_id,
    } for row in rows], total=total, has_more=offset+len(rows)<total, next_offset=offset+len(rows))


@bp.post("/api/mytube/collections")
@require_profile()
def create_collection():
    actor, name, error = _actor()
    if error:
        return error
    data = request.get_json(silent=True) or {}; title = _clean(data.get("name"), 100)
    visibility = str(data.get("visibility") or "shared").strip().casefold()
    if not title:
        return jsonify(error="Give the collection a name."), 422
    if visibility not in {"shared", "private"}:
        return jsonify(error="Choose Shared or Only me."), 422
    collection_id = uuid.uuid4().hex; timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute(
            """INSERT INTO mytube_collections(id,name,owner_id,owner_name,visibility,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (collection_id, title, actor.principal_id, name, visibility, timestamp, timestamp),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_collections WHERE id=?", (collection_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_collection",
            object_id=collection_id, action="create", before=None, after=saved,
        )
    return jsonify(id=collection_id, name=title, visibility=visibility, video_count=0, is_mine=True), 201


@bp.put("/api/mytube/collections/<collection_id>/videos/<video_id>")
@require_profile()
def add_collection_video(collection_id, video_id):
    actor, _name, error = _actor()
    if error:
        return error
    video = _video(video_id, actor)
    if not video:
        return jsonify(error="Video not found."), 404
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        collection = connection.execute(
            "SELECT * FROM mytube_collections WHERE id=? AND deleted_at IS NULL", (collection_id,),
        ).fetchone()
        if not collection or collection["owner_id"] != actor.principal_id:
            return jsonify(error="Collection not found."), 404
        if collection["visibility"] == "shared" and video["visibility"] == "private":
            return jsonify(error="A private video cannot be added to a shared collection."), 409
        connection.execute(
            "INSERT OR IGNORE INTO mytube_collection_members(collection_id,video_id,added_at,added_by) VALUES(?,?,?,?)",
            (collection_id, video_id, utcnow(), actor.principal_id),
        )
        connection.execute(
            "UPDATE mytube_collections SET version=version+1,updated_at=? WHERE id=?", (utcnow(), collection_id),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_collections WHERE id=?", (collection_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_collection_membership",
            object_id=collection_id, action="membership_update",
            before=collection, after=saved,
        )
    return jsonify(ok=True)


@bp.delete("/api/mytube/collections/<collection_id>/videos/<video_id>")
@require_profile()
def remove_collection_video(collection_id, video_id):
    actor, _name, error = _actor()
    if error:
        return error
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        collection = _collection(connection, collection_id, actor)
        if not collection or collection["owner_id"] != actor.principal_id:
            return jsonify(error="Collection not found."), 404
        removed = connection.execute("DELETE FROM mytube_collection_members WHERE collection_id=? AND video_id=?", (collection_id, video_id)).rowcount
        if removed:
            connection.execute("UPDATE mytube_collections SET version=version+1,updated_at=? WHERE id=?", (utcnow(), collection_id))
            saved = _collection(connection, collection_id, actor)
            audit_mutation(connection, actor=actor, domain="mytube_collection_membership", object_id=collection_id,
                action="membership_update", before=collection, after=saved)
    return jsonify(ok=True, removed=bool(removed), video_unchanged=True)


@bp.get("/api/mytube/videos/<video_id>")
@require_profile()
def video_detail(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _video(video_id, actor)
    if not row:
        return jsonify(error="Video not found."), 404
    with connect(DB_PATH) as connection:
        progress = connection.execute("SELECT * FROM mytube_progress WHERE video_id=? AND owner_id=?", (video_id, actor.principal_id)).fetchone()
    return jsonify(video=_serialize(row, actor, progress))


@bp.post("/api/mytube/uploads")
@require_profile()
def reserve_upload():
    actor, name, error = _actor()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    filename = secure_filename(_clean(data.get("filename"), 240))
    title = _clean(data.get("title") or Path(filename).stem, 200)
    visibility = str(data.get("visibility") or "shared").strip().casefold()
    key = _clean(request.headers.get("Idempotency-Key") or data.get("idempotency_key"), 160)
    try:
        size = _safe_int(data.get("size"), minimum=1, maximum=MAX_UPLOAD_BYTES)
    except (TypeError, ValueError):
        return jsonify(error="Choose a video within the upload size limit."), 422
    expected_sha = str(data.get("sha256") or "").strip().casefold()
    if not filename or Path(filename).suffix.casefold() not in ALLOWED_SUFFIXES or not title:
        return jsonify(error="Choose a supported video file."), 422
    if visibility not in {"shared", "private"}:
        return jsonify(error="Choose Shared or Only me."), 422
    if not key or len(key) < 8 or not SHA256.fullmatch(expected_sha):
        return jsonify(error="Upload identity or checksum is invalid."), 422
    digest = _upload_digest(filename, size, expected_sha, visibility, title)
    timestamp = utcnow()
    expires = (datetime.now(timezone.utc) + timedelta(hours=UPLOAD_TTL_HOURS)).isoformat()
    upload_id = uuid.uuid4().hex
    staging_name = f"{upload_id}.part"
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM mytube_uploads WHERE owner_id=? AND idempotency_key=?", (actor.principal_id, key),
        ).fetchone()
        if existing:
            if existing["request_digest"] != digest:
                return jsonify(error="That upload key was already used for a different file."), 409
            return jsonify(upload_id=existing["id"], offset=existing["offset"], state=existing["state"], expires_at=existing["expires_at"])
        active = connection.execute(
            "SELECT COUNT(*) FROM mytube_uploads WHERE owner_id=? AND state IN ('open','finalizing') AND expires_at>?",
            (actor.principal_id, timestamp),
        ).fetchone()[0]
        if active >= MAX_ACTIVE_UPLOADS:
            return jsonify(error="Finish or cancel an active upload first."), 429
        reserved = _reserved_bytes(connection, timestamp)
        if _free_bytes() < reserved + size + MIN_FREE_BYTES:
            return jsonify(error="There is not enough protected storage for this video."), 507
        connection.execute(
            """INSERT INTO mytube_uploads(id,owner_id,owner_name,original_name,staging_name,expected_size,offset,
            expected_sha256,visibility,title,idempotency_key,request_digest,state,created_at,updated_at,expires_at)
            VALUES(?,?,?,?,?,?,0,?,?,?,?,?,'open',?,?,?)""",
            (upload_id, actor.principal_id, name, filename, staging_name, size, expected_sha, visibility, title, key, digest, timestamp, timestamp, expires),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_uploads WHERE id=?", (upload_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_upload",
            object_id=upload_id, action="reserve", before=None, after=saved,
        )
    try:
        descriptor = os.open(INCOMING / staging_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        _fsync_directory(INCOMING)
    except OSError:
        with connect(DB_PATH) as connection:
            connection.execute("UPDATE mytube_uploads SET state='failed',updated_at=? WHERE id=? AND state='open'", (utcnow(), upload_id))
        return jsonify(error="Protected upload storage is temporarily unavailable."), 503
    return jsonify(upload_id=upload_id, offset=0, state="open", expires_at=expires), 201


def _owned_upload(upload_id, actor):
    if not UPLOAD_ID.fullmatch(str(upload_id or "")):
        return None
    with connect(DB_PATH) as connection:
        return connection.execute("SELECT * FROM mytube_uploads WHERE id=? AND owner_id=?", (upload_id, actor.principal_id)).fetchone()


def _upload_expired(row) -> bool:
    try:
        return datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


@bp.get("/api/mytube/uploads/<upload_id>")
@require_profile()
def upload_status(upload_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _owned_upload(upload_id, actor)
    if not row:
        return jsonify(error="Upload not found."), 404
    return jsonify(upload_id=row["id"], offset=row["offset"], size=row["expected_size"],
        state=row["state"], video_id=row["video_id"], expired=_upload_expired(row))


@bp.patch("/api/mytube/uploads/<upload_id>")
@require_profile()
def append_upload(upload_id):
    actor, _name, error = _actor()
    if error:
        return error
    try:
        client_offset = _safe_int(request.headers.get("Upload-Offset"), maximum=MAX_UPLOAD_BYTES)
        length = _safe_int(request.content_length, minimum=1, maximum=MAX_CHUNK_BYTES)
    except (TypeError, ValueError):
        return jsonify(error="Upload chunk or offset is invalid."), 422
    row = _owned_upload(upload_id, actor)
    if not row or row["state"] != "open":
        return jsonify(error="Upload not found."), 404
    if _upload_expired(row):
        return jsonify(error="Upload reservation expired. Start it again."), 410
    if client_offset != row["offset"]:
        response = jsonify(error="Upload offset does not match.", offset=row["offset"])
        response.status_code = 409; response.headers["Upload-Offset"] = str(row["offset"])
        return response
    if row["offset"] + length > row["expected_size"]:
        return jsonify(error="Upload exceeds the reserved size."), 413
    path = safe_regular_file(INCOMING, row["staging_name"])
    data = request.get_data(cache=False)
    if len(data) != length:
        return jsonify(error="Upload chunk was incomplete."), 400
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM mytube_uploads WHERE id=? AND owner_id=?", (upload_id, actor.principal_id)).fetchone()
        if not current or current["state"] != "open" or current["offset"] != client_offset:
            actual = current["offset"] if current else 0
            response = jsonify(error="Upload offset changed.", offset=actual); response.status_code = 409
            response.headers["Upload-Offset"] = str(actual); return response
        reserved = _reserved_bytes(connection, utcnow(), excluding=upload_id)
        current_remaining = int(current["expected_size"]) - int(current["offset"])
        if _free_bytes() < reserved + current_remaining + MIN_FREE_BYTES:
            return jsonify(error="Protected storage can no longer safely accept this upload."), 507
        with path.open("r+b", buffering=0) as destination:
            destination.seek(client_offset); destination.write(data); destination.flush(); os.fsync(destination.fileno())
        new_offset = client_offset + length
        connection.execute("UPDATE mytube_uploads SET offset=?,updated_at=? WHERE id=?", (new_offset, utcnow(), upload_id))
        saved = connection.execute(
            "SELECT * FROM mytube_uploads WHERE id=?", (upload_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_upload",
            object_id=upload_id, action="append", before=current, after=saved,
        )
    response = jsonify(upload_id=upload_id, offset=new_offset, complete=new_offset == row["expected_size"])
    response.headers["Upload-Offset"] = str(new_offset)
    return response


@bp.post("/api/mytube/uploads/<upload_id>/finalize")
@require_profile()
def finalize_upload(upload_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _owned_upload(upload_id, actor)
    if not row:
        return jsonify(error="Upload not found."), 404
    if row["state"] == "complete":
        return jsonify(ok=True, video_id=row["video_id"], state="complete")
    if row["state"] == "finalizing":
        recover_incomplete_uploads()
        refreshed = _owned_upload(upload_id, actor)
        if refreshed and refreshed["state"] == "complete":
            return jsonify(ok=True, video_id=refreshed["video_id"], state="complete")
        return jsonify(error="Video finalization is still being recovered."), 409
    if row["state"] != "open" or row["offset"] != row["expected_size"]:
        return jsonify(error="Upload is not complete.", offset=row["offset"]), 409
    if _upload_expired(row):
        return jsonify(error="Upload reservation expired. Start it again."), 410
    path = safe_regular_file(INCOMING, row["staging_name"])
    if path.stat().st_size != row["expected_size"]:
        return jsonify(error="Uploaded bytes do not match the reservation."), 409
    digest = _stream_sha256(path)
    if digest != row["expected_sha256"]:
        with connect(DB_PATH) as connection:
            connection.execute("UPDATE mytube_uploads SET state='failed',updated_at=? WHERE id=?", (utcnow(), upload_id))
        return jsonify(error="Video checksum verification failed."), 422
    try:
        metadata = _probe(path)
    except ValueError as probe_error:
        return jsonify(error=str(probe_error)), 422
    video_id = uuid.uuid4().hex; timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT * FROM mytube_uploads WHERE id=? AND owner_id=?", (upload_id, actor.principal_id)).fetchone()
        if current["state"] == "complete":
            return jsonify(ok=True, video_id=current["video_id"], state="complete")
        if current["state"] != "open" or current["offset"] != current["expected_size"]:
            return jsonify(error="Upload state changed."), 409
        connection.execute(
            "UPDATE mytube_uploads SET state='finalizing',video_id=?,updated_at=? WHERE id=?",
            (video_id, timestamp, upload_id),
        )
    journaled = _owned_upload(upload_id, actor)
    _finalize_reserved_file(journaled, path, metadata, digest)
    return jsonify(ok=True, video_id=video_id, state="complete"), 201


@bp.delete("/api/mytube/uploads/<upload_id>")
@require_profile()
def cancel_upload(upload_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _owned_upload(upload_id, actor)
    if not row or row["state"] not in {"open", "failed", "cancelled"}:
        return jsonify(error="Upload not found."), 404
    if row["state"] != "cancelled":
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM mytube_uploads WHERE id=? AND owner_id=?",
                (upload_id, actor.principal_id),
            ).fetchone()
            connection.execute("UPDATE mytube_uploads SET state='cancelled',updated_at=? WHERE id=?", (utcnow(), upload_id))
            saved = connection.execute(
                "SELECT * FROM mytube_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            audit_mutation(
                connection, actor=actor, domain="mytube_upload",
                object_id=upload_id, action="cancel", before=current, after=saved,
            )
        _remove_staging(INCOMING / row["staging_name"])
    return jsonify(ok=True)


@bp.route("/api/mytube/videos/<video_id>/stream", methods=["GET", "HEAD"])
@require_profile()
def stream_video(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _video(video_id, actor)
    if not row:
        return jsonify(error="Video not found."), 404
    try:
        path = _playback_path(row)
    except FileNotFoundError:
        return jsonify(error="Video source is unavailable."), 404
    return ranged_response(path, row["content_type"] or "video/mp4", etag=row["sha256"])


def _hls_root(row):
    generation = str(row["hls_generation"] or "")
    if not generation or Path(generation).name != generation:
        raise FileNotFoundError
    path = STREAMING / row["id"] / generation
    if path.is_symlink() or not path.is_dir():
        raise FileNotFoundError
    return path


@bp.get("/api/mytube/videos/<video_id>/hls/<name>")
@require_profile()
def hls_video(video_id, name):
    actor, _name, error = _actor()
    if error:
        return error
    row = _video(video_id, actor)
    if not row or row["playback_mode"] != "hls":
        return jsonify(error="Prepared playback is not ready."), 404
    try:
        root = _hls_root(row)
    except FileNotFoundError:
        return jsonify(error="Prepared playback is not ready."), 404
    return hls_file(root, name, playlist=name.endswith(".m3u8"))


@bp.get("/api/mytube/videos/<video_id>/poster")
@require_profile()
def poster(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _video(video_id, actor)
    if not row or not row["poster_name"]:
        return jsonify(error="Poster not found."), 404
    try:
        path = safe_regular_file(POSTERS, row["poster_name"])
    except FileNotFoundError:
        return jsonify(error="Poster not found."), 404
    response = ranged_response(path, "image/webp", etag=f"{row['sha256']}-poster")
    response.headers["Cache-Control"] = "private, max-age=604800"
    return response


@bp.put("/api/mytube/videos/<video_id>/progress")
@require_profile()
def save_progress(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    row = _video(video_id, actor)
    data = request.get_json(silent=True) or {}
    if not row:
        return jsonify(error="Video not found."), 404
    try:
        position = float(data.get("position_seconds", 0))
        if not math.isfinite(position):
            raise ValueError
        duration = max(float(row["duration_seconds"] or 0), 0)
        position = max(0.0, min(position, duration + 30 if duration else 31 * 86400))
    except (TypeError, ValueError, OverflowError):
        return jsonify(error="Playback position is invalid."), 422
    completed = bool(data.get("completed")); timestamp = utcnow()
    with connect(DB_PATH) as connection:
        before = connection.execute(
            "SELECT * FROM mytube_progress WHERE video_id=? AND owner_id=?",
            (video_id, actor.principal_id),
        ).fetchone()
        connection.execute(
            """INSERT INTO mytube_progress(video_id,owner_id,position_seconds,completed,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(video_id,owner_id) DO UPDATE SET position_seconds=excluded.position_seconds,
            completed=excluded.completed,updated_at=excluded.updated_at""",
            (video_id, actor.principal_id, position, int(completed), timestamp),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_progress WHERE video_id=? AND owner_id=?",
            (video_id, actor.principal_id),
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_progress",
            object_id=f"{video_id}:{actor.principal_id}", action="update",
            before=before, after=saved,
        )
    return jsonify(ok=True, position_seconds=position, completed=completed)


@bp.delete("/api/mytube/videos/<video_id>")
@require_profile()
def trash_video(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    timestamp = utcnow(); purge_after = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM mytube_videos WHERE id=? AND deleted_at IS NULL", (video_id,)).fetchone()
        if not row or row["owner_id"] != actor.principal_id:
            return jsonify(error="Video not found."), 404
        if row["source_kind"] == "media":
            return jsonify(error="Remove this link from Media; the original video will stay untouched."), 409
        connection.execute(
            "UPDATE mytube_videos SET deleted_at=?,deleted_by=?,purge_after=?,version=version+1,updated_at=? WHERE id=?",
            (timestamp, actor.principal_id, purge_after, timestamp, video_id),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_videos WHERE id=?", (video_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_video",
            object_id=video_id, action="trash", before=row, after=saved,
        )
    return jsonify(ok=True, recoverable_until=purge_after)


@bp.post("/api/mytube/videos/<video_id>/restore")
@require_profile()
def restore_video(video_id):
    actor, _name, error = _actor()
    if error:
        return error
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM mytube_videos WHERE id=? AND deleted_at IS NOT NULL", (video_id,)).fetchone()
        if not row or row["owner_id"] != actor.principal_id:
            return jsonify(error="Video not found."), 404
        connection.execute(
            "UPDATE mytube_videos SET deleted_at=NULL,deleted_by=NULL,purge_after=NULL,version=version+1,updated_at=? WHERE id=?",
            (utcnow(), video_id),
        )
        saved = connection.execute(
            "SELECT * FROM mytube_videos WHERE id=?", (video_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="mytube_video",
            object_id=video_id, action="restore", before=row, after=saved,
        )
    return jsonify(ok=True)


def register_media_reference(reference: MediaReference) -> str:
    """Idempotently refresh the rebuildable MyTube side of a committed media link."""
    ensure_storage(); DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700); migrate(DB_PATH, initialize_mytube)
    if not VIDEO_ID.fullmatch(reference.video_id) or not VIDEO_ID.fullmatch(reference.media_id):
        raise ValueError("Media reference identifiers are invalid.")
    if reference.visibility not in {"shared", "private"} or not SHA256.fullmatch(reference.sha256):
        raise ValueError("Media reference metadata is invalid.")
    timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT id FROM mytube_videos WHERE media_id=?", (reference.media_id,)).fetchone()
        if existing and existing["id"] != reference.video_id:
            raise sqlite3.IntegrityError("Media is already cataloged in MyTube.")
        connection.execute(
            """INSERT INTO mytube_videos(id,title,source_kind,stored_name,media_id,content_type,byte_size,sha256,
            duration_seconds,owner_id,owner_name,visibility,created_at,updated_at)
            VALUES(?,?,'media',NULL,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,content_type=excluded.content_type,
            byte_size=excluded.byte_size,sha256=excluded.sha256,
            duration_seconds=CASE WHEN excluded.duration_seconds>0 THEN excluded.duration_seconds ELSE mytube_videos.duration_seconds END,
            owner_id=excluded.owner_id,owner_name=excluded.owner_name,visibility=excluded.visibility,updated_at=excluded.updated_at
            WHERE (mytube_videos.title,mytube_videos.content_type,mytube_videos.byte_size,mytube_videos.sha256,
                   mytube_videos.owner_id,mytube_videos.owner_name,mytube_videos.visibility)
              IS NOT (excluded.title,excluded.content_type,excluded.byte_size,excluded.sha256,excluded.owner_id,excluded.owner_name,excluded.visibility)
              OR (excluded.duration_seconds>0 AND mytube_videos.duration_seconds<>excluded.duration_seconds)""",
            (reference.video_id, reference.title, reference.media_id, reference.content_type, reference.byte_size,
             reference.sha256, reference.duration_seconds, reference.owner_id, reference.owner_name,
             reference.visibility, timestamp, timestamp),
        )
    return reference.video_id


def reconcile_media_references(references) -> None:
    """Atomically converge the rebuildable catalog to photos.db's authoritative links."""
    items = tuple(references)
    for reference in items:
        if (
            not VIDEO_ID.fullmatch(reference.video_id)
            or not VIDEO_ID.fullmatch(reference.media_id)
            or reference.visibility not in {"shared", "private"}
            or not SHA256.fullmatch(reference.sha256)
        ):
            raise ValueError("Media reference metadata is invalid.")
    timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for reference in items:
            existing = connection.execute(
                "SELECT id FROM mytube_videos WHERE media_id=?", (reference.media_id,),
            ).fetchone()
            if existing and existing["id"] != reference.video_id:
                connection.execute(
                    "DELETE FROM mytube_videos WHERE media_id=? AND source_kind='media'",
                    (reference.media_id,),
                )
            connection.execute(
                """INSERT INTO mytube_videos(id,title,source_kind,stored_name,media_id,content_type,byte_size,sha256,
                duration_seconds,owner_id,owner_name,visibility,created_at,updated_at)
                VALUES(?,?,'media',NULL,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET title=excluded.title,content_type=excluded.content_type,
                byte_size=excluded.byte_size,sha256=excluded.sha256,
                duration_seconds=CASE WHEN excluded.duration_seconds>0 THEN excluded.duration_seconds ELSE mytube_videos.duration_seconds END,
                owner_id=excluded.owner_id,owner_name=excluded.owner_name,visibility=excluded.visibility,
                deleted_at=NULL,deleted_by=NULL,purge_after=NULL,updated_at=excluded.updated_at
                WHERE (mytube_videos.title,mytube_videos.content_type,mytube_videos.byte_size,mytube_videos.sha256,
                       mytube_videos.owner_id,mytube_videos.owner_name,mytube_videos.visibility)
                  IS NOT (excluded.title,excluded.content_type,excluded.byte_size,excluded.sha256,excluded.owner_id,excluded.owner_name,excluded.visibility)
                  OR (excluded.duration_seconds>0 AND mytube_videos.duration_seconds<>excluded.duration_seconds)
                  OR mytube_videos.deleted_at IS NOT NULL""",
                (reference.video_id, reference.title, reference.media_id, reference.content_type,
                 reference.byte_size, reference.sha256, reference.duration_seconds, reference.owner_id,
                 reference.owner_name, reference.visibility, timestamp, timestamp),
            )
        media_ids = [reference.media_id for reference in items]
        if media_ids:
            placeholders = ",".join("?" for _ in media_ids)
            connection.execute(
                f"DELETE FROM mytube_videos WHERE source_kind='media' AND media_id NOT IN ({placeholders})",
                media_ids,
            )
        else:
            connection.execute("DELETE FROM mytube_videos WHERE source_kind='media'")


def media_reference_payload(reference: MediaReference) -> dict:
    return asdict(reference)


def remove_media_reference(media_id: str, video_id: str) -> None:
    """Remove only the rebuildable projection; never touch the Media original."""
    if not VIDEO_ID.fullmatch(str(video_id or "")):
        raise ValueError("Media reference identifier is invalid.")
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM mytube_videos WHERE id=? AND media_id=? AND source_kind='media'",
            (video_id, media_id),
        )
