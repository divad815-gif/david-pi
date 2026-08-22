import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename

register_heif_opener()

DATA = Path(os.environ.get("PHOTO_DATA", "/data"))
ORIGINALS = DATA / "originals"
PREVIEWS = DATA / "previews"
VIEWER_PREVIEWS = DATA / "viewer-previews"
THUMBS = DATA / "thumbs"
INCOMING = DATA / "incoming"
QUARANTINE = DATA / "quarantine"
DB_PATH = DATA / "photos.db"
METRICS_DB = DATA / "metrics.db"
MUSIC_LIBRARY = Path(os.environ.get("DAVID_PI_MUSIC_LIBRARY", Path(__file__).parent / "assets" / "music"))
HOST_PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
HOST_SYS = Path(os.environ.get("HOST_SYS", "/host/sys"))
PIHOLE_SUMMARY = Path(os.environ.get("PIHOLE_SUMMARY", "/run/david-pi/pihole-summary.json"))
BACKUP_STATUS = Path(os.environ.get("DAVID_PI_BACKUP_STATUS", "/run/david-pi/backup-status.json"))
SERVER_STATUS = Path(os.environ.get("DAVID_PI_SERVER_STATUS", "/run/david-pi/server-status.json"))
IMAGE_ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".tif", ".tiff"}
VIDEO_ALLOWED = {".mp4", ".mov", ".m4v"}
ALLOWED = IMAGE_ALLOWED | VIDEO_ALLOWED

for directory in (
    DATA, ORIGINALS, PREVIEWS, VIEWER_PREVIEWS, THUMBS, INCOMING, QUARANTINE,
):
    directory.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024
MAX_MEDIA_FILES = 200
MAX_MEDIA_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
SHUTDOWN_REQUEST = DATA / "platform" / "control" / "shutdown.request"
SHUTDOWN_PASSWORD_ENV = "DAVID_PI_SHUTDOWN_PASSWORD_HASH_B64"
SHUTDOWN_FAILURE_LIMIT = 5
SHUTDOWN_FAILURE_WINDOW_SECONDS = 15 * 60

from modules.security import init_security
from modules.identity import current_device, init_identity
from modules.files import init_files
from modules.movies import init_movies
from modules.notes import init_notes, purge_expired_notes
from modules.recipes import init_recipes
from modules.places import init_places
from modules.audiobooks import init_audiobooks
from modules.assistant import init_assistant, seed_knowledge
from modules.games import init_games

init_security(app)
init_identity(app)
init_files(app)
init_notes(app)
init_movies(app)
init_recipes(app)
init_places(app)
init_audiobooks(app)
init_games(app)


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_once():
    with db() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS photos (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                preview_name TEXT NOT NULL,
                thumb_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                sha256 TEXT NOT NULL UNIQUE,
                taken_at TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                uploaded_by TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS photos_taken_idx ON photos(taken_at DESC)")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(photos)").fetchall()}
        if "deleted_at" not in columns:
            try:
                connection.execute("ALTER TABLE photos ADD COLUMN deleted_at TEXT")
            except sqlite3.OperationalError:
                if "deleted_at" not in {row[1] for row in connection.execute("PRAGMA table_info(photos)").fetchall()}:
                    raise
        if "playback_name" not in columns:
            try:
                connection.execute("ALTER TABLE photos ADD COLUMN playback_name TEXT")
            except sqlite3.OperationalError:
                if "playback_name" not in {row[1] for row in connection.execute("PRAGMA table_info(photos)").fetchall()}:
                    raise
        if "loop_playback" not in columns:
            try:
                connection.execute("ALTER TABLE photos ADD COLUMN loop_playback INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                if "loop_playback" not in {row[1] for row in connection.execute("PRAGMA table_info(photos)").fetchall()}:
                    raise
        for name, definition in (
            ("owner_id", "TEXT"),
            ("owner_name", "TEXT"),
            ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
            ("content_sha256", "TEXT"),
            ("source_device_id", "TEXT"),
            ("ingestion_source", "TEXT NOT NULL DEFAULT 'manual_upload'"),
            ("capture_timestamp", "TEXT"),
            ("primary_verification_state", "TEXT NOT NULL DEFAULT 'primary_verified'"),
            ("secondary_verification_state", "TEXT NOT NULL DEFAULT 'secondary_pending'"),
        ):
            if name not in columns:
                try:
                    connection.execute(f"ALTER TABLE photos ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError:
                    current = {row[1] for row in connection.execute("PRAGMA table_info(photos)")}
                    if name not in current:
                        raise
        connection.execute("CREATE INDEX IF NOT EXISTS photos_visibility_idx ON photos(visibility, owner_id)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS photos_content_sha_idx ON photos(content_sha256, byte_size)"
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS photos_shared_gallery_idx
               ON photos(visibility, deleted_at, taken_at DESC, uploaded_at DESC, id DESC)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS photos_owner_gallery_idx
               ON photos(owner_id, deleted_at, taken_at DESC, uploaded_at DESC, id DESC)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS photos_shared_capture_gallery_idx
               ON photos(visibility, deleted_at, capture_timestamp DESC,
                         uploaded_at DESC, id DESC)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS photos_owner_capture_gallery_idx
               ON photos(owner_id, deleted_at, capture_timestamp DESC,
                         uploaded_at DESC, id DESC)"""
        )
        connection.execute(
            "UPDATE photos SET content_sha256=sha256 WHERE content_sha256 IS NULL"
        )
        connection.execute(
            "UPDATE photos SET capture_timestamp=taken_at WHERE capture_timestamp IS NULL"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            )
            """
        )
        collection_columns = {row[1] for row in connection.execute("PRAGMA table_info(collections)")}
        for name, definition in (
            ("owner_id", "TEXT"),
            ("owner_name", "TEXT"),
            ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
        ):
            if name not in collection_columns:
                try:
                    connection.execute(f"ALTER TABLE collections ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError:
                    current = {row[1] for row in connection.execute("PRAGMA table_info(collections)")}
                    if name not in current:
                        raise
        connection.execute("CREATE INDEX IF NOT EXISTS collections_visibility_idx ON collections(visibility, owner_id)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS collection_photos (
                collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                added_at TEXT NOT NULL,
                PRIMARY KEY (collection_id, photo_id)
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS collection_photo_idx ON collection_photos(photo_id)")
        video_count = connection.execute(
            "SELECT COUNT(*) FROM photos WHERE deleted_at IS NULL AND content_type LIKE 'video/%'"
        ).fetchone()[0]
        if video_count:
            videos = connection.execute(
                "SELECT id FROM collections WHERE lower(name)='videos' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if videos:
                videos_id = videos["id"]
            else:
                videos_id = uuid.uuid4().hex
                created_at = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """INSERT INTO collections
                       (id,name,created_at,created_by,owner_id,owner_name,visibility)
                       VALUES (?,?,?,?,?,?,?)""",
                    (videos_id, "Videos", created_at, "David-Pi", None, "David-Pi", "shared"),
                )
            connection.execute(
                """INSERT OR IGNORE INTO collection_photos (collection_id,photo_id,added_at)
                   SELECT ?,id,COALESCE(uploaded_at,?) FROM photos
                   WHERE deleted_at IS NULL AND content_type LIKE 'video/%'""",
                (videos_id, datetime.now(timezone.utc).isoformat()),
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slideshow_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL,
                message TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                result_photo_id TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(slideshow_jobs)")}
        for name, definition in (
            ("owner_id", "TEXT"),
            ("owner_name", "TEXT"),
            ("visibility", "TEXT NOT NULL DEFAULT 'shared'"),
        ):
            if name not in job_columns:
                try:
                    connection.execute(f"ALTER TABLE slideshow_jobs ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError:
                    current = {row[1] for row in connection.execute("PRAGMA table_info(slideshow_jobs)")}
                    if name not in current:
                        raise


def initialize():
    for attempt in range(10):
        try:
            initialize_once()
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


initialize()


@contextmanager
def metrics_db():
    connection = sqlite3.connect(METRICS_DB, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_metrics():
    with metrics_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS system_metrics ("
            "timestamp INTEGER PRIMARY KEY, cpu REAL NOT NULL, memory REAL NOT NULL, "
            "temperature REAL, disk_used REAL NOT NULL, load1 REAL NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS metrics_time_idx ON system_metrics(timestamp)")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(system_metrics)")}
        for name in (
            "swap", "root_disk_used", "container_memory", "health_latency",
            "backup_state", "queue_depth",
        ):
            if name not in columns:
                try:
                    connection.execute(f"ALTER TABLE system_metrics ADD COLUMN {name} REAL")
                except sqlite3.OperationalError:
                    current = {row[1] for row in connection.execute("PRAGMA table_info(system_metrics)")}
                    if name not in current:
                        raise
        connection.execute(
            "CREATE TABLE IF NOT EXISTS shutdown_auth_attempts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, attempted_at INTEGER NOT NULL, "
            "succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)))"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS shutdown_auth_time_idx "
            "ON shutdown_auth_attempts(attempted_at)"
        )


initialize_metrics()


def read_text(path, fallback=""):
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return fallback


def cpu_times():
    values = read_text(HOST_PROC / "stat").splitlines()[0].split()[1:]
    numbers = [int(value) for value in values]
    idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
    return sum(numbers), idle


def system_snapshot(interval=0.12):
    status = server_status_snapshot()
    cards = status["subsystems"]
    portal = cards.get("portal", {}).get("details", {})
    storage = cards.get("storage", {}).get("details", {})
    external = storage.get("external", {})
    power = cards.get("temperature_power", {}).get("details", {})
    load = power.get("load_average") or [0, 0, 0]
    memory_total = float(power.get("ram_total_gb") or 0)
    memory_available = float(power.get("ram_available_gb") or 0)
    memory_used = 100 * (1 - memory_available / max(memory_total, 0.001))
    def percent(value):
        try:
            return float(str(value).replace("%", "").strip())
        except (TypeError, ValueError):
            return 0.0
    return {
        "timestamp": int(time.time()),
        "cpu": round(percent(portal.get("cpu_percent")), 1),
        "memory": round(memory_used, 1),
        "memory_used_gb": round(memory_total - memory_available, 2),
        "memory_total_gb": round(memory_total, 2),
        "temperature": power.get("temperature_c"),
        "disk_used": float(external.get("used_percent") or 0),
        "disk_used_gb": float(external.get("used_gb") or 0),
        "disk_total_gb": float(external.get("total_gb") or 0),
        "disk_free_gb": float(external.get("free_gb") or 0),
        "load1": round(float(load[0]), 2),
        "load5": round(float(load[1]), 2),
        "load15": round(float(load[2]), 2),
        "uptime": int(portal.get("uptime_seconds") or 0),
    }


def server_status_snapshot():
    if not SERVER_STATUS.is_file() or SERVER_STATUS.stat().st_size > 1024 * 1024:
        raise FileNotFoundError("Sanitized server status is unavailable.")
    payload = json.loads(SERVER_STATUS.read_text(encoding="utf-8"))
    required = {"schema_version", "generated_at", "state", "subsystems", "databases", "privacy"}
    if not isinstance(payload, dict) or not required.issubset(payload) or payload.get("schema_version") != 1:
        raise ValueError("Unsupported server status schema.")
    if payload.get("privacy") != {
        "contains_personal_filenames": False, "contains_domains": False,
        "contains_clients": False, "contains_secrets": False,
    }:
        raise ValueError("Server status privacy declaration failed.")
    allowed_cards = {
        "portal", "external_drive", "storage", "backups", "temperature_power",
        "tailscale", "pihole", "background_jobs", "services", "updates",
    }
    if set(payload["subsystems"]) - allowed_cards:
        raise ValueError("Server status contains unsupported subsystems.")
    return payload


HEALTH_THRESHOLDS = {
    "cpu": (80, 95),
    "memory": (80, 92),
    "disk": (75, 90),
    "temperature": (70, 80),
}


def metric_health(value, warning, critical):
    if value is None:
        return "unknown"
    if value >= critical:
        return "critical"
    if value >= warning:
        return "warning"
    return "good"


def health_assessment(snapshot):
    states = {
        "cpu": metric_health(snapshot.get("cpu"), *HEALTH_THRESHOLDS["cpu"]),
        "memory": metric_health(snapshot.get("memory"), *HEALTH_THRESHOLDS["memory"]),
        "disk": metric_health(snapshot.get("disk_used"), *HEALTH_THRESHOLDS["disk"]),
        "temperature": metric_health(snapshot.get("temperature"), *HEALTH_THRESHOLDS["temperature"]),
    }
    if "critical" in states.values():
        overall, message = "critical", "David-Pi needs attention"
    elif "warning" in states.values():
        overall, message = "warning", "One thing needs watching"
    elif "unknown" in states.values():
        overall, message = "unknown", "Some health details are unavailable"
    else:
        overall, message = "good", "Everything looks good"
    return {"overall": overall, "message": message, "metrics": states}


def record_metrics():
    while True:
        try:
            snapshot = system_snapshot()
            history = status_history_values()
            write_metric_sample(snapshot, history)
            purge_expired_photos()
            purge_expired_notes()
            cleanup_stale_upload_parts()
        except (OSError, ValueError, sqlite3.Error, IndexError, NameError):
            pass
        time.sleep(300)


def write_metric_sample(snapshot, history, now=None):
    now = int(time.time()) if now is None else int(now)
    with metrics_db() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO system_metrics
               (timestamp,cpu,memory,temperature,disk_used,load1,swap,root_disk_used,
                container_memory,health_latency,backup_state,queue_depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot["timestamp"], snapshot["cpu"], snapshot["memory"],
                snapshot["temperature"], snapshot["disk_used"], snapshot["load1"],
                history.get("swap"), history.get("root_disk_used"),
                history.get("container_memory"), history.get("health_latency"),
                history.get("backup_state"), history.get("queue_depth"),
            ),
        )
        connection.execute("DELETE FROM system_metrics WHERE timestamp < ?", (now - 30 * 86400,))


def status_history_values():
    try:
        cards = server_status_snapshot()["subsystems"]
        power = cards["temperature_power"]["details"]
        storage = cards["storage"]["details"]
        portal = cards["portal"]["details"]
        jobs = cards["background_jobs"]["details"]["slideshows"]
        backups = cards["backups"]
        swap_total = float(power.get("swap_total_gb") or 0)
        swap_free = float(power.get("swap_free_gb") or 0)
        def percent(value):
            try:
                return float(str(value).replace("%", "").strip())
            except (TypeError, ValueError):
                return None
        return {
            "swap": round(100 * (swap_total - swap_free) / max(swap_total, 0.001), 1) if swap_total else 0,
            "root_disk_used": storage["microsd"].get("used_percent"),
            "container_memory": percent(portal.get("memory_percent")),
            "health_latency": portal.get("health_latency_ms"),
            "backup_state": {"healthy": 0, "warning": 1, "critical": 2, "unavailable": 3}.get(backups.get("state"), 3),
            "queue_depth": int(jobs.get("active", 0)) + int(jobs.get("pending", 0)),
        }
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return {}


def cleanup_stale_upload_parts(now=None, minimum_age=24 * 3600):
    """Remove only abandoned `.part` files; active recent uploads are untouched."""
    now = time.time() if now is None else now
    removed = 0
    for root in (INCOMING, DATA / "tmp" / "uploads", DATA / "files" / "incoming"):
        if not root.is_dir():
            continue
        for path in root.glob("*.part"):
            try:
                if path.is_file() and not path.is_symlink() and now - path.stat().st_mtime > minimum_age:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


if os.environ.get("DAVID_PI_DISABLE_METRICS") != "1":
    threading.Thread(target=record_metrics, daemon=True).start()


def identity():
    return current_device()["name"]


def visibility_sql(alias=""):
    prefix = f"{alias}." if alias else ""
    owner_id = current_device()["owner_id"]
    if owner_id:
        return f"({prefix}visibility = 'shared' OR {prefix}owner_id = ?)", [owner_id]
    return f"{prefix}visibility = 'shared'", []


def requested_visibility(value):
    value = str(value or "shared").lower()
    if value not in ("shared", "private"):
        raise ValueError("Choose Shared or Only me.")
    if value == "private" and not current_device()["owner_id"]:
        raise ValueError("Open David-Pi through its private Tailscale address.")
    return value


def exif_time(image, fallback):
    try:
        exif = image.getexif()
        value = exif.get(36867) or exif.get(306)
        if value:
            return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        pass
    return fallback


def save_jpeg(image, path, max_size, quality):
    converted = ImageOps.exif_transpose(image)
    converted.thumbnail(max_size, Image.Resampling.LANCZOS)
    if converted.mode not in ("RGB", "L"):
        background = Image.new("RGB", converted.size, "white")
        if "A" in converted.getbands():
            background.paste(converted, mask=converted.getchannel("A"))
        else:
            background.paste(converted)
        converted = background
    elif converted.mode == "L":
        converted = converted.convert("RGB")
    converted.save(path, "JPEG", quality=quality, optimize=True)


def viewer_preview_name(preview_name):
    """Return the immutable phone-viewer derivative for a physical preview."""
    return f"{Path(str(preview_name)).stem}.webp"


def ensure_viewer_preview(preview_name):
    """Create a bounded WebP viewer copy without changing the original or preview."""
    source = managed_path(PREVIEWS, preview_name)
    destination = managed_path(VIEWER_PREVIEWS, viewer_preview_name(preview_name))
    if destination.is_file() and not destination.is_symlink() and destination.stat().st_size:
        return destination
    if not source.is_file() or source.is_symlink():
        raise FileNotFoundError("The media preview is unavailable.")
    temporary = VIEWER_PREVIEWS / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with Image.open(source) as image:
            image.load()
            converted = ImageOps.exif_transpose(image)
            converted.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if converted.mode not in ("RGB", "L"):
                background = Image.new("RGB", converted.size, "white")
                if "A" in converted.getbands():
                    background.paste(converted, mask=converted.getchannel("A"))
                else:
                    background.paste(converted)
                converted = background
            elif converted.mode == "L":
                converted = converted.convert("RGB")
            converted.save(temporary, "WEBP", quality=80, method=4)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def backfill_viewer_previews(limit=None):
    """Warm missing viewer derivatives; safe to resume after interruption."""
    with db() as connection:
        rows = connection.execute(
            """SELECT DISTINCT preview_name FROM photos
               WHERE preview_name IS NOT NULL ORDER BY preview_name"""
        ).fetchall()
    created = skipped = failed = 0
    for row in rows[:limit] if limit else rows:
        destination = VIEWER_PREVIEWS / viewer_preview_name(row["preview_name"])
        if destination.is_file() and destination.stat().st_size:
            skipped += 1
            continue
        try:
            ensure_viewer_preview(row["preview_name"])
            created += 1
        except (OSError, ValueError, UnidentifiedImageError):
            failed += 1
    return {"created": created, "skipped": skipped, "failed": failed}


def run_media_command(arguments):
    # Large HEVC originals can require more than an hour to decode and create
    # a portable H.264 derivative on a thermally constrained Raspberry Pi 4.
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=14400, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "Video processing failed.").strip().splitlines()[-1]
        raise ValueError(detail[:160])
    return result


def video_codecs(path):
    result = run_media_command([
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
        "-of", "json", str(path),
    ])
    codecs = {}
    for stream in json.loads(result.stdout).get("streams", []):
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        # Some iPhone MOV files contain metadata tracks which ffprobe labels
        # as audio but which have no codec. Do not let those overwrite the
        # real AAC track selected for playback.
        if codec_type and codec_name and codec_type not in codecs:
            codecs[codec_type] = codec_name
    return codecs


def prepare_video(path, photo_id, extension):
    preview_name = f"{photo_id}.jpg"
    thumb_name = f"{photo_id}.jpg"
    preview_path = PREVIEWS / preview_name
    run_media_command([
        # Start at the first decodable frame. Some Takeout motion-photo clips
        # are shorter than 0.1 seconds and have no frame at the old seek point.
        "ffmpeg", "-y", "-ss", "0", "-i", str(path), "-frames:v", "1",
        "-vf", "scale=2200:2200:force_original_aspect_ratio=decrease",
        "-q:v", "3", str(preview_path),
    ])
    with Image.open(preview_path) as poster:
        save_jpeg(poster.copy(), THUMBS / thumb_name, (360, 360), 78)

    codecs = video_codecs(path)
    compatible = codecs.get("video") == "h264" and codecs.get("audio") in (None, "aac", "mp3")
    if extension == ".mp4" and compatible:
        return preview_name, thumb_name, None

    playback_name = f"{photo_id}.mp4"
    playback_path = PREVIEWS / playback_name
    if compatible:
        arguments = ["ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(playback_path)]
    else:
        arguments = [
            "ffmpeg", "-y", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
            # Rotation metadata can turn an otherwise even iPhone frame into an
            # odd scaled width (for example 959x1280). libx264 requires both
            # dimensions to be divisible by two.
            "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(playback_path),
        ]
    run_media_command(arguments)
    return preview_name, thumb_name, playback_name


def parse_capture_timestamp(value, fallback):
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return fallback


def canonical_ingest_media(
    *,
    staged_path,
    original_filename,
    mime_type,
    owner_user_id,
    owner_name,
    visibility="shared",
    capture_timestamp=None,
    source_device_id=None,
    ingestion_source="manual_upload",
    authoritative_sha256=None,
    authoritative_size=None,
    collection_id=None,
):
    """Create one canonical logical media record, reusing physical bytes when safe.

    Browser uploads and device backups both call this function. Physical-object
    deduplication is intentionally independent from logical ownership.
    """
    original_name = secure_filename(original_filename or "media") or "media"
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED:
        raise ValueError("Unsupported photo or video format.")
    now = datetime.now(timezone.utc)
    staged = Path(staged_path) if staged_path is not None else None
    if staged is not None:
        if not staged.is_file() or staged.is_symlink():
            raise ValueError("The staged upload is unavailable.")
        size = staged.stat().st_size
        if size <= 0 or size > MAX_MEDIA_FILE_BYTES:
            raise ValueError("The media file is outside the supported size limit.")
        digest = hashlib.sha256()
        with staged.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()
    else:
        size = int(authoritative_size or 0)
        checksum = str(authoritative_sha256 or "").lower()
        if size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise ValueError("Verified physical media is required.")
    if authoritative_size is not None and size != int(authoritative_size):
        raise ValueError("The media size did not verify.")
    if authoritative_sha256 is not None and not hmac.compare_digest(
        checksum, str(authoritative_sha256).lower()
    ):
        raise ValueError("The media hash did not verify.")

    with db() as connection:
        physical = connection.execute(
            """SELECT * FROM photos
               WHERE COALESCE(content_sha256,sha256)=? AND byte_size=?
               ORDER BY uploaded_at LIMIT 1""",
            (checksum, size),
        ).fetchone()

    photo_id = uuid.uuid4().hex
    destination = None
    created_derivatives = []
    if physical:
        stored_path = physical["stored_path"]
        preview_name = physical["preview_name"]
        thumb_name = physical["thumb_name"]
        playback_name = physical["playback_name"]
        content_type = physical["content_type"]
        taken = parse_capture_timestamp(capture_timestamp, datetime.fromisoformat(physical["taken_at"]))
        if staged is not None:
            staged.unlink(missing_ok=True)
    else:
        if staged is None:
            raise ValueError("The verified physical object could not be found.")
        free_space = shutil.disk_usage(DATA).free
        required = size * (2 if extension in VIDEO_ALLOWED else 1) + 512 * 1024 * 1024
        if free_space < required:
            raise ValueError("David-Pi does not have enough free space.")
        playback_name = None
        if extension in VIDEO_ALLOWED:
            taken = parse_capture_timestamp(capture_timestamp, now)
            try:
                preview_name, thumb_name, playback_name = prepare_video(
                    staged, photo_id, extension
                )
            except Exception:
                for derivative in (
                    PREVIEWS / f"{photo_id}.jpg",
                    PREVIEWS / f"{photo_id}.mp4",
                    THUMBS / f"{photo_id}.jpg",
                ):
                    derivative.unlink(missing_ok=True)
                raise
            created_derivatives.extend([PREVIEWS / preview_name, THUMBS / thumb_name])
            if playback_name:
                created_derivatives.append(PREVIEWS / playback_name)
            content_type = "video/mp4" if extension in (".mp4", ".m4v") else "video/quicktime"
        else:
            with Image.open(staged) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("This image has too many pixels to process safely.")
                image.load()
                taken = parse_capture_timestamp(capture_timestamp, exif_time(image, now))
                preview_name = f"{photo_id}.jpg"
                thumb_name = f"{photo_id}.jpg"
                save_jpeg(image.copy(), PREVIEWS / preview_name, (2200, 2200), 88)
                save_jpeg(image.copy(), THUMBS / thumb_name, (360, 360), 78)
                created_derivatives.extend([PREVIEWS / preview_name, THUMBS / thumb_name])
            content_type = mime_type or "application/octet-stream"
        dated = taken.astimezone(timezone.utc)
        relative = Path(f"{dated.year:04d}") / f"{dated.month:02d}" / f"{photo_id}{extension}"
        destination = managed_path(ORIGINALS, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, destination)
        stored_path = relative.as_posix()

    try:
        viewer_derivative = ensure_viewer_preview(preview_name)
        if not physical:
            created_derivatives.append(viewer_derivative)
    except (OSError, ValueError, UnidentifiedImageError):
        # The full preview remains a safe fallback and the resumable warmer can retry.
        pass

    # Keep the legacy unique sha256 column compatible while content_sha256 is canonical.
    legacy_hash = checksum if not physical else f"{checksum}:{photo_id}"
    try:
        with db() as connection:
            connection.execute(
                """INSERT INTO photos
                   (id,original_name,stored_path,preview_name,thumb_name,content_type,
                    byte_size,sha256,content_sha256,taken_at,capture_timestamp,uploaded_at,
                    uploaded_by,playback_name,owner_id,owner_name,visibility,source_device_id,
                    ingestion_source,primary_verification_state,secondary_verification_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    photo_id, original_name, stored_path, preview_name, thumb_name,
                    content_type, size, legacy_hash, checksum, taken.isoformat(),
                    taken.isoformat(), now.isoformat(), owner_name or "Home", playback_name,
                    owner_user_id, owner_name, visibility, source_device_id,
                    ingestion_source, "primary_verified", "secondary_pending",
                ),
            )
            if collection_id:
                connection.execute(
                    """INSERT OR IGNORE INTO collection_photos
                       (collection_id,photo_id,added_at) VALUES (?,?,?)""",
                    (collection_id, photo_id, now.isoformat()),
                )
            if content_type.startswith("video/"):
                videos = connection.execute(
                    "SELECT id FROM collections WHERE lower(name)='videos' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if videos:
                    videos_id = videos["id"]
                else:
                    videos_id = uuid.uuid4().hex
                    connection.execute(
                        """INSERT INTO collections
                           (id,name,created_at,created_by,owner_id,owner_name,visibility)
                           VALUES (?,?,?,?,?,?,?)""",
                        (videos_id, "Videos", now.isoformat(), "David-Pi", None, "David-Pi", "shared"),
                    )
                connection.execute(
                    """INSERT OR IGNORE INTO collection_photos
                       (collection_id,photo_id,added_at) VALUES (?,?,?)""",
                    (videos_id, photo_id, now.isoformat()),
                )
    except Exception:
        if not physical:
            if destination is not None:
                destination.unlink(missing_ok=True)
            for derivative in created_derivatives:
                derivative.unlink(missing_ok=True)
        raise
    return {
        "id": photo_id, "name": original_name, "content_sha256": checksum,
        "physical_reused": bool(physical),
    }


def rollback_canonical_ingest(photo_id):
    """Compensate a failed outer transaction without leaving logical/physical orphans."""
    with db() as connection:
        changed = connection.execute(
            "UPDATE photos SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), photo_id),
        )
    if changed.rowcount:
        destroy_photo(photo_id)


from modules.device_backup import init_device_backup

init_device_backup(app, db, canonical_ingest_media, rollback_canonical_ingest, DATA)

from modules.chat import init_chat

init_chat(app, canonical_ingest_media)


@app.get("/")
def home():
    return render_template("home.html", person=identity())


@app.get("/photos")
def photos_page():
    visible, parameters = visibility_sql("c")
    with db() as connection:
        organizer_collections = connection.execute(
            f"SELECT c.id, c.name FROM collections c WHERE {visible} ORDER BY lower(c.name)",
            parameters,
        ).fetchall()
    return render_template(
        "photos.html", person=identity(),
        organizer_collections=[dict(row) for row in organizer_collections],
    )


@app.get("/status")
def status_page():
    return render_template("status.html", person=identity())


@app.get("/assistant")
def assistant_page():
    return render_template("assistant.html", person=identity())


@app.get("/games")
def games_page():
    return render_template("games.html", person=identity())


@app.get("/david-pi-icon-<int:size>.png")
def david_pi_icon(size):
    if size not in (32, 180, 192, 512):
        return "Not found", 404
    scale = size / 512
    icon = Image.new("RGB", (size, size), "#fffaf2")
    draw = ImageDraw.Draw(icon)
    def box(values):
        return tuple(round(value * scale) for value in values)
    draw.rounded_rectangle(box((54, 54, 458, 458)), radius=round(112 * scale), fill="#222019")
    draw.line(box((166, 151, 166, 361)), fill="#f4a77c", width=max(round(34 * scale), 2))
    draw.arc(box((135, 143, 363, 369)), -90, 90, fill="#f4a77c", width=max(round(34 * scale), 2))
    for y in (205, 256, 307):
        draw.rounded_rectangle(box((195, y, 291, y + 18)), radius=max(round(9 * scale), 1), fill="#fffaf2")
    draw.ellipse(box((344, 344, 426, 426)), fill="#222019")
    draw.ellipse(box((356, 356, 414, 414)), fill="#63b877")
    output = BytesIO()
    icon.save(output, "PNG", optimize=True)
    output.seek(0)
    return send_file(output, mimetype="image/png", max_age=86400)


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/api/status")
def status_api():
    try:
        snapshot = system_snapshot()
        backup = None
        try:
            backup = json.loads(BACKUP_STATUS.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            backup = {"ok": False, "error": "status_unavailable"}
        return jsonify(ok=True, health=health_assessment(snapshot), backup=backup, **snapshot)
    except (OSError, ValueError, IndexError):
        return jsonify(ok=False, error="Server health details are unavailable right now."), 503


@app.get("/api/status/summary")
def status_summary_api():
    try:
        payload = server_status_snapshot()
        generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
        age_seconds = max(0, int((datetime.now(timezone.utc) - generated).total_seconds()))
        response = json.loads(json.dumps(payload))
        response["ok"] = True
        response["age_seconds"] = age_seconds
        response["stale"] = age_seconds > 600
        if response["stale"] and response["state"] == "healthy":
            response["state"] = "warning"
        return jsonify(response)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return jsonify(ok=False, error="Server status is temporarily unavailable."), 503


@app.get("/api/status/history")
def status_history():
    metrics = {
        "cpu": "cpu", "memory": "memory", "temperature": "temperature",
        "load": "load1", "hdd": "disk_used", "microsd": "root_disk_used",
        "swap": "swap", "container_memory": "container_memory",
        "health_latency": "health_latency", "backup": "backup_state",
        "queue": "queue_depth",
    }
    ranges = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}
    metric = request.args.get("metric", "temperature")
    selected_range = request.args.get("range", "24h")
    if metric not in metrics or selected_range not in ranges:
        return jsonify(error="Choose a supported metric and time range."), 400
    column = metrics[metric]
    with metrics_db() as connection:
        rows = connection.execute(
            f"SELECT timestamp, {column} FROM system_metrics "
            f"WHERE timestamp >= ? AND {column} IS NOT NULL ORDER BY timestamp",
            (int(time.time()) - ranges[selected_range] * 3600,),
        ).fetchall()
    maximum = 600
    step = max(1, (len(rows) + maximum - 1) // maximum)
    points = [{"timestamp": row[0], "value": row[1]} for row in rows[::step]]
    if rows and (not points or points[-1]["timestamp"] != rows[-1][0]):
        points.append({"timestamp": rows[-1][0], "value": rows[-1][1]})
    return jsonify(metric=metric, range=selected_range, points=points, sampled=step > 1)


def shutdown_password_hash():
    encoded = os.environ.get(SHUTDOWN_PASSWORD_ENV, "").strip()
    if not encoded or len(encoded) > 2048:
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return ""


def shutdown_attempt_state(succeeded=None, now=None):
    timestamp = int(now or time.time())
    cutoff = timestamp - SHUTDOWN_FAILURE_WINDOW_SECONDS
    with metrics_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM shutdown_auth_attempts WHERE attempted_at < ?", (timestamp - 86400,)
        )
        failures = connection.execute(
            "SELECT COUNT(*) FROM shutdown_auth_attempts "
            "WHERE succeeded=0 AND attempted_at >= ?", (cutoff,)
        ).fetchone()[0]
        if succeeded is True:
            connection.execute("DELETE FROM shutdown_auth_attempts WHERE succeeded=0")
        elif succeeded is False:
            connection.execute(
                "INSERT INTO shutdown_auth_attempts(attempted_at,succeeded) VALUES (?,0)",
                (timestamp,),
            )
            failures += 1
    return failures


def write_shutdown_request(now=None):
    request_path = SHUTDOWN_REQUEST
    data_root = DATA.resolve()
    parent = request_path.parent.resolve()
    try:
        parent.relative_to(data_root)
    except ValueError as error:
        raise OSError("Shutdown control path is outside David-Pi data.") from error
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if request_path.exists() or request_path.is_symlink():
        raise FileExistsError("A shutdown request is already pending.")
    payload = {
        "action": "poweroff",
        "request_id": uuid.uuid4().hex,
        "requested_at": int(now or time.time()),
        "version": 1,
    }
    temporary = parent / f".shutdown-{payload['request_id']}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, request_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return payload["request_id"]


@app.post("/api/system/shutdown")
def safe_shutdown_api():
    password_hash = shutdown_password_hash()
    if not password_hash:
        return jsonify(error="Safe shutdown is not configured."), 503
    if shutdown_attempt_state() >= SHUTDOWN_FAILURE_LIMIT:
        return jsonify(error="Too many attempts. Wait 15 minutes and try again."), 429
    payload = request.get_json(silent=True) or request.form
    password = payload.get("password", "") if hasattr(payload, "get") else ""
    if not isinstance(password, str) or not password or len(password) > 128:
        shutdown_attempt_state(False)
        return jsonify(error="The shutdown password does not match."), 401
    try:
        accepted = check_password_hash(password_hash, password)
    except ValueError:
        return jsonify(error="Safe shutdown is not configured correctly."), 503
    if not accepted:
        shutdown_attempt_state(False)
        return jsonify(error="The shutdown password does not match."), 401
    try:
        request_id = write_shutdown_request()
    except FileExistsError:
        return jsonify(error="David-Pi is already preparing to shut down."), 409
    except OSError:
        app.logger.exception("Safe shutdown request could not be queued.")
        return jsonify(error="David-Pi could not start a safe shutdown."), 503
    shutdown_attempt_state(True)
    return jsonify(
        ok=True,
        request_id=request_id,
        message="David-Pi is shutting down safely.",
    ), 202


BLOCKED_STATUSES = (1, 4, 5, 6, 7, 8, 9, 10, 11, 15, 16, 18)


def pihole_summary():
    if not PIHOLE_SUMMARY.exists():
        raise FileNotFoundError("Pi-hole statistics are unavailable.")
    data = json.loads(PIHOLE_SUMMARY.read_text(encoding="utf-8"))
    allowed = {"enabled", "total", "blocked", "blocked_percent", "clients", "window", "updated_at", "stale"}
    if set(data) - allowed:
        raise ValueError("Pi-hole summary contains unsupported fields.")
    return {key: data[key] for key in allowed if key in data}


@app.get("/api/pihole")
def pihole_api():
    try:
        return jsonify(ok=True, **pihole_summary())
    except (OSError, ValueError, json.JSONDecodeError):
        return jsonify(ok=False, error="Pi-hole totals are unavailable right now."), 503


def photo_library_summary():
    photo_visible, photo_params = visibility_sql("p")
    collection_visible, collection_params = visibility_sql("c")
    with db() as connection:
        active = connection.execute(
            f"SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NULL AND {photo_visible}", photo_params
        ).fetchone()[0]
        deleted = connection.execute(
            f"SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NOT NULL AND {photo_visible}", photo_params
        ).fetchone()[0]
        collections = connection.execute(
            f"SELECT COUNT(*) FROM collections c WHERE {collection_visible}", collection_params
        ).fetchone()[0]
    return {"photos": active, "deleted": deleted, "collections": collections}


def format_duration(seconds):
    days, remainder = divmod(max(int(seconds), 0), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if not days and minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts[:2]) or "less than a minute"


def assistant_help():
    return (
        "I can currently answer questions about your media, collections, Recently Deleted, "
        "storage, server health, uptime, temperature, and aggregate Pi-hole activity."
    )


def answer_assistant(question):
    normalized = " ".join(question.lower().split())
    if re.search(r"\b(domains?|websites?|sites? visited|browsing|browser history|device activity|who visited)\b", normalized):
        return "I only provide aggregate Pi-hole totals, not websites, browsing history, or device-specific activity.", "privacy"
    if re.search(r"\b(help|what can you|what do you|capabilities)\b", normalized):
        return assistant_help(), "help"
    if re.search(r"\b(recently deleted|deleted photos|deleted media|trash)\b", normalized):
        facts = photo_library_summary()
        return f"Recently Deleted currently has {facts['deleted']} item{'s' if facts['deleted'] != 1 else ''}.", "deleted"
    if re.search(r"\b(collection|collections|album|albums)\b", normalized):
        facts = photo_library_summary()
        return f"You currently have {facts['collections']} collection{'s' if facts['collections'] != 1 else ''}.", "collections"
    if re.search(r"\b(photo|photos|pictures|video|videos|media|gallery)\b", normalized):
        facts = photo_library_summary()
        return f"David-Pi currently has {facts['photos']} media item{'s' if facts['photos'] != 1 else ''} in the gallery.", "photos"
    if re.search(r"\b(storage|space|disk|room|capacity)\b", normalized):
        snapshot = system_snapshot()
        return (
            f"David-Pi has {snapshot['disk_free_gb']} GB free out of {snapshot['disk_total_gb']} GB. "
            f"Storage is {snapshot['disk_used']}% used."
        ), "storage"
    if re.search(r"\b(temp|temperature|hot|heat)\b", normalized):
        snapshot = system_snapshot()
        if snapshot["temperature"] is None:
            return "The Pi temperature is unavailable right now.", "temperature"
        state = health_assessment(snapshot)["metrics"]["temperature"]
        description = {"good": "a healthy temperature", "warning": "warm enough to watch", "critical": "hot enough to need attention"}.get(state, "unavailable")
        return f"The Pi is {snapshot['temperature']}°C, which is {description}.", "temperature"
    if re.search(r"\b(uptime|how long|running for|been running)\b", normalized):
        snapshot = system_snapshot()
        return f"David-Pi has been running for {format_duration(snapshot['uptime'])}.", "uptime"
    if re.search(r"\b(pi[ -]?hole|blocked|blocking|ads|queries|dns)\b", normalized):
        try:
            totals = pihole_summary()
        except (sqlite3.Error, FileNotFoundError):
            return "Pi-hole totals are unavailable right now.", "pihole"
        return (
            f"In the last 24 hours, Pi-hole blocked {totals['blocked']:,} of {totals['total']:,} DNS queries "
            f"({totals['blocked_percent']}%)."
        ), "pihole"
    if re.search(r"\b(healthy|health|status|doing|okay|ok|server)\b", normalized):
        assessment = health_assessment(system_snapshot())
        detail = {
            "good": "CPU, memory, storage, and temperature are all in their healthy ranges.",
            "warning": "At least one measurement is in the watch range. The Server status page has the details.",
            "critical": "At least one measurement needs attention. Please open Server status for the details.",
            "unknown": "Some measurements are unavailable right now.",
        }[assessment["overall"]]
        return f"{assessment['message']}. {detail}", "health"
    return assistant_help(), "unsupported"


init_assistant(app, answer_assistant)
_assistant_knowledge = Path(__file__).parent / "knowledge" / "assistant"
seed_knowledge(
    [
        (path.stem, path.stem.replace("-", " ").title(), path.read_text(encoding="utf-8"))
        for path in sorted(_assistant_knowledge.glob("*.md"))
    ]
)


@app.get("/api/photos")
def list_photos():
    limit = min(max(request.args.get("limit", 30, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    raw_cursor = request.args.get("cursor")
    cursor = decode_gallery_cursor(raw_cursor, 3)
    if raw_cursor and not cursor:
        return jsonify(error="The gallery position is invalid. Refresh and try again."), 400
    collection_id = request.args.get("collection", "").strip()
    try:
        period = gallery_period()
    except ValueError as error:
        return jsonify(error=str(error)), 400
    period_sql, period_params = gallery_period_sql(period)
    view = request.args.get("view", "all")
    if view == "mine":
        visible, visibility_params = "p.owner_id = ?", [current_device()["owner_id"] or ""]
        collection_visible, collection_params = (
            "(c.owner_id = ? OR lower(c.name) = 'videos')", [current_device()["owner_id"] or ""]
        )
    else:
        visible, visibility_params = "p.visibility = 'shared'", []
        collection_visible, collection_params = "c.visibility = 'shared'", []
    cursor_sql = ""
    cursor_params = []
    if cursor:
        cursor_sql = (
            " AND (p.capture_timestamp < ? OR (p.capture_timestamp = ? AND p.uploaded_at < ?) "
            "OR (p.capture_timestamp = ? AND p.uploaded_at = ? AND p.id < ?))"
        )
        cursor_params = [
            cursor[0], cursor[0], cursor[1], cursor[0], cursor[1], cursor[2],
        ]
    gallery_columns = (
        "p.id,p.original_name,p.content_type,p.taken_at,p.capture_timestamp,"
        "p.uploaded_at,p.deleted_at,"
        "p.playback_name,p.loop_playback,p.owner_id,p.owner_name,p.uploaded_by,p.visibility"
    )
    with db() as connection:
        if collection_id:
            collection = connection.execute(
                f"SELECT 1 FROM collections c WHERE c.id = ? AND {collection_visible}",
                (collection_id, *collection_params),
            ).fetchone()
            if not collection:
                return jsonify(error="Collection not found."), 404
            rows = connection.execute(
                f"SELECT {gallery_columns} FROM photos p "
                "JOIN collection_photos cp ON cp.photo_id = p.id "
                f"WHERE cp.collection_id = ? AND p.deleted_at IS NULL AND {visible}"
                f"{period_sql}{cursor_sql} "
                "ORDER BY p.capture_timestamp DESC, p.uploaded_at DESC, p.id DESC "
                "LIMIT ? OFFSET ?",
                (
                    collection_id, *visibility_params, *period_params, *cursor_params,
                    limit + 1, 0 if cursor else offset,
                ),
            ).fetchall()
            total = None if cursor else connection.execute(
                "SELECT COUNT(*) FROM collection_photos cp JOIN photos p ON p.id = cp.photo_id "
                f"WHERE cp.collection_id = ? AND p.deleted_at IS NULL AND {visible}{period_sql}",
                (collection_id, *visibility_params, *period_params),
            ).fetchone()[0]
        else:
            rows = connection.execute(
                f"SELECT {gallery_columns} FROM photos p "
                f"WHERE p.deleted_at IS NULL AND {visible}{period_sql}{cursor_sql} "
                "ORDER BY p.capture_timestamp DESC, p.uploaded_at DESC, p.id DESC "
                "LIMIT ? OFFSET ?",
                (
                    *visibility_params, *period_params, *cursor_params,
                    limit + 1, 0 if cursor else offset,
                ),
            ).fetchall()
            total = None if cursor else connection.execute(
                f"SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NULL AND {visible}{period_sql}",
                (*visibility_params, *period_params),
            ).fetchone()[0]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_gallery_cursor(
            rows[-1]["capture_timestamp"], rows[-1]["uploaded_at"], rows[-1]["id"]
        )
        if has_more and rows else None
    )
    return jsonify(
        total=total, has_more=has_more, next_cursor=next_cursor, period=period,
        photos=[photo_json(row) for row in rows],
    )


@app.get("/api/photos/timeline")
def photo_timeline():
    collection_id = request.args.get("collection", "").strip()
    view = request.args.get("view", "all")
    if view == "mine":
        visible, visibility_params = "p.owner_id = ?", [current_device()["owner_id"] or ""]
        collection_visible, collection_params = "(c.owner_id = ? OR lower(c.name) = 'videos')", [current_device()["owner_id"] or ""]
    else:
        visible, visibility_params = "p.visibility = 'shared'", []
        collection_visible, collection_params = "c.visibility = 'shared'", []
    with db() as connection:
        if collection_id:
            collection = connection.execute(
                f"SELECT 1 FROM collections c WHERE c.id=? AND {collection_visible}",
                (collection_id, *collection_params),
            ).fetchone()
            if not collection:
                return jsonify(error="Collection not found."), 404
            rows = connection.execute(
                "SELECT substr(p.capture_timestamp,1,7) AS month,COUNT(*) AS item_count "
                "FROM photos p JOIN collection_photos cp ON cp.photo_id=p.id "
                f"WHERE cp.collection_id=? AND p.deleted_at IS NULL AND {visible} "
                "GROUP BY substr(p.capture_timestamp,1,7) ORDER BY month DESC",
                (collection_id, *visibility_params),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT substr(p.capture_timestamp,1,7) AS month,COUNT(*) AS item_count "
                f"FROM photos p WHERE p.deleted_at IS NULL AND {visible} "
                "GROUP BY substr(p.capture_timestamp,1,7) ORDER BY month DESC",
                visibility_params,
            ).fetchall()
    months = [dict(row) for row in rows if re.fullmatch(r"\d{4}-\d{2}", row["month"] or "")]
    return jsonify(months=months, total=sum(row["item_count"] for row in months))


@app.get("/api/photos/deleted")
def deleted_photos():
    limit = min(max(request.args.get("limit", 30, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    raw_cursor = request.args.get("cursor")
    cursor = decode_gallery_cursor(raw_cursor, 2)
    if raw_cursor and not cursor:
        return jsonify(error="The gallery position is invalid. Refresh and try again."), 400
    if request.args.get("view") == "mine":
        visible, parameters = "p.owner_id = ?", [current_device()["owner_id"] or ""]
    else:
        visible, parameters = "p.visibility = 'shared'", []
    cursor_sql = ""
    cursor_params = []
    if cursor:
        cursor_sql = " AND (p.deleted_at < ? OR (p.deleted_at = ? AND p.id < ?))"
        cursor_params = [cursor[0], cursor[0], cursor[1]]
    with db() as connection:
        rows = connection.execute(
            """SELECT p.id,p.original_name,p.content_type,p.taken_at,p.capture_timestamp,
                      p.uploaded_at,
                      p.deleted_at,p.playback_name,p.loop_playback,p.owner_id,
                      p.owner_name,p.uploaded_by,p.visibility
               FROM photos p """
            f"WHERE p.deleted_at IS NOT NULL AND {visible}{cursor_sql} "
            "ORDER BY p.deleted_at DESC,p.id DESC LIMIT ? OFFSET ?",
            (*parameters, *cursor_params, limit + 1, 0 if cursor else offset),
        ).fetchall()
        total = None if cursor else connection.execute(
            f"SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NOT NULL AND {visible}", parameters
        ).fetchone()[0]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_gallery_cursor(rows[-1]["deleted_at"], rows[-1]["id"])
        if has_more and rows else None
    )
    return jsonify(
        total=total, has_more=has_more, next_cursor=next_cursor,
        photos=[photo_json(row) for row in rows],
    )


def encode_gallery_cursor(*values):
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_gallery_cursor(value, expected_items):
    if not value:
        return None
    if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    try:
        padding = "=" * (-len(value) % 4)
        decoded = json.loads(
            base64.urlsafe_b64decode(value + padding).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(decoded, list)
        or len(decoded) != expected_items
        or any(not isinstance(item, str) or len(item) > 128 for item in decoded)
    ):
        return None
    return decoded


def gallery_period():
    value = request.args.get("period", "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}(?:-(?:0[1-9]|1[0-2]))?", value):
        raise ValueError("Choose a valid year or month.")
    return value


def gallery_period_sql(period):
    if not period:
        return "", []
    year = int(period[:4])
    if len(period) == 4:
        start, end = f"{year:04d}-", f"{year + 1:04d}-"
    else:
        month = int(period[5:7])
        start = f"{year:04d}-{month:02d}-"
        end = f"{year + 1:04d}-01-" if month == 12 else f"{year:04d}-{month + 1:02d}-"
    return " AND p.capture_timestamp >= ? AND p.capture_timestamp < ?", [start, end]


def photo_json(row):
    values = dict(row)
    is_video = values.get("content_type", "").startswith("video/")
    deleted_prefix = "/media/deleted" if values.get("deleted_at") else "/media"
    return {
        "id": values["id"],
        "original_name": values["original_name"],
        "visibility": values.get("visibility") or "shared",
        "is_mine": bool(current_device()["owner_id"] and values.get("owner_id") == current_device()["owner_id"]),
        "owner_display": values.get("owner_name") or values.get("uploaded_by") or "Home",
        "is_video": is_video,
        "captured_at": values.get("capture_timestamp") or values.get("taken_at"),
        "loop_playback": bool(values.get("loop_playback", 0)),
        "thumb": f"{deleted_prefix}/thumb/{row['id']}",
        "preview": f"{deleted_prefix}/preview/{row['id']}",
        "view": f"{deleted_prefix}/view/{row['id']}",
        "playback": f"{deleted_prefix}/play/{row['id']}" if is_video else None,
        "original": f"{deleted_prefix}/original/{row['id']}",
    }


def managed_path(root, relative):
    root = root.resolve()
    candidate = root / str(relative)
    if candidate.is_symlink():
        raise ValueError("Managed objects cannot be symbolic links.")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("Managed object path escaped its storage root.")
    return resolved


def destroy_photo(photo_id, cutoff=None, enforce_visibility=False):
    quarantined = []
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sql = "SELECT * FROM photos WHERE id = ? AND deleted_at IS NOT NULL"
        parameters = [photo_id]
        if cutoff is not None:
            sql += " AND deleted_at < ?"
            parameters.append(cutoff)
        if enforce_visibility:
            visible, visible_parameters = visibility_sql()
            sql += f" AND {visible}"
            parameters.extend(visible_parameters)
        row = connection.execute(sql, parameters).fetchone()
        if not row:
            return False
        other_reference = connection.execute(
            """SELECT 1 FROM photos WHERE id<>? AND stored_path=? LIMIT 1""",
            (photo_id, row["stored_path"]),
        ).fetchone()
        paths = []
        if not other_reference:
            paths = [
                managed_path(ORIGINALS, row["stored_path"]),
                managed_path(PREVIEWS, row["preview_name"]),
                managed_path(VIEWER_PREVIEWS, viewer_preview_name(row["preview_name"])),
                managed_path(THUMBS, row["thumb_name"]),
            ]
            if row["playback_name"]:
                paths.append(managed_path(PREVIEWS, row["playback_name"]))
        try:
            for source in paths:
                if source.is_file():
                    target = QUARANTINE / f"{photo_id}-{uuid.uuid4().hex}-{source.name}"
                    os.replace(source, target)
                    quarantined.append((source, target))
            deleted = connection.execute(
                "DELETE FROM photos WHERE id = ? AND deleted_at IS NOT NULL",
                (photo_id,),
            )
            if deleted.rowcount != 1:
                raise sqlite3.IntegrityError("Media state changed during deletion.")
        except Exception:
            for source, target in reversed(quarantined):
                if target.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(target, source)
            raise
    for _, target in quarantined:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
    return True


def requested_photo_ids():
    data = request.get_json(silent=True) or {}
    values = data.get("ids", [])
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value) for value in values if value))[:500]


@app.patch("/api/photos/visibility")
def set_photos_visibility():
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one media item."), 400
    data = request.get_json(silent=True) or {}
    try:
        visibility = requested_visibility(data.get("visibility"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    actor = current_device()
    placeholders = ",".join("?" for _ in ids)
    visible, parameters = visibility_sql()
    with db() as connection:
        rows = connection.execute(
            f"SELECT id, owner_id FROM photos WHERE id IN ({placeholders}) AND {visible}",
            (*ids, *parameters),
        ).fetchall()
        if len(rows) != len(ids):
            return jsonify(error="One or more media items could not be found."), 404
        if visibility == "private" and any(
            row["owner_id"] and row["owner_id"] != actor["owner_id"] for row in rows
        ):
            return jsonify(error="Only the owner can make that media private."), 403
        if visibility == "private":
            connection.execute(
                f"""UPDATE photos SET visibility='private',
                    owner_id=COALESCE(owner_id, ?), owner_name=COALESCE(owner_name, ?)
                    WHERE id IN ({placeholders})""",
                (actor["owner_id"], actor["name"], *ids),
            )
        else:
            connection.execute(
                f"UPDATE photos SET visibility='shared' WHERE id IN ({placeholders})", ids
            )
    return jsonify(ok=True, count=len(ids), visibility=visibility)


@app.post("/api/photos/trash")
def trash_photos():
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    placeholders = ",".join("?" for _ in ids)
    now = datetime.now(timezone.utc).isoformat()
    visible, parameters = visibility_sql()
    with db() as connection:
        result = connection.execute(
            f"UPDATE photos SET deleted_at = ? WHERE id IN ({placeholders}) "
            f"AND deleted_at IS NULL AND {visible}", (now, *ids, *parameters)
        )
    return jsonify(ok=True, count=result.rowcount)


@app.post("/api/photos/restore")
def restore_photos():
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    placeholders = ",".join("?" for _ in ids)
    visible, parameters = visibility_sql()
    with db() as connection:
        result = connection.execute(
            f"UPDATE photos SET deleted_at = NULL WHERE id IN ({placeholders}) AND {visible}",
            (*ids, *parameters),
        )
    return jsonify(ok=True, count=result.rowcount)


@app.post("/api/photos/restore-all")
def restore_all_photos():
    visible, parameters = visibility_sql()
    with db() as connection:
        result = connection.execute(
            f"UPDATE photos SET deleted_at = NULL WHERE deleted_at IS NOT NULL AND {visible}",
            parameters,
        )
    return jsonify(ok=True, count=result.rowcount)


@app.post("/api/photos/purge")
def purge_photos():
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    count = sum(1 for photo_id in ids if destroy_photo(photo_id, enforce_visibility=True))
    return jsonify(ok=True, count=count)


@app.post("/api/photos/purge-all")
def purge_all_photos():
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != "empty-recently-deleted":
        return jsonify(error="Confirmation is required before permanently deleting photos."), 400
    with db() as connection:
        visible, parameters = visibility_sql()
        ids = [row[0] for row in connection.execute(
            f"SELECT id FROM photos WHERE deleted_at IS NOT NULL AND {visible} ORDER BY deleted_at",
            parameters,
        ).fetchall()]
    count = sum(1 for photo_id in ids if destroy_photo(photo_id, enforce_visibility=True))
    return jsonify(ok=True, count=count)


@app.delete("/api/photos/<photo_id>")
def delete_photo(photo_id):
    row = photo_row(photo_id)
    if not row:
        return jsonify(error="Photo not found."), 404
    with db() as connection:
        visible, parameters = visibility_sql()
        connection.execute(
            f"UPDATE photos SET deleted_at = ? WHERE id = ? AND {visible}",
            (datetime.now(timezone.utc).isoformat(), photo_id, *parameters),
        )
    return jsonify(ok=True)


def purge_expired_photos():
    cutoff = datetime.fromtimestamp(time.time() - 30 * 86400, timezone.utc).isoformat()
    with db() as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM photos WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,)).fetchall()]
    for photo_id in ids:
        destroy_photo(photo_id, cutoff=cutoff)


def clean_collection_name():
    data = request.get_json(silent=True) or {}
    name = " ".join(str(data.get("name", "")).split()).strip()
    return name[:80]


@app.get("/api/collections")
def list_collections():
    if request.args.get("view") == "mine":
        collection_visible, collection_params = (
            "(c.owner_id = ? OR lower(c.name) = 'videos')", [current_device()["owner_id"] or ""]
        )
        photo_visible, photo_params = "p.owner_id = ?", [current_device()["owner_id"] or ""]
    else:
        collection_visible, collection_params = "c.visibility = 'shared'", []
        photo_visible, photo_params = "p.visibility = 'shared'", []
    with db() as connection:
        rows = connection.execute(
            f"SELECT c.* FROM collections c WHERE {collection_visible} ORDER BY c.created_at DESC",
            collection_params,
        ).fetchall()
        result = []
        for row in rows:
            count = connection.execute(
                f"""SELECT COUNT(*) FROM collection_photos cp
                    JOIN photos p ON p.id=cp.photo_id
                    WHERE cp.collection_id=? AND p.deleted_at IS NULL AND {photo_visible}""",
                (row["id"], *photo_params),
            ).fetchone()[0]
            cover = connection.execute(
                f"""SELECT p.id FROM collection_photos cp
                    JOIN photos p ON p.id=cp.photo_id
                    WHERE cp.collection_id=? AND p.deleted_at IS NULL AND {photo_visible}
                    ORDER BY p.capture_timestamp DESC, cp.added_at DESC LIMIT 1""",
                (row["id"], *photo_params),
            ).fetchone()
            item = dict(row)
            item.update(
                photo_count=count,
                cover=f"/media/thumb/{cover['id']}" if cover else None,
                is_mine=bool(current_device()["owner_id"] and row["owner_id"] == current_device()["owner_id"]),
                owner_display=row["owner_name"] or row["created_by"] or "Home",
            )
            result.append(item)
    return jsonify(collections=result, current_user=current_device()["name"])


@app.post("/api/collections")
def create_collection():
    name = clean_collection_name()
    if not name:
        return jsonify(error="Give the collection a name."), 400
    collection_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    data = request.get_json(silent=True) or {}
    try:
        visibility = requested_visibility(data.get("visibility", "shared"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    actor = current_device()
    with db() as connection:
        existing = connection.execute("SELECT id FROM collections WHERE lower(name) = lower(?)", (name,)).fetchone()
        if existing:
            return jsonify(error="A collection with that name already exists."), 409
        connection.execute(
            """INSERT INTO collections
               (id,name,created_at,created_by,owner_id,owner_name,visibility)
               VALUES (?,?,?,?,?,?,?)""",
            (collection_id, name, created_at, actor["name"], actor["owner_id"], actor["name"], visibility),
        )
    return jsonify(id=collection_id, name=name, photo_count=0, cover=None,
                   visibility=visibility, is_mine=True, owner_display=actor["name"]), 201


@app.patch("/api/collections/<collection_id>")
def rename_collection(collection_id):
    name = clean_collection_name()
    if not name:
        return jsonify(error="Give the collection a name."), 400
    data = request.get_json(silent=True) or {}
    visibility = data.get("visibility")
    with db() as connection:
        conflict = connection.execute("SELECT id FROM collections WHERE lower(name) = lower(?) AND id != ?", (name, collection_id)).fetchone()
        if conflict:
            return jsonify(error="A collection with that name already exists."), 409
        visible, parameters = visibility_sql()
        row = connection.execute(
            f"SELECT * FROM collections WHERE id=? AND {visible}", (collection_id, *parameters)
        ).fetchone()
        if not row:
            return jsonify(error="Collection not found."), 404
        if visibility is not None:
            try:
                visibility = requested_visibility(visibility)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            if visibility == "private" and row["owner_id"] not in (None, current_device()["owner_id"]):
                return jsonify(error="Only the owner can make this collection private."), 403
            owner_id = row["owner_id"] or current_device()["owner_id"]
            owner_name = row["owner_name"] or current_device()["name"]
        else:
            visibility, owner_id, owner_name = row["visibility"], row["owner_id"], row["owner_name"]
        result = connection.execute(
            "UPDATE collections SET name=?,visibility=?,owner_id=?,owner_name=? WHERE id=?",
            (name, visibility, owner_id, owner_name, collection_id),
        )
        if not result.rowcount:
            return jsonify(error="Collection not found."), 404
    return jsonify(ok=True, name=name)


@app.delete("/api/collections/<collection_id>")
def delete_collection(collection_id):
    visible, parameters = visibility_sql()
    with db() as connection:
        result = connection.execute(
            f"DELETE FROM collections WHERE id = ? AND {visible}", (collection_id, *parameters)
        )
        if not result.rowcount:
            return jsonify(error="Collection not found."), 404
    return jsonify(ok=True)


SLIDESHOW_TRANSITIONS = {"none", "mixed", "fade", "dissolve", "wipeleft", "slideright", "circleopen"}
SLIDESHOW_MIXED_TRANSITIONS = ("fade", "dissolve", "wipeleft", "slideright", "circleopen")
SLIDESHOW_LAYOUTS = {"balanced", "fill", "fit"}
SLIDESHOW_MAX_ITEMS = 200
SLIDESHOW_MUSIC = (
    {
        "id": "vaporware", "title": "Vaporware", "artist": "The Cynic Project",
        "license": "CC0 / public domain", "filename": "vaporware.mp3",
        "source": "https://opengameart.org/content/calm-piano-1-vaporware",
    },
    {
        "id": "jrpg-piano", "title": "JRPG Piano", "artist": "Joth",
        "license": "CC0 / public domain", "filename": "jrpg-piano.mp3",
        "source": "https://opengameart.org/content/jrpg-piano",
    },
    {
        "id": "dream", "title": "Dream", "artist": "jkjkke",
        "license": "CC0 / public domain", "filename": "dream.mp3",
        "source": "https://opengameart.org/content/mainmenu-music",
    },
    {
        "id": "mystical-piano", "title": "Mystical Piano", "artist": "Indieteur",
        "license": "CC0 / public domain", "filename": "mystical-piano.mp3",
        "source": "https://opengameart.org/content/mystical-piano",
    },
)


def update_slideshow_job(job_id, **values):
    if not values:
        return
    values["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db() as connection:
        connection.execute(
            f"UPDATE slideshow_jobs SET {assignments} WHERE id = ?",
            (*values.values(), job_id),
        )


@contextmanager
def global_slideshow_lock():
    lock_path = INCOMING / ".slideshow-render.lock"
    with lock_path.open("a+b") as lock:
        try:
            import fcntl
        except ImportError:
            yield
            return
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def media_details(path):
    result = run_media_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", str(path),
    ])
    details = json.loads(result.stdout)
    try:
        duration = float(details.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    return {
        "duration": duration,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in details.get("streams", [])),
    }


def detect_dark_border_crop(image):
    """Find conservative, nearly uniform black bars without trimming normal dark content."""
    original_width, original_height = image.size
    sample = image.convert("L")
    sample.thumbnail((512, 512), Image.Resampling.LANCZOS)
    width, height = sample.size
    pixels = list(sample.getdata())

    def dark_row(y):
        values = pixels[y * width:(y + 1) * width]
        dark = sum(value <= 24 for value in values)
        return dark / width >= 0.985 and sum(values) / width <= 14

    def dark_column(x):
        values = pixels[x::width]
        dark = sum(value <= 24 for value in values)
        return dark / height >= 0.985 and sum(values) / height <= 14

    top = 0
    while top < int(height * 0.35) and dark_row(top):
        top += 1
    bottom = height
    while bottom > int(height * 0.65) and dark_row(bottom - 1):
        bottom -= 1
    left = 0
    while left < int(width * 0.35) and dark_column(left):
        left += 1
    right = width
    while right > int(width * 0.65) and dark_column(right - 1):
        right -= 1

    # Ignore tiny edge noise and refuse any crop that would remove most of the image.
    if top < 2:
        top = 0
    if height - bottom < 2:
        bottom = height
    if left < 2:
        left = 0
    if width - right < 2:
        right = width
    if (left, top, right, bottom) == (0, 0, width, height):
        return None
    if right - left < width * 0.3 or bottom - top < height * 0.3:
        return None
    inner = [
        pixels[y * width + x]
        for y in range(top, bottom)
        for x in range(left, right)
    ]
    if not inner or sum(value > 32 for value in inner) / len(inner) < 0.02:
        return None

    scale_x = original_width / width
    scale_y = original_height / height
    box = (
        max(0, round(left * scale_x)),
        max(0, round(top * scale_y)),
        min(original_width, round(right * scale_x)),
        min(original_height, round(bottom * scale_y)),
    )
    return box if box[2] > box[0] and box[3] > box[1] else None


def create_balanced_frame(source, destination):
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        crop = detect_dark_border_crop(image)
        if crop:
            image = image.crop(crop)
        background = ImageOps.fit(
            image, (1280, 720), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
        background = background.filter(ImageFilter.GaussianBlur(radius=28))
        background = ImageEnhance.Brightness(background).enhance(0.62)
        foreground = ImageOps.contain(image, (1280, 720), Image.Resampling.LANCZOS)
        left = (1280 - foreground.width) // 2
        top = (720 - foreground.height) // 2
        background.paste(foreground, (left, top))
        background.save(destination, "JPEG", quality=90, optimize=True)
    return crop


def slideshow_timing(items, total_seconds, transition):
    """Return item durations, overlap durations and timeline starts."""
    photo_count = sum(not item["is_video"] for item in items)
    video_seconds = sum(item.get("source_duration", 0) for item in items if item["is_video"])
    transition_enabled = transition != "none" and len(items) > 1
    photo_seconds = max(0.5, (total_seconds - video_seconds) / max(photo_count, 1))

    for _ in range(4):
        durations = [
            item.get("source_duration", 0) if item["is_video"] else photo_seconds
            for item in items
        ]
        overlaps = [
            min(0.75, durations[index] / 3, durations[index + 1] / 3)
            if transition_enabled else 0
            for index in range(len(items) - 1)
        ]
        if photo_count:
            photo_seconds = (total_seconds - video_seconds + sum(overlaps)) / photo_count

    if photo_count and photo_seconds < 0.5:
        raise ValueError("Increase the total time so each photo can be shown for at least half a second.")
    durations = [
        item.get("source_duration", 0) if item["is_video"] else photo_seconds
        for item in items
    ]
    overlaps = [
        min(0.75, durations[index] / 3, durations[index + 1] / 3)
        if transition_enabled else 0
        for index in range(len(items) - 1)
    ]
    natural_total = sum(durations) - sum(overlaps)
    if not photo_count:
        if total_seconds + 0.05 < natural_total:
            raise ValueError(
                f"The videos need at least {int(natural_total + .99)} seconds to play in full. "
                "Increase the total time or use a collection with photos."
            )
        durations[-1] += total_seconds - natural_total

    starts = [0.0]
    for index in range(1, len(items)):
        starts.append(starts[-1] + durations[index - 1] - overlaps[index - 1])
    return durations, overlaps, starts


def slideshow_filter(items, total_seconds, transition, layout):
    durations, overlaps, starts = slideshow_timing(items, total_seconds, transition)
    graph = []
    for index, item in enumerate(items):
        if layout == "balanced" and not item["is_video"]:
            sizing = "scale=1280:720"
        elif layout == "fill":
            sizing = (
                "scale=1280:720:force_original_aspect_ratio=increase,"
                "crop=1280:720"
            )
        else:
            sizing = (
                "scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black"
            )
        extension = ""
        if item["is_video"] and durations[index] > item["source_duration"]:
            extension = f",tpad=stop_mode=clone:stop_duration={durations[index] - item['source_duration']:.3f}"
        graph.append(
            f"[{index}:v]{sizing},setsar=1,"
            f"trim=duration={item['source_duration'] if item['is_video'] else durations[index]:.3f}"
            f"{extension},setpts=PTS-STARTPTS,format=yuv420p,settb=AVTB,fps=30[v{index}]"
        )

    if transition == "none" or len(items) == 1:
        graph.append(
            f"{''.join(f'[v{index}]' for index in range(len(items)))}"
            f"concat=n={len(items)}:v=1:a=0,trim=duration={total_seconds:.3f},setpts=PTS-STARTPTS[outv]"
        )
    else:
        current = "v0"
        for index in range(1, len(items)):
            output = f"x{index}"
            effect = (
                SLIDESHOW_MIXED_TRANSITIONS[(index - 1) % len(SLIDESHOW_MIXED_TRANSITIONS)]
                if transition == "mixed" else transition
            )
            graph.append(
                f"[{current}][v{index}]xfade=transition={effect}:duration={overlaps[index - 1]:.3f}:"
                f"offset={starts[index]:.3f},settb=AVTB,fps=30[{output}]"
            )
            current = output
        graph.append(f"[{current}]trim=duration={total_seconds:.3f},setpts=PTS-STARTPTS[outv]")
    return graph, durations, starts


def slideshow_audio_filter(items, durations, starts, total_seconds, music_input=None):
    graph = []
    video_audio = []
    silent_intervals = []
    for index, item in enumerate(items):
        if not item["is_video"] or not item.get("has_audio"):
            continue
        end = min(total_seconds, starts[index] + item["source_duration"])
        if end <= starts[index]:
            continue
        output = f"sourceaudio{index}"
        delay_ms = max(0, round(starts[index] * 1000))
        graph.append(
            f"[{index}:a]atrim=duration={item['source_duration']:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[{output}]"
        )
        video_audio.append(output)
        silent_intervals.append((starts[index], end))

    if music_input is None:
        graph.append(
            f"anullsrc=channel_layout=stereo:sample_rate=48000,atrim=duration={total_seconds:.3f}[background]"
        )
    else:
        music_filters = (
            f"[{music_input}:a]atrim=duration={total_seconds:.3f},asetpts=PTS-STARTPTS,"
            "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.28"
        )
        for start, end in silent_intervals:
            music_filters += f",volume=0:enable='between(t\\,{start:.3f}\\,{end:.3f})'"
        graph.append(f"{music_filters}[background]")

    inputs = ["background", *video_audio]
    graph.append(
        f"{''.join(f'[{name}]' for name in inputs)}"
        f"amix=inputs={len(inputs)}:duration=longest:normalize=0,"
        f"atrim=duration={total_seconds:.3f},alimiter=limit=0.95[outa]"
    )
    return graph


def generate_slideshow(job_id):
    with global_slideshow_lock():
        return generate_slideshow_locked(job_id)


def generate_slideshow_locked(job_id):
    temp_path = INCOMING / f"{job_id}.mp4"
    photo_id = uuid.uuid4().hex
    preview_name = thumb_name = playback_name = None
    balanced_paths = []
    try:
        with db() as connection:
            job = connection.execute("SELECT * FROM slideshow_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        parameters = json.loads(job["parameters_json"])
        ids = parameters["media_ids"]
        placeholders = ",".join("?" for _ in ids)
        with db() as connection:
            rows = connection.execute(
                f"SELECT id, preview_name, stored_path, playback_name, content_type "
                f"FROM photos WHERE id IN ({placeholders}) AND deleted_at IS NULL",
                ids,
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        ordered = [dict(by_id[media_id]) for media_id in ids if media_id in by_id]
        if len(ordered) != len(ids):
            raise ValueError("One or more source items are no longer available.")

        total_seconds = float(parameters["duration_seconds"])
        transition = parameters["transition"]
        layout = parameters.get("layout", "balanced")
        if layout == "balanced":
            update_slideshow_job(job_id, status="working", progress=8, message="Balancing your pictures…")
        for index, item in enumerate(ordered):
            item["is_video"] = item["content_type"].startswith("video/")
            if item["is_video"]:
                item["source_path"] = (
                    PREVIEWS / item["playback_name"]
                    if item.get("playback_name") else ORIGINALS / item["stored_path"]
                )
                details = media_details(item["source_path"])
                if details["duration"] <= 0:
                    raise ValueError("David-Pi could not read the length of one source video.")
                item["source_duration"] = details["duration"]
                item["has_audio"] = details["has_audio"]
            else:
                item["source_path"] = PREVIEWS / item["preview_name"]
                item["source_duration"] = 0
                item["has_audio"] = False
            if not item["source_path"].is_file():
                raise ValueError("One or more source files are no longer available.")
            if layout == "balanced" and not item["is_video"]:
                balanced_path = INCOMING / f"{job_id}-balanced-{index}.jpg"
                create_balanced_frame(item["source_path"], balanced_path)
                balanced_paths.append(balanced_path)
                item["source_path"] = balanced_path

        visual_graph, durations, starts = slideshow_filter(
            ordered, total_seconds, transition, layout
        )
        arguments = ["ffmpeg", "-y"]
        for index, item in enumerate(ordered):
            if item["is_video"]:
                arguments.extend(["-i", str(item["source_path"])])
            else:
                arguments.extend([
                    "-loop", "1", "-t", f"{durations[index]:.3f}",
                    "-i", str(item["source_path"]),
                ])

        music_input = None
        music_id = parameters.get("music_id")
        if music_id:
            track = next((track for track in SLIDESHOW_MUSIC if track["id"] == music_id), None)
            if not track or not (MUSIC_LIBRARY / track["filename"]).is_file():
                raise ValueError("The selected music is no longer available.")
            music_input = len(ordered)
            arguments.extend(["-stream_loop", "-1", "-i", str(MUSIC_LIBRARY / track["filename"])])
        filter_graph = ";".join([
            *visual_graph,
            *slideshow_audio_filter(ordered, durations, starts, total_seconds, music_input),
        ])
        arguments.extend([
            "-filter_complex", filter_graph, "-map", "[outv]", "-map", "[outa]",
            "-t", f"{total_seconds:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "160k",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-metadata", f"comment=David-Pi slideshow {job_id}", str(temp_path),
        ])

        update_slideshow_job(job_id, status="working", progress=15, message="Building your video…")
        run_media_command(arguments)
        update_slideshow_job(job_id, progress=86, message="Saving your video…")

        now = datetime.now(timezone.utc)
        preview_name, thumb_name, playback_name = prepare_video(temp_path, photo_id, ".mp4")
        relative = Path("generated") / f"{now.year:04d}" / f"{now.month:02d}" / f"{photo_id}.mp4"
        destination = ORIGINALS / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(destination)
        digest = hashlib.sha256()
        with destination.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        source_name = parameters["source_name"]
        original_name = secure_filename(f"Slideshow - {source_name}.mp4") or "Slideshow.mp4"
        created_at = now.isoformat()
        with db() as connection:
            collection_visible, collection_params = visibility_sql()
            videos = connection.execute(
                f"SELECT id FROM collections WHERE lower(name)='videos' AND {collection_visible}",
                collection_params,
            ).fetchone()
            if videos:
                videos_id = videos["id"]
            else:
                videos_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO collections
                       (id,name,created_at,created_by,owner_id,owner_name,visibility)
                       VALUES (?,?,?,?,?,?,?)""",
                    (videos_id, "Videos", created_at, parameters["created_by"],
                     parameters.get("owner_id"), parameters["created_by"], parameters.get("visibility", "shared")),
                )
            connection.execute(
                "INSERT INTO photos (id, original_name, stored_path, preview_name, thumb_name, content_type, "
                "byte_size, sha256, taken_at, uploaded_at, uploaded_by, playback_name, loop_playback, "
                "owner_id, owner_name, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    photo_id, original_name, str(relative), preview_name, thumb_name, "video/mp4",
                    destination.stat().st_size, digest.hexdigest(), created_at, created_at,
                    parameters["created_by"], playback_name, 1 if parameters["loop_playback"] else 0,
                    parameters.get("owner_id"), parameters["created_by"], parameters.get("visibility", "shared"),
                ),
            )
            connection.execute(
                "INSERT INTO collection_photos VALUES (?, ?, ?)",
                (videos_id, photo_id, created_at),
            )
        update_slideshow_job(
            job_id, status="completed", progress=100, message="Your slideshow is ready.",
            result_photo_id=photo_id,
        )
    except Exception as error:
        temp_path.unlink(missing_ok=True)
        if preview_name:
            (PREVIEWS / preview_name).unlink(missing_ok=True)
        if thumb_name:
            (THUMBS / thumb_name).unlink(missing_ok=True)
        if playback_name:
            (PREVIEWS / playback_name).unlink(missing_ok=True)
        update_slideshow_job(
            job_id, status="failed", progress=0, message="The slideshow could not be created.",
            error="Video generation failed safely. No source media was changed.",
        )
    finally:
        for path in balanced_paths:
            path.unlink(missing_ok=True)


@app.get("/api/slideshows/options")
def slideshow_options():
    collection_visible, collection_params = visibility_sql("c")
    photo_visible, photo_params = visibility_sql("p")
    with db() as connection:
        rows = connection.execute(
            "SELECT c.id, c.name, COUNT(p.id) AS item_count, "
            "SUM(CASE WHEN p.content_type LIKE 'video/%' THEN 1 ELSE 0 END) AS video_count, "
            "SUM(CASE WHEN p.id IS NOT NULL AND p.content_type NOT LIKE 'video/%' THEN 1 ELSE 0 END) AS image_count "
            "FROM collections c "
            "LEFT JOIN collection_photos cp ON cp.collection_id = c.id "
            f"LEFT JOIN photos p ON p.id = cp.photo_id AND p.deleted_at IS NULL AND {photo_visible} "
            f"WHERE {collection_visible} GROUP BY c.id ORDER BY lower(c.name)",
            (*photo_params, *collection_params),
        ).fetchall()
    return jsonify(
        collections=[dict(row) for row in rows if row["item_count"]],
        music=[
            {
                **{key: track[key] for key in ("id", "title", "artist", "license", "source")},
                "preview_url": f"/api/slideshows/music/{track['id']}",
            }
            for track in SLIDESHOW_MUSIC
            if (MUSIC_LIBRARY / track["filename"]).is_file()
        ],
    )


@app.get("/api/slideshows/music/<track_id>")
def slideshow_music(track_id):
    track = next((track for track in SLIDESHOW_MUSIC if track["id"] == track_id), None)
    if not track or not (MUSIC_LIBRARY / track["filename"]).is_file():
        return jsonify(error="Music track not found."), 404
    return send_from_directory(
        MUSIC_LIBRARY, track["filename"], mimetype="audio/mpeg", conditional=True, max_age=86400
    )


@app.post("/api/slideshows")
def create_slideshow():
    data = request.get_json(silent=True) or {}
    collection_id = str(data.get("collection_id", "")).strip()
    transition = str(data.get("transition", "fade")).strip().lower()
    layout = str(data.get("layout", "balanced")).strip().lower()
    music_id = str(data.get("music_id", "")).strip()
    try:
        duration_seconds = int(data.get("duration_seconds", 30))
    except (TypeError, ValueError):
        duration_seconds = 0
    if not collection_id:
        return jsonify(error="Choose a media collection."), 400
    if transition not in SLIDESHOW_TRANSITIONS:
        return jsonify(error="Choose a supported transition."), 400
    if layout not in SLIDESHOW_LAYOUTS:
        return jsonify(error="Choose a supported screen layout."), 400
    if music_id and music_id not in {track["id"] for track in SLIDESHOW_MUSIC}:
        return jsonify(error="Choose music from the David-Pi library."), 400
    if music_id:
        track = next(track for track in SLIDESHOW_MUSIC if track["id"] == music_id)
        if not (MUSIC_LIBRARY / track["filename"]).is_file():
            return jsonify(error="That music track is not available."), 400
    if duration_seconds < 5 or duration_seconds > 1200:
        return jsonify(error="Choose a total time between 5 seconds and 20 minutes."), 400

    with db() as connection:
        collection_visible, collection_params = visibility_sql()
        collection = connection.execute(
            f"SELECT * FROM collections WHERE id=? AND {collection_visible}",
            (collection_id, *collection_params),
        ).fetchone()
        if not collection:
            return jsonify(error="That collection could not be found."), 404
        photo_visible, photo_params = visibility_sql("p")
        rows = connection.execute(
            "SELECT p.id FROM collection_photos cp JOIN photos p ON p.id = cp.photo_id "
            f"WHERE cp.collection_id = ? AND p.deleted_at IS NULL AND {photo_visible} "
            "ORDER BY p.taken_at ASC, p.uploaded_at ASC",
            (collection_id, *photo_params),
        ).fetchall()
    if not rows:
        return jsonify(error="This collection does not contain any media."), 400
    if len(rows) > SLIDESHOW_MAX_ITEMS:
        return jsonify(error=f"Choose a collection with no more than {SLIDESHOW_MAX_ITEMS} items."), 400
    if shutil.disk_usage(DATA).free < 1024 * 1024 * 1024:
        return jsonify(error="David-Pi needs at least 1 GB free before creating a slideshow."), 507

    job_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    parameters = {
        "collection_id": collection_id,
        "source_name": collection["name"],
        "media_ids": [row["id"] for row in rows],
        "duration_seconds": duration_seconds,
        "transition": transition,
        "layout": layout,
        "music_id": music_id or None,
        "loop_playback": bool(data.get("loop_playback")),
        "created_by": identity(),
        "owner_id": current_device()["owner_id"],
        "visibility": collection["visibility"],
    }
    parameters_json = json.dumps(parameters, sort_keys=True)
    with db() as connection:
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM slideshow_jobs WHERE status IN ('queued','working')"
        ).fetchone()[0]
        if pending_count >= 3:
            return jsonify(error="The video queue is full. Try again after one finishes."), 429
        duplicate = connection.execute(
            "SELECT id FROM slideshow_jobs WHERE status IN ('queued','working') "
            "AND parameters_json = ? LIMIT 1",
            (parameters_json,),
        ).fetchone()
        if duplicate:
            return jsonify(id=duplicate["id"], status="queued", duplicate=True), 202
        connection.execute(
            """INSERT INTO slideshow_jobs
               (id,status,progress,message,parameters_json,result_photo_id,error,created_at,updated_at,
                owner_id,owner_name,visibility)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, "queued", 5, "Getting your media ready…", parameters_json, None, None, now, now,
                current_device()["owner_id"], current_device()["name"], collection["visibility"],
            ),
        )
    threading.Thread(target=generate_slideshow, args=(job_id,), daemon=True).start()
    return jsonify(id=job_id, status="queued"), 202


@app.get("/api/slideshows/<job_id>")
def slideshow_status(job_id):
    visible, parameters = visibility_sql()
    with db() as connection:
        row = connection.execute(
            f"""SELECT id, status, progress, message, result_photo_id, error
                FROM slideshow_jobs WHERE id = ? AND {visible}""",
            (job_id, *parameters),
        ).fetchone()
    if not row:
        return jsonify(error="Slideshow job not found."), 404
    return jsonify(**dict(row))


@app.get("/api/photos/<photo_id>/collections")
def photo_collections(photo_id):
    visible, parameters = visibility_sql("c")
    with db() as connection:
        if not validate_active_photos(connection, [photo_id]):
            return jsonify(error="Photo not found."), 404
        rows = connection.execute(
            "SELECT c.id, c.name, CASE WHEN cp.photo_id IS NULL THEN 0 ELSE 1 END AS selected "
            "FROM collections c LEFT JOIN collection_photos cp ON cp.collection_id = c.id AND cp.photo_id = ? "
            f"WHERE {visible} ORDER BY lower(c.name)", (photo_id, *parameters)
        ).fetchall()
    return jsonify(collections=[dict(row) for row in rows])


def validate_active_photos(connection, ids):
    placeholders = ",".join("?" for _ in ids)
    visible, parameters = visibility_sql()
    count = connection.execute(
        f"SELECT COUNT(*) FROM photos WHERE id IN ({placeholders}) AND deleted_at IS NULL AND {visible}",
        (*ids, *parameters),
    ).fetchone()[0]
    return count == len(ids)


@app.post("/api/collections/membership-state")
def collection_membership_state():
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    placeholders = ",".join("?" for _ in ids)
    with db() as connection:
        if not validate_active_photos(connection, ids):
            return jsonify(error="One or more photos could not be found."), 404
        rows = connection.execute(
            "SELECT c.id, c.name, COUNT(cp.photo_id) AS selected_count "
            "FROM collections c LEFT JOIN collection_photos cp "
            f"ON cp.collection_id = c.id AND cp.photo_id IN ({placeholders}) "
            f"WHERE {visibility_sql('c')[0]} GROUP BY c.id ORDER BY lower(c.name)",
            (*ids, *visibility_sql("c")[1]),
        ).fetchall()
    collections = []
    for row in rows:
        selected_count = row["selected_count"]
        state = "all" if selected_count == len(ids) else "none" if selected_count == 0 else "mixed"
        collections.append({"id": row["id"], "name": row["name"], "state": state, "selected_count": selected_count})
    return jsonify(collections=collections, photo_count=len(ids))


@app.post("/api/collections/membership")
def update_collection_membership():
    data = request.get_json(silent=True) or {}
    ids = requested_photo_ids()
    changes = data.get("changes", [])
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    if not isinstance(changes, list) or len(changes) > 500:
        return jsonify(error="Collection changes are invalid."), 400

    normalized = {}
    for change in changes:
        if not isinstance(change, dict):
            return jsonify(error="Collection changes are invalid."), 400
        collection_id = str(change.get("collection_id", "")).strip()
        action = change.get("action")
        if not collection_id or action not in ("add", "remove"):
            return jsonify(error="Collection changes are invalid."), 400
        normalized[collection_id] = action
    if not normalized:
        return jsonify(ok=True, added=0, removed=0, changed_collections=0)

    collection_ids = list(normalized)
    photo_placeholders = ",".join("?" for _ in ids)
    collection_placeholders = ",".join("?" for _ in collection_ids)
    now = datetime.now(timezone.utc).isoformat()
    added = removed = 0
    try:
        with db() as connection:
            if not validate_active_photos(connection, ids):
                return jsonify(error="One or more photos could not be found."), 404
            collection_visible, collection_parameters = visibility_sql()
            found = connection.execute(
                f"SELECT COUNT(*) FROM collections WHERE id IN ({collection_placeholders}) AND {collection_visible}",
                (*collection_ids, *collection_parameters),
            ).fetchone()[0]
            if found != len(collection_ids):
                return jsonify(error="One or more collections could not be found."), 404
            for collection_id, action in normalized.items():
                if action == "add":
                    before = connection.total_changes
                    connection.executemany(
                        "INSERT OR IGNORE INTO collection_photos VALUES (?, ?, ?)",
                        [(collection_id, photo_id, now) for photo_id in ids],
                    )
                    added += connection.total_changes - before
                else:
                    result = connection.execute(
                        f"DELETE FROM collection_photos WHERE collection_id = ? AND photo_id IN ({photo_placeholders})",
                        (collection_id, *ids),
                    )
                    removed += result.rowcount
    except sqlite3.IntegrityError:
        return jsonify(error="The collection changes could not be saved."), 409
    return jsonify(ok=True, added=added, removed=removed, changed_collections=len(normalized))


@app.put("/api/collections/<collection_id>/photos/<photo_id>")
def add_photo_to_collection(collection_id, photo_id):
    try:
        with db() as connection:
            if not validate_active_photos(connection, [photo_id]):
                return jsonify(error="Photo not found."), 404
            collection_visible, parameters = visibility_sql()
            if not connection.execute(
                f"SELECT 1 FROM collections WHERE id=? AND {collection_visible}",
                (collection_id, *parameters),
            ).fetchone():
                return jsonify(error="Collection not found."), 404
            connection.execute(
                "INSERT OR IGNORE INTO collection_photos VALUES (?, ?, ?)",
                (collection_id, photo_id, datetime.now(timezone.utc).isoformat()),
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Photo or collection not found."), 404
    return jsonify(ok=True)


@app.post("/api/collections/<collection_id>/photos")
def add_photos_to_collection(collection_id):
    ids = requested_photo_ids()
    if not ids:
        return jsonify(error="Choose at least one photo."), 400
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db() as connection:
            collection_visible, parameters = visibility_sql()
            exists = connection.execute(
                f"SELECT id FROM collections WHERE id = ? AND {collection_visible}",
                (collection_id, *parameters),
            ).fetchone()
            if not exists:
                return jsonify(error="Collection not found."), 404
            if not validate_active_photos(connection, ids):
                return jsonify(error="One or more photos could not be found."), 404
            connection.executemany(
                "INSERT OR IGNORE INTO collection_photos VALUES (?, ?, ?)",
                [(collection_id, photo_id, now) for photo_id in ids],
            )
    except sqlite3.IntegrityError:
        return jsonify(error="One or more photos could not be found."), 404
    return jsonify(ok=True, count=len(ids))


@app.delete("/api/collections/<collection_id>/photos/<photo_id>")
def remove_photo_from_collection(collection_id, photo_id):
    collection_visible, parameters = visibility_sql()
    with db() as connection:
        if not connection.execute(
            f"SELECT 1 FROM collections WHERE id=? AND {collection_visible}",
            (collection_id, *parameters),
        ).fetchone():
            return jsonify(error="Collection not found."), 404
        connection.execute(
            "DELETE FROM collection_photos WHERE collection_id = ? AND photo_id = ?",
            (collection_id, photo_id),
        )
    return jsonify(ok=True)


@app.post("/api/upload")
def upload():
    files = request.files.getlist("media") or request.files.getlist("photos")
    if not files:
        return jsonify(error="Choose at least one photo or video."), 400
    if len(files) > MAX_MEDIA_FILES:
        return jsonify(error=f"Choose no more than {MAX_MEDIA_FILES} media items at once."), 413
    content_length = request.content_length or 0
    if content_length and shutil.disk_usage(DATA).free < content_length * 2 + 512 * 1024 * 1024:
        return jsonify(error="David-Pi does not have enough free storage for this upload."), 507

    try:
        visibility = requested_visibility(request.form.get("visibility", "shared"))
    except ValueError as error:
        return jsonify(error=str(error)), 400
    actor = current_device()
    collection_id = request.form.get("collection_id", "").strip()
    collection = None
    if collection_id:
        collection_visible, collection_params = visibility_sql()
        with db() as connection:
            collection = connection.execute(
                f"SELECT * FROM collections WHERE id = ? AND {collection_visible}",
                (collection_id, *collection_params),
            ).fetchone()
        if not collection:
            return jsonify(error="That collection could not be found."), 404
        if collection["visibility"] == "private":
            if not actor["owner_id"]:
                return jsonify(error="Open David-Pi through its private Tailscale address."), 401
            visibility = "private"
    added, duplicates, errors = [], [], []
    for item in files:
        original_name = secure_filename(item.filename or "media") or "media"
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED:
            errors.append({"name": original_name, "reason": "Unsupported photo or video format"})
            continue

        photo_id = uuid.uuid4().hex
        temp_path = INCOMING / f"{photo_id}.part"
        try:
            item.save(temp_path)
            uploaded_size = temp_path.stat().st_size
            if uploaded_size <= 0:
                raise ValueError("Empty media files cannot be uploaded.")
            if uploaded_size > MAX_MEDIA_FILE_BYTES:
                raise ValueError("This media file exceeds the 2 GB file limit.")
            free_space = shutil.disk_usage(DATA).free
            required_space = uploaded_size * (2 if extension in VIDEO_ALLOWED else 1) + 512 * 1024 * 1024
            if free_space < required_space:
                raise ValueError("David-Pi does not have enough free space for this file and its preview.")
            digest = hashlib.sha256()
            with temp_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            checksum = digest.hexdigest()

            with db() as connection:
                existing = connection.execute(
                    """SELECT id FROM photos
                       WHERE COALESCE(content_sha256,sha256)=?
                         AND COALESCE(owner_id,'')=COALESCE(?, '')
                         AND deleted_at IS NULL LIMIT 1""",
                    (checksum, actor["owner_id"]),
                ).fetchone()
            if existing:
                duplicates.append(original_name)
                temp_path.unlink(missing_ok=True)
                continue
            result = canonical_ingest_media(
                staged_path=temp_path,
                original_filename=original_name,
                mime_type=item.mimetype or "application/octet-stream",
                owner_user_id=actor["owner_id"],
                owner_name=actor["name"],
                visibility=visibility,
                ingestion_source="manual_upload",
                authoritative_sha256=checksum,
                authoritative_size=uploaded_size,
                collection_id=collection_id or None,
            )
            added.append({"id": result["id"], "name": original_name})
        except (UnidentifiedImageError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            temp_path.unlink(missing_ok=True)
            (PREVIEWS / f"{photo_id}.jpg").unlink(missing_ok=True)
            (PREVIEWS / f"{photo_id}.mp4").unlink(missing_ok=True)
            (THUMBS / f"{photo_id}.jpg").unlink(missing_ok=True)
            errors.append({"name": original_name, "reason": "This media item could not be processed safely."})

    return jsonify(
        added=[item["name"] for item in added],
        added_items=added,
        duplicates=duplicates,
        errors=errors,
        collection_id=collection_id or None,
    )


def photo_row(photo_id, include_deleted=False):
    visible, parameters = visibility_sql()
    with db() as connection:
        condition = "" if include_deleted else " AND deleted_at IS NULL"
        return connection.execute(
            f"SELECT * FROM photos WHERE id = ?{condition} AND {visible}",
            (photo_id, *parameters),
        ).fetchone()


@app.get("/media/thumb/<photo_id>")
def thumb(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return send_from_directory(THUMBS, row["thumb_name"], max_age=86400)


@app.get("/media/preview/<photo_id>")
def preview(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return send_from_directory(PREVIEWS, row["preview_name"], max_age=86400)


@app.get("/media/view/<photo_id>")
def viewer_preview(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    try:
        path = ensure_viewer_preview(row["preview_name"])
        return send_file(
            path, mimetype="image/webp", conditional=True, max_age=2592000
        )
    except (OSError, ValueError, UnidentifiedImageError):
        response = send_from_directory(PREVIEWS, row["preview_name"], max_age=0)
        response.headers["X-David-Pi-Preview-Fallback"] = "1"
        return response


@app.get("/media/original/<photo_id>")
def original(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return send_from_directory(ORIGINALS, row["stored_path"], as_attachment=True, download_name=row["original_name"])


@app.get("/media/play/<photo_id>")
def play(photo_id):
    row = photo_row(photo_id)
    if not row or not row["content_type"].startswith("video/"):
        return "Not found", 404
    if row["playback_name"]:
        return send_from_directory(PREVIEWS, row["playback_name"], mimetype="video/mp4", conditional=True)
    return send_from_directory(ORIGINALS, row["stored_path"], mimetype=row["content_type"], conditional=True)


@app.get("/media/deleted/<kind>/<photo_id>")
def deleted_media(kind, photo_id):
    if kind not in {"thumb", "preview", "view", "original", "play"}:
        return "Not found", 404
    row = photo_row(photo_id, include_deleted=True)
    if not row or row["deleted_at"] is None:
        return "Not found", 404
    if kind == "thumb":
        return send_from_directory(THUMBS, row["thumb_name"], max_age=0)
    if kind == "preview":
        return send_from_directory(PREVIEWS, row["preview_name"], max_age=0)
    if kind == "view":
        try:
            path = ensure_viewer_preview(row["preview_name"])
            return send_file(path, mimetype="image/webp", conditional=True, max_age=0)
        except (OSError, ValueError, UnidentifiedImageError):
            return send_from_directory(PREVIEWS, row["preview_name"], max_age=0)
    if kind == "original":
        return send_from_directory(
            ORIGINALS, row["stored_path"], as_attachment=True,
            download_name=row["original_name"], max_age=0,
        )
    if not row["content_type"].startswith("video/"):
        return "Not found", 404
    if row["playback_name"]:
        return send_from_directory(
            PREVIEWS, row["playback_name"], mimetype="video/mp4",
            conditional=True, max_age=0,
        )
    return send_from_directory(
        ORIGINALS, row["stored_path"], mimetype=row["content_type"],
        conditional=True, max_age=0,
    )


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
