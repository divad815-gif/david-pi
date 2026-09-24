import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename
from modules.installation import get_installation, module_enabled, substrate_enabled, display_name, InstallationError
from modules.secure_storage import (
    PinnedStorageRoot,
    StorageSafetyError,
    ensure_restricted_directory,
    identity as storage_identity,
    safe_component,
    safe_relative_parts,
)
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
HISTORY_METRICS_DB = Path(os.environ.get("DAVID_PI_HISTORY_METRICS_DB", METRICS_DB))
MUSIC_LIBRARY = Path(os.environ.get("DAVID_PI_MUSIC_LIBRARY", Path(__file__).parent / "assets" / "music"))
HOST_PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
HOST_SYS = Path(os.environ.get("HOST_SYS", "/host/sys"))
PIHOLE_SUMMARY = Path(os.environ.get("PIHOLE_SUMMARY", "/run/david-pi/pihole-summary.json"))
BACKUP_STATUS = Path(os.environ.get("DAVID_PI_BACKUP_STATUS", "/run/david-pi/backup-status.json"))
SERVER_STATUS = Path(os.environ.get("DAVID_PI_SERVER_STATUS", "/run/david-pi/server-status.json"))
DATA_SENTINEL = Path(os.environ.get("DAVID_PI_DATA_SENTINEL", DATA / ".david-pi-storage"))
DATA_SENTINEL_VALUE = os.environ.get("DAVID_PI_DATA_ID", "david-pi-family-storage-v1")
READINESS_MAX_STATUS_AGE_SECONDS = 10 * 60
IMAGE_ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".tif", ".tiff"}
VIDEO_ALLOWED = {".mp4", ".mov", ".m4v"}
ALLOWED = IMAGE_ALLOWED | VIDEO_ALLOWED
SLIDESHOW_EXECUTOR_MODE = os.environ.get(
    "DAVID_PI_SLIDESHOW_EXECUTOR_MODE", "queue"
).strip().lower()
IS_SLIDESHOW_WORKER = (
    os.environ.get("DAVID_PI_WORKER_MODE", "").strip().lower() == "slideshow"
    and SLIDESHOW_EXECUTOR_MODE == "worker"
)

_media_directories = (ORIGINALS, PREVIEWS, VIEWER_PREVIEWS, THUMBS) if substrate_enabled("media") else ()
for directory in (DATA, INCOMING, QUARANTINE, *_media_directories):
    # The slideshow executor must never bootstrap or repair application
    # storage. Its entrypoint and pinned roots require the portal-provisioned
    # directories to exist already, so a missing path fails activation.
    if not IS_SLIDESHOW_WORKER:
        ensure_restricted_directory(directory)

LOGGER = logging.getLogger(__name__)
DATA_STORAGE = PinnedStorageRoot(DATA)
ORIGINAL_STORAGE = PinnedStorageRoot(ORIGINALS) if substrate_enabled("media") else None
PREVIEW_STORAGE = PinnedStorageRoot(PREVIEWS) if substrate_enabled("media") else None
VIEWER_PREVIEW_STORAGE = PinnedStorageRoot(VIEWER_PREVIEWS) if substrate_enabled("media") else None
THUMB_STORAGE = PinnedStorageRoot(THUMBS) if substrate_enabled("media") else None
INCOMING_STORAGE = PinnedStorageRoot(INCOMING)
QUARANTINE_STORAGE = PinnedStorageRoot(QUARANTINE)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024
MAX_MEDIA_FILES = 200
MAX_MEDIA_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_INLINE_FULL_IMAGE_BYTES = 50 * 1024 * 1024
try:
    MEDIA_COMMAND_TIMEOUT_SECONDS = min(
        max(int(os.environ.get("DAVID_PI_MEDIA_COMMAND_TIMEOUT", "14400")), 30),
        14400,
    )
except ValueError:
    MEDIA_COMMAND_TIMEOUT_SECONDS = 14400
SHUTDOWN_REQUEST = DATA / "platform" / "control" / "shutdown.request"
SHUTDOWN_PASSWORD_ENV = "DAVID_PI_SHUTDOWN_PASSWORD_HASH_B64"
SHUTDOWN_FAILURE_LIMIT = 5
SHUTDOWN_FAILURE_WINDOW_SECONDS = 15 * 60

from modules.content_ownership import (
    actor_for_identity,
    audit_mutation,
    authorize,
    mutation_policy,
    row_is_visible,
)
from modules.content_policy import Actor, AuthorizationFacts, decide_transaction_authorization
from modules.platform import (
    MigrationStep,
    apply_domain_migrations,
    connect,
    initialize_data_foundation,
    utcnow,
)
if not IS_SLIDESHOW_WORKER:
    # These portal modules either register HTTP behavior or migrate their own
    # databases at import time. The no-network renderer must not initialize
    # unrelated domains while adopting a pre-provisioned slideshow queue.
    from modules.security import init_security
    from modules.identity import current_device, init_identity
    from modules.access_control import init_access_control
    from modules.portal_configuration import init_portal_configuration
    from importlib import import_module

    init_security(app)
    init_identity(app)
    init_access_control(app)
    init_portal_configuration(app)
    for _module in ("files", "notes", "movies", "recipes", "places", "audiobooks", "games"):
        if module_enabled(_module):
            getattr(import_module(f"modules.{_module}"), f"init_{_module}")(app)
    if module_enabled("assistant"):
        from modules.assistant import init_assistant, seed_knowledge



@contextmanager
def db():
    with connect(DB_PATH) as connection:
        yield connection


MEDIA_METADATA_MIGRATIONS = (
    MigrationStep.from_sql(
        1,
        "add-caption-and-personal-media-state",
        (
            """CREATE TABLE media_captions (
                photo_id TEXT PRIMARY KEY REFERENCES photos(id) ON DELETE CASCADE,
                caption TEXT NOT NULL,
                version INTEGER NOT NULL CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE media_personal_state (
                photo_id TEXT NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
                principal_id TEXT NOT NULL,
                favorite INTEGER NOT NULL CHECK(favorite IN (0,1)),
                version INTEGER NOT NULL CHECK(version >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(photo_id, principal_id)
            )""",
            """CREATE INDEX media_personal_favorite_idx
                ON media_personal_state(principal_id, favorite, photo_id)""",
        ),
    ),
)


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
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("deleted_by_id", "TEXT"),
            ("purge_after", "TEXT"),
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
            """CREATE TABLE IF NOT EXISTS mytube_media_links (
                media_id TEXT PRIMARY KEY REFERENCES photos(id) ON DELETE RESTRICT,
                video_id TEXT NOT NULL UNIQUE,
                linked_by_id TEXT NOT NULL,
                linked_at TEXT NOT NULL
            )"""
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
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("deleted_at", "TEXT"),
            ("deleted_by_id", "TEXT"),
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
                added_by_id TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (collection_id, photo_id)
            )
            """
        )
        membership_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(collection_photos)")
        }
        for name, definition in (
            ("added_by_id", "TEXT"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in membership_columns:
                try:
                    connection.execute(
                        f"ALTER TABLE collection_photos ADD COLUMN {name} {definition}"
                    )
                except sqlite3.OperationalError:
                    current = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(collection_photos)"
                        )
                    }
                    if name not in current:
                        raise
        connection.execute("CREATE INDEX IF NOT EXISTS collection_photo_idx ON collection_photos(photo_id)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS media_publish_intents (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                preview_name TEXT NOT NULL,
                thumb_name TEXT NOT NULL,
                playback_name TEXT,
                content_type TEXT NOT NULL,
                byte_size INTEGER NOT NULL CHECK(byte_size > 0),
                content_sha256 TEXT NOT NULL,
                legacy_sha256 TEXT NOT NULL,
                taken_at TEXT NOT NULL,
                capture_timestamp TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                uploaded_by TEXT NOT NULL,
                visibility TEXT NOT NULL CHECK(visibility IN ('shared','private')),
                source_device_id TEXT,
                ingestion_source TEXT NOT NULL,
                loop_playback INTEGER NOT NULL DEFAULT 0,
                collection_id TEXT,
                collection_version INTEGER,
                collection_visibility TEXT,
                source_path TEXT NOT NULL,
                artifacts_json TEXT NOT NULL,
                requires_live_validator INTEGER NOT NULL DEFAULT 0,
                requires_publication_callback INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK(state IN ('prepared','committed')),
                created_at TEXT NOT NULL,
                committed_at TEXT
            )
            """
        )
        media_intent_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(media_publish_intents)")
        }
        if "requires_publication_callback" not in media_intent_columns:
            connection.execute(
                "ALTER TABLE media_publish_intents ADD COLUMN "
                "requires_publication_callback INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS media_publish_intents_recovery_idx
               ON media_publish_intents(state,created_at,id)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS media_publish_intents_owner_idx
               ON media_publish_intents(owner_id,state,created_at)"""
        )
        # Older releases populated one global, unowned Videos collection at
        # startup.  Leave those legacy rows readable but immutable; assigning
        # them to whichever person happens to start this release would silently
        # transfer saved household content. New videos use an owned collection
        # in canonical_ingest_media instead.
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
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("generation", "INTEGER NOT NULL DEFAULT 1"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_expires_at", "TEXT"),
            ("target_photo_id", "TEXT"),
            ("target_name", "TEXT"),
            ("target_dev", "INTEGER"),
            ("target_ino", "INTEGER"),
            ("target_size", "INTEGER"),
            ("target_sha256", "TEXT"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
            ("source_snapshot_sha256", "TEXT"),
            ("publish_intent_id", "TEXT"),
            ("publish_state", "TEXT NOT NULL DEFAULT 'none'"),
            ("failure_code", "TEXT"),
        ):
            if name not in job_columns:
                try:
                    connection.execute(f"ALTER TABLE slideshow_jobs ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError:
                    current = {row[1] for row in connection.execute("PRAGMA table_info(slideshow_jobs)")}
                    if name not in current:
                        raise
        connection.execute(
            """CREATE INDEX IF NOT EXISTS slideshow_jobs_queue_idx
               ON slideshow_jobs(status,created_at,id)"""
        )
        connection.execute(
            """CREATE INDEX IF NOT EXISTS slideshow_jobs_lease_idx
               ON slideshow_jobs(status,lease_expires_at,id)"""
        )
        initialize_data_foundation(connection)


def initialize():
    for attempt in range(10):
        try:
            initialize_once()
            apply_domain_migrations(
                DB_PATH,
                "media_metadata",
                MEDIA_METADATA_MIGRATIONS,
            )
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


if not IS_SLIDESHOW_WORKER and substrate_enabled("media"):
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


@contextmanager
def history_metrics_db():
    """Open operational history read-only; the portal never owns its writes."""
    connection = sqlite3.connect(
        f"file:{HISTORY_METRICS_DB}?mode=ro", uri=True, timeout=5
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
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


if not IS_SLIDESHOW_WORKER:
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
    load = power.get("load_average")

    def number(value):
        try:
            parsed = float(str(value).replace("%", "").strip())
            return parsed if parsed == parsed and abs(parsed) != float("inf") else None
        except (TypeError, ValueError):
            return None

    memory_total = number(power.get("ram_total_gb"))
    memory_available = number(power.get("ram_available_gb"))
    memory_used = None
    memory_used_gb = None
    if memory_total is not None and memory_total > 0 and memory_available is not None:
        memory_used = round(100 * (1 - memory_available / memory_total), 1)
        memory_used_gb = round(memory_total - memory_available, 2)
    load_values = [number(value) for value in load[:3]] if isinstance(load, list) and len(load) >= 3 else [None, None, None]
    host_uptime_value = number(power.get("host_uptime_seconds"))
    portal_uptime_value = number(portal.get("uptime_seconds"))
    host_uptime = int(host_uptime_value) if host_uptime_value is not None and host_uptime_value >= 0 else None
    portal_uptime = int(portal_uptime_value) if portal_uptime_value is not None and portal_uptime_value >= 0 else None
    cpu = number(portal.get("cpu_percent"))
    disk_used = number(external.get("used_percent"))
    return {
        "timestamp": int(time.time()),
        "cpu": round(cpu, 1) if cpu is not None else None,
        "memory": memory_used,
        "memory_used_gb": memory_used_gb,
        "memory_total_gb": round(memory_total, 2) if memory_total is not None else None,
        "temperature": number(power.get("temperature_c")),
        "disk_used": disk_used,
        "disk_used_gb": number(external.get("used_gb")),
        "disk_total_gb": number(external.get("total_gb")),
        "disk_free_gb": number(external.get("free_gb")),
        "load1": round(load_values[0], 2) if load_values[0] is not None else None,
        "load5": round(load_values[1], 2) if load_values[1] is not None else None,
        "load15": round(load_values[2], 2) if load_values[2] is not None else None,
        "uptime": host_uptime if host_uptime is not None else portal_uptime,
        "host_uptime": host_uptime,
        "portal_uptime": portal_uptime,
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
        "access_control",
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


def identity():
    return current_device()["name"]


def home_recent_storage_scope(identity_value):
    """Return an opaque, stable browser-storage scope for one principal."""
    principal_id = str(identity_value.get("owner_id") or "").strip().casefold()
    if not principal_id:
        return ""
    return hashlib.sha256(
        f"david-pi:home-recents:v2\0{principal_id}".encode("utf-8")
    ).hexdigest()[:32]


def visibility_sql(alias=""):
    prefix = f"{alias}." if alias else ""
    identity = current_device()
    actor = actor_for_identity(identity)
    if actor.principal_id and actor.role in {"admin", "household"}:
        return (
            f"({prefix}visibility = 'shared' OR {prefix}owner_id = ?)",
            [actor.principal_id],
        )
    return "0 = 1", []


def ownership_sql(alias=""):
    """Restrict a mutation to records owned by the current Tailscale identity."""
    prefix = f"{alias}." if alias else ""
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return "0 = 1", []
    return f"{prefix}owner_id = ?", [actor.principal_id]


def requested_visibility(value, actor=None):
    value = str(value or "shared").lower()
    if value not in ("shared", "private"):
        raise ValueError("Choose Shared or Only me.")
    actor = actor or actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        raise ValueError("Open David-Pi through an approved private Tailscale account.")
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


def descriptor_sha256(descriptor):
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _stage_image(image, image_format, **save_options):
    """Encode an image into an unguessable descriptor-created staging entry."""
    temp_name = safe_component(f".media-{uuid.uuid4().hex}.tmp")
    descriptor = -1
    created_identity = None
    try:
        descriptor, metadata = INCOMING_STORAGE.create_regular(temp_name)
        created_identity = storage_identity(metadata)
        with os.fdopen(descriptor, "wb") as target:
            descriptor = -1
            image.save(target, image_format, **save_options)
            target.flush()
            os.fsync(target.fileno())
        return temp_name, created_identity
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if created_identity is not None:
            try:
                INCOMING_STORAGE.unlink_if_identity(temp_name, created_identity)
            except (OSError, StorageSafetyError):
                pass
        raise


def _publish_staged_artifact(temp_name, target_storage, target_name):
    """Publish the exact staged inode, tolerating an identical concurrent result."""
    source_descriptor = target_descriptor = -1
    try:
        source_descriptor, source_metadata = INCOMING_STORAGE.open_regular(temp_name)
        try:
            target_storage.link_descriptor(source_descriptor, target_name)
        except FileExistsError:
            pass
        target_descriptor, target_metadata = target_storage.open_regular(target_name)
        if storage_identity(target_metadata) != storage_identity(source_metadata):
            raise StorageSafetyError("Published media artifact collided with another inode")
        return target_metadata
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if source_descriptor >= 0:
            try:
                source_metadata = os.fstat(source_descriptor)
                INCOMING_STORAGE.unlink_if_identity(
                    temp_name, storage_identity(source_metadata)
                )
            except (OSError, StorageSafetyError):
                pass
            os.close(source_descriptor)


def publish_jpeg(image, storage, name, max_size, quality):
    temp_name = stage_jpeg(image, max_size, quality)
    return _publish_staged_artifact(temp_name, storage, safe_component(name))


def stage_jpeg(image, max_size, quality):
    """Encode a JPEG in incoming storage without publishing its final name."""
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
    temp_name, _created_identity = _stage_image(
        converted, "JPEG", quality=quality, optimize=True
    )
    return temp_name


def _unlink_staged_artifact(source_path):
    descriptor = -1
    try:
        descriptor, metadata = DATA_STORAGE.open_regular_path(source_path)
        DATA_STORAGE.unlink_if_identity_path(source_path, storage_identity(metadata))
    except (FileNotFoundError, OSError, StorageSafetyError):
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def viewer_preview_name(preview_name):
    """Return the immutable phone-viewer derivative for a physical preview."""
    return f"{Path(str(preview_name)).stem}.webp"


def ensure_viewer_preview(preview_name):
    """Create a bounded WebP viewer copy without changing the original or preview."""
    preview_name = safe_component(preview_name)
    destination_name = safe_component(viewer_preview_name(preview_name))
    existing_descriptor = source_descriptor = -1
    try:
        try:
            existing_descriptor, metadata = VIEWER_PREVIEW_STORAGE.open_regular(
                destination_name
            )
            if metadata.st_size:
                return VIEWER_PREVIEWS / destination_name
            raise StorageSafetyError("Viewer derivative is empty")
        except FileNotFoundError:
            pass
        finally:
            if existing_descriptor >= 0:
                os.close(existing_descriptor)
                existing_descriptor = -1

        source_descriptor, _metadata = PREVIEW_STORAGE.open_regular(preview_name)
        with os.fdopen(os.dup(source_descriptor), "rb") as source:
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
                temp_name, _created_identity = _stage_image(
                    converted, "WEBP", quality=80, method=4
                )
        try:
            _publish_staged_artifact(
                temp_name, VIEWER_PREVIEW_STORAGE, destination_name
            )
        except StorageSafetyError:
            # A concurrent worker may have completed the same immutable
            # derivative. Accept it only after a fresh descriptor-safe open.
            descriptor, metadata = VIEWER_PREVIEW_STORAGE.open_regular(
                destination_name
            )
            os.close(descriptor)
            if not metadata.st_size:
                raise
        return VIEWER_PREVIEWS / destination_name
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)


def backfill_viewer_previews(limit=None):
    """Warm missing viewer derivatives; safe to resume after interruption."""
    with db() as connection:
        rows = connection.execute(
            """SELECT DISTINCT preview_name FROM photos
               WHERE preview_name IS NOT NULL ORDER BY preview_name"""
        ).fetchall()
    created = skipped = failed = 0
    for row in rows[:limit] if limit else rows:
        try:
            preview_name = safe_component(row["preview_name"])
            destination_name = safe_component(
                viewer_preview_name(preview_name)
            )
            descriptor = -1
            try:
                descriptor, metadata = VIEWER_PREVIEW_STORAGE.open_regular(
                    destination_name
                )
                if metadata.st_size:
                    skipped += 1
                    continue
            except FileNotFoundError:
                pass
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            ensure_viewer_preview(preview_name)
            created += 1
        except (OSError, ValueError, UnidentifiedImageError):
            failed += 1
    return {"created": created, "skipped": skipped, "failed": failed}


def run_media_command(arguments):
    # Large HEVC originals can require more than an hour to decode and create
    # a portable H.264 derivative on a thermally constrained Raspberry Pi 4.
    pass_fds = tuple(
        sorted(
            {
                int(match.group(1))
                for argument in arguments
                if (match := re.fullmatch(r"/proc/self/fd/(\d+)", str(argument)))
            }
        )
    )
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        timeout=MEDIA_COMMAND_TIMEOUT_SECONDS,
        check=False,
        pass_fds=pass_fds,
    )
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


def _run_descriptor_media_output(storage, name, arguments, output_format):
    """Render into an exclusively created inode beneath a pinned root."""
    name = safe_component(name)
    descriptor = -1
    created_identity = None
    try:
        descriptor, metadata = storage.create_regular(name)
        created_identity = storage_identity(metadata)
        run_media_command(
            [*arguments, "-f", output_format, f"/proc/self/fd/{descriptor}"]
        )
        os.fsync(descriptor)
        rendered = os.fstat(descriptor)
        if (
            rendered.st_size <= 0
            or not storage.matches_descriptor(name, descriptor)
        ):
            raise StorageSafetyError("Rendered media output changed identity")
        return rendered
    except BaseException:
        if created_identity is not None:
            try:
                storage.unlink_if_identity(name, created_identity)
            except (OSError, StorageSafetyError):
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def prepare_video(path, photo_id, extension):
    """Render video derivatives into pinned staging, never final storage.

    The caller persists the returned artifact manifest in a publish intent
    before any final name is linked. Thus termination during rendering can
    leave only unreferenced staging files, never an unjournaled final object.
    """
    preview_name = safe_component(f"{photo_id}.jpg")
    thumb_name = safe_component(f"{photo_id}.jpg")
    preview_stage = safe_component(f".media-{uuid.uuid4().hex}.tmp")
    staged_sources = []
    try:
        _run_descriptor_media_output(INCOMING_STORAGE, preview_stage, [
            # Start at the first decodable frame. Some Takeout motion-photo clips
            # are shorter than 0.1 seconds and have no frame at the old seek point.
            "ffmpeg", "-y", "-ss", "0", "-i", str(path), "-frames:v", "1",
            "-vf", "scale=2200:2200:force_original_aspect_ratio=decrease",
            "-q:v", "3", "-update", "1",
        ], "image2")
        preview_source_path = f"incoming/{preview_stage}"
        staged_sources.append(preview_source_path)

        preview_descriptor = -1
        try:
            preview_descriptor, _metadata = INCOMING_STORAGE.open_regular(
                preview_stage
            )
            with os.fdopen(os.dup(preview_descriptor), "rb") as preview_source:
                with Image.open(preview_source) as poster:
                    poster.load()
                    thumb_stage = stage_jpeg(poster.copy(), (360, 360), 78)
                    thumb_source_path = f"incoming/{thumb_stage}"
                    staged_sources.append(thumb_source_path)
        finally:
            if preview_descriptor >= 0:
                os.close(preview_descriptor)

        codecs = video_codecs(path)
        compatible = (
            codecs.get("video") == "h264"
            and codecs.get("audio") in (None, "aac", "mp3")
        )
        playback_name = None
        playback_source_path = None
        if extension != ".mp4" or not compatible:
            playback_name = safe_component(f"{photo_id}.mp4")
            playback_stage = safe_component(f".media-{uuid.uuid4().hex}.tmp")
            if compatible:
                arguments = [
                    "ffmpeg", "-y", "-i", str(path), "-map", "0:v:0",
                    "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart",
                ]
            else:
                arguments = [
                    "ffmpeg", "-y", "-i", str(path), "-map", "0:v:0",
                    "-map", "0:a:0?",
                    # Rotation metadata can turn an otherwise even iPhone frame
                    # into an odd width. libx264 requires even dimensions.
                    "-vf", (
                        "scale=1280:1280:force_original_aspect_ratio=decrease:"
                        "force_divisible_by=2"
                    ),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                ]
            _run_descriptor_media_output(
                INCOMING_STORAGE, playback_stage, arguments, "mp4"
            )
            playback_source_path = f"incoming/{playback_stage}"
            staged_sources.append(playback_source_path)

        artifacts = [
            _staged_artifact_record(
                "preview", preview_name, preview_source_path
            ),
            _staged_artifact_record("thumb", thumb_name, thumb_source_path),
        ]
        if playback_name:
            artifacts.append(
                _staged_artifact_record(
                    "playback", playback_name, playback_source_path
                )
            )
        return preview_name, thumb_name, playback_name, artifacts
    except BaseException:
        for source_path in staged_sources:
            _unlink_staged_artifact(source_path)
        raise


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


class MediaIntentDeferred(RuntimeError):
    """A durable media intent was preserved for a later safe recovery pass."""

    preserve_staged_media = True


MEDIA_ARTIFACT_STORAGE = {
    "original": ORIGINAL_STORAGE,
    "preview": PREVIEW_STORAGE,
    "thumb": THUMB_STORAGE,
    "playback": PREVIEW_STORAGE,
}


def _data_relative_path(path):
    absolute = Path(os.path.abspath(os.fspath(path)))
    data_absolute = Path(os.path.abspath(os.fspath(DATA)))
    try:
        relative = absolute.relative_to(data_absolute).as_posix()
    except ValueError as error:
        raise StorageSafetyError("Media staging must remain on managed storage") from error
    safe_relative_parts(relative)
    return relative


def _open_staged_media(path, expected_size=None, expected_identity=None):
    relative = _data_relative_path(path)
    descriptor, metadata = DATA_STORAGE.open_regular_path(
        relative, expected_size=expected_size
    )
    if (
        expected_identity is not None
        and storage_identity(metadata) != tuple(expected_identity)
    ):
        os.close(descriptor)
        raise StorageSafetyError("Media staging identity changed")
    return relative, descriptor, metadata


def _verified_artifact(kind, name, *, expected_size=None, expected_digest=None):
    storage = MEDIA_ARTIFACT_STORAGE[kind]
    descriptor, metadata = storage.open_regular_path(
        name, expected_size=expected_size
    )
    try:
        digest = descriptor_sha256(descriptor)
        if expected_digest is not None and not hmac.compare_digest(
            digest, str(expected_digest)
        ):
            raise StorageSafetyError("Managed media artifact digest changed")
        return descriptor, metadata, digest
    except Exception:
        os.close(descriptor)
        raise


def _artifact_record(kind, name, *, expected_size=None, expected_digest=None):
    descriptor, metadata, digest = _verified_artifact(
        kind,
        name,
        expected_size=expected_size,
        expected_digest=expected_digest,
    )
    os.close(descriptor)
    return {
        "kind": kind,
        "name": str(name),
        "byte_size": int(metadata.st_size),
        "sha256": digest,
    }


def _staged_artifact_record(
    kind,
    name,
    source_path,
    *,
    expected_size=None,
    expected_digest=None,
):
    descriptor = -1
    try:
        descriptor, metadata = DATA_STORAGE.open_regular_path(
            source_path, expected_size=expected_size
        )
        digest = descriptor_sha256(descriptor)
        if expected_digest is not None and not hmac.compare_digest(
            digest, str(expected_digest)
        ):
            raise StorageSafetyError("Staged media artifact digest changed")
        return {
            "kind": kind,
            "name": str(name),
            "byte_size": int(metadata.st_size),
            "sha256": digest,
            "source_path": str(source_path),
            "source_dev": int(metadata.st_dev),
            "source_ino": int(metadata.st_ino),
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _artifact_manifest(
    stored_path,
    preview_name,
    thumb_name,
    playback_name,
    *,
    original_size,
    original_digest,
):
    artifacts = [
        _artifact_record(
            "original",
            stored_path,
            expected_size=original_size,
            expected_digest=original_digest,
        ),
        _artifact_record("preview", preview_name),
        _artifact_record("thumb", thumb_name),
    ]
    if playback_name:
        artifacts.append(_artifact_record("playback", playback_name))
    return artifacts


def _prepare_media_intent(
    record,
    source_path,
    artifacts,
    *,
    collection_id=None,
    expected_collection_owner_id=None,
    expected_collection_visibility=None,
    expected_collection_version=None,
    precommit_validator=None,
    publication_callback=None,
):
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target_collection = None
        if collection_id:
            target_collection = connection.execute(
                "SELECT * FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            if (
                not target_collection
                or target_collection["deleted_at"]
                or target_collection["owner_id"] != record["owner_id"]
            ):
                raise ValueError(
                    "New media can only be added directly to your own current collection."
                )
            if (
                expected_collection_owner_id is not None
                and target_collection["owner_id"] != expected_collection_owner_id
            ):
                raise ValueError("The target collection owner changed. Reload and try again.")
            if (
                expected_collection_visibility is not None
                and target_collection["visibility"] != expected_collection_visibility
            ):
                raise ValueError("The target collection privacy changed. Reload and try again.")
            if (
                expected_collection_version is not None
                and int(target_collection["version"])
                != int(expected_collection_version)
            ):
                raise ValueError("The target collection changed. Reload and try again.")
            if target_collection["visibility"] == "private":
                record["visibility"] = "private"
            record["collection_version"] = int(target_collection["version"])
            record["collection_visibility"] = target_collection["visibility"]
        else:
            record["collection_version"] = None
            record["collection_visibility"] = None
        if precommit_validator is not None:
            precommit_validator(connection)
        connection.execute(
            """INSERT INTO media_publish_intents
               (id,owner_id,owner_name,original_name,stored_path,preview_name,
                thumb_name,playback_name,content_type,byte_size,content_sha256,
                legacy_sha256,taken_at,capture_timestamp,uploaded_at,uploaded_by,
                visibility,source_device_id,ingestion_source,loop_playback,
                collection_id,collection_version,collection_visibility,source_path,
                artifacts_json,requires_live_validator,requires_publication_callback,
                state,created_at,committed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       'prepared',?,NULL)""",
            (
                record["id"],
                record["owner_id"],
                record["owner_name"],
                record["original_name"],
                record["stored_path"],
                record["preview_name"],
                record["thumb_name"],
                record["playback_name"],
                record["content_type"],
                record["byte_size"],
                record["content_sha256"],
                record["legacy_sha256"],
                record["taken_at"],
                record["capture_timestamp"],
                record["uploaded_at"],
                record["uploaded_by"],
                record["visibility"],
                record["source_device_id"],
                record["ingestion_source"],
                record["loop_playback"],
                collection_id,
                record["collection_version"],
                record["collection_visibility"],
                source_path,
                json.dumps(artifacts, separators=(",", ":"), sort_keys=True),
                # Older rollback images know only this live-validator bit. Mark
                # callback-bound device intents here as well so rollback startup
                # cannot publish them without the atomic device callback.
                1 if precommit_validator is not None or publication_callback is not None else 0,
                1 if publication_callback is not None else 0,
                record["uploaded_at"],
            ),
        )
        return dict(
            connection.execute(
                "SELECT * FROM media_publish_intents WHERE id=?", (record["id"],)
            ).fetchone()
        )


def _intent_artifacts(intent):
    try:
        artifacts = json.loads(intent["artifacts_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("The media recovery manifest is invalid") from error
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("The media recovery manifest is invalid")
    normalized = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("kind") not in MEDIA_ARTIFACT_STORAGE:
            raise ValueError("The media recovery manifest is invalid")
        name = str(artifact.get("name") or "")
        safe_relative_parts(name)
        try:
            byte_size = int(artifact["byte_size"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("The media recovery manifest is invalid") from error
        digest = str(artifact.get("sha256") or "").lower()
        if byte_size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("The media recovery manifest is invalid")
        normalized_artifact = {
            "kind": artifact["kind"],
            "name": name,
            "byte_size": byte_size,
            "sha256": digest,
        }
        source_path = artifact.get("source_path")
        if source_path is not None:
            source_path = str(source_path)
            safe_relative_parts(source_path)
            normalized_artifact["source_path"] = source_path
            source_dev_value = artifact.get("source_dev")
            source_ino_value = artifact.get("source_ino")
            if (source_dev_value is None) != (source_ino_value is None):
                raise ValueError("The media recovery manifest is invalid")
            if source_dev_value is not None:
                try:
                    source_dev = int(source_dev_value)
                    source_ino = int(source_ino_value)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        "The media recovery manifest is invalid"
                    ) from error
                if source_dev < 0 or source_ino <= 0:
                    raise ValueError("The media recovery manifest is invalid")
                normalized_artifact["source_dev"] = source_dev
                normalized_artifact["source_ino"] = source_ino
        normalized.append(normalized_artifact)
    if sum(item["kind"] == "original" for item in normalized) != 1:
        raise ValueError("The media recovery manifest is invalid")
    return normalized


def _intent_row(intent_id):
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM media_publish_intents WHERE id=?", (intent_id,)
        ).fetchone()
    return dict(row) if row else None


def _ensure_intent_artifacts(intent):
    verified = {}
    try:
        for artifact in _intent_artifacts(intent):
            key = (artifact["kind"], artifact["name"])
            try:
                descriptor, metadata, _digest = _verified_artifact(
                    artifact["kind"],
                    artifact["name"],
                    expected_size=artifact["byte_size"],
                    expected_digest=artifact["sha256"],
                )
            except FileNotFoundError:
                source_path = artifact.get("source_path")
                if source_path is None and artifact["kind"] == "original":
                    source_path = intent["source_path"]
                if source_path is None:
                    raise MediaIntentDeferred(
                        "A media artifact is unavailable; the intent was retained."
                    )
                source_descriptor = -1
                try:
                    source_descriptor, source_metadata = DATA_STORAGE.open_regular_path(
                        source_path, expected_size=artifact["byte_size"]
                    )
                    expected_source_identity = (
                        artifact.get("source_dev"), artifact.get("source_ino")
                    )
                    if None in expected_source_identity:
                        raise MediaIntentDeferred(
                            "The staged media lacks an inode binding; the intent was retained."
                        )
                    if storage_identity(source_metadata) != expected_source_identity:
                        raise StorageSafetyError(
                            "The staged media identity changed"
                        )
                    if not hmac.compare_digest(
                        descriptor_sha256(source_descriptor), artifact["sha256"]
                    ):
                        raise StorageSafetyError("The staged media digest changed")
                    try:
                        MEDIA_ARTIFACT_STORAGE[artifact["kind"]].link_descriptor(
                            source_descriptor, safe_component(artifact["name"])
                        )
                    except FileExistsError:
                        pass
                    descriptor, metadata, _digest = _verified_artifact(
                        artifact["kind"],
                        artifact["name"],
                        expected_size=artifact["byte_size"],
                        expected_digest=artifact["sha256"],
                    )
                    if storage_identity(metadata) != storage_identity(source_metadata):
                        os.close(descriptor)
                        descriptor = -1
                        raise StorageSafetyError(
                            "Published media does not match its staged inode"
                        )
                finally:
                    if source_descriptor >= 0:
                        os.close(source_descriptor)
            verified[key] = (descriptor, metadata)
        return verified
    except BaseException:
        for descriptor, _metadata in verified.values():
            os.close(descriptor)
        raise


def _photo_matches_intent(row, intent):
    if row is None:
        return False
    return all(
        row[column] == intent[intent_column]
        for column, intent_column in (
            ("id", "id"),
            ("original_name", "original_name"),
            ("stored_path", "stored_path"),
            ("preview_name", "preview_name"),
            ("thumb_name", "thumb_name"),
            ("playback_name", "playback_name"),
            ("content_type", "content_type"),
            ("byte_size", "byte_size"),
            ("sha256", "legacy_sha256"),
            ("content_sha256", "content_sha256"),
            ("taken_at", "taken_at"),
            ("capture_timestamp", "capture_timestamp"),
            ("uploaded_at", "uploaded_at"),
            ("uploaded_by", "uploaded_by"),
            ("loop_playback", "loop_playback"),
            ("owner_id", "owner_id"),
            ("owner_name", "owner_name"),
            ("visibility", "visibility"),
            ("source_device_id", "source_device_id"),
            ("ingestion_source", "ingestion_source"),
        )
    )


def _finalize_media_intent(
    intent_id,
    verified,
    precommit_validator=None,
    publication_callback=None,
):
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        intent = connection.execute(
            "SELECT * FROM media_publish_intents WHERE id=?", (intent_id,)
        ).fetchone()
        if not intent:
            raise sqlite3.IntegrityError("Media publish intent disappeared")
        existing = connection.execute(
            "SELECT * FROM photos WHERE id=?", (intent_id,)
        ).fetchone()
        for artifact in _intent_artifacts(intent):
            descriptor, _metadata = verified[(artifact["kind"], artifact["name"])]
            if not MEDIA_ARTIFACT_STORAGE[artifact["kind"]].matches_descriptor_path(
                artifact["name"], descriptor
            ):
                raise StorageSafetyError(
                    "A media artifact changed before database publication"
                )
        if intent["requires_publication_callback"] and publication_callback is None:
            raise MediaIntentDeferred(
                "This media intent requires its device publication callback."
            )
        if intent["state"] == "committed":
            if not _photo_matches_intent(existing, intent):
                raise sqlite3.IntegrityError(
                    "Committed media intent does not match its photo"
                )
            if publication_callback is not None:
                publication_callback(connection, existing)
            return dict(existing)
        if (
            intent["requires_live_validator"]
            and precommit_validator is None
            and publication_callback is None
        ):
            raise MediaIntentDeferred(
                "This media intent requires its live source authorization check."
            )
        if precommit_validator is not None:
            precommit_validator(connection)

        target_collection = None
        if intent["collection_id"]:
            target_collection = connection.execute(
                "SELECT * FROM collections WHERE id=?", (intent["collection_id"],)
            ).fetchone()
            if (
                not target_collection
                or target_collection["deleted_at"]
                or target_collection["owner_id"] != intent["owner_id"]
                or target_collection["visibility"] != intent["collection_visibility"]
                or int(target_collection["version"])
                != int(intent["collection_version"])
            ):
                raise MediaIntentDeferred(
                    "The target collection changed; the media intent was retained."
                )
            if target_collection["visibility"] == "private" and intent["visibility"] != "private":
                raise sqlite3.IntegrityError(
                    "Private collection media must remain private"
                )

        if existing is None:
            connection.execute(
                """INSERT INTO photos
                   (id,original_name,stored_path,preview_name,thumb_name,content_type,
                    byte_size,sha256,content_sha256,taken_at,capture_timestamp,uploaded_at,
                    uploaded_by,playback_name,loop_playback,owner_id,owner_name,visibility,
                    source_device_id,ingestion_source,primary_verification_state,
                    secondary_verification_state)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    intent["id"], intent["original_name"], intent["stored_path"],
                    intent["preview_name"], intent["thumb_name"], intent["content_type"],
                    intent["byte_size"], intent["legacy_sha256"], intent["content_sha256"],
                    intent["taken_at"], intent["capture_timestamp"], intent["uploaded_at"],
                    intent["uploaded_by"], intent["playback_name"], intent["loop_playback"],
                    intent["owner_id"], intent["owner_name"], intent["visibility"],
                    intent["source_device_id"], intent["ingestion_source"],
                    "primary_verified", "secondary_pending",
                ),
            )
            existing = connection.execute(
                "SELECT * FROM photos WHERE id=?", (intent_id,)
            ).fetchone()
        elif not _photo_matches_intent(existing, intent):
            raise sqlite3.IntegrityError("Media intent collided with another photo")

        if publication_callback is not None:
            publication_callback(connection, existing)

        if target_collection:
            connection.execute(
                """INSERT OR IGNORE INTO collection_photos
                   (collection_id,photo_id,added_at,added_by_id,version)
                   VALUES (?,?,?,?,1)""",
                (
                    target_collection["id"], intent_id, intent["uploaded_at"],
                    intent["owner_id"],
                ),
            )

        videos = None
        videos_created = False
        if str(intent["content_type"]).startswith("video/"):
            videos = connection.execute(
                """SELECT * FROM collections WHERE lower(name)='videos'
                   AND owner_id=? AND deleted_at IS NULL ORDER BY created_at LIMIT 1""",
                (intent["owner_id"],),
            ).fetchone()
            if videos is None:
                videos_id = uuid.uuid4().hex
                videos_created = True
                connection.execute(
                    """INSERT INTO collections
                       (id,name,created_at,created_by,owner_id,owner_name,visibility)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        videos_id, "Videos", intent["uploaded_at"], intent["owner_name"],
                        intent["owner_id"], intent["owner_name"], intent["visibility"],
                    ),
                )
                videos = connection.execute(
                    "SELECT * FROM collections WHERE id=?", (videos_id,)
                ).fetchone()
            connection.execute(
                """INSERT OR IGNORE INTO collection_photos
                   (collection_id,photo_id,added_at,added_by_id,version)
                   VALUES (?,?,?,?,1)""",
                (videos["id"], intent_id, intent["uploaded_at"], intent["owner_id"]),
            )

        actor = Actor(
            principal_id=intent["owner_id"],
            kind="device" if intent["source_device_id"] else "human",
        )
        already_audited = connection.execute(
            """SELECT 1 FROM mutation_audit
               WHERE domain='media' AND object_id=? AND action='create'""",
            (intent_id,),
        ).fetchone()
        if not already_audited:
            audit_mutation(
                connection,
                actor=actor,
                domain="media",
                object_id=intent_id,
                action="create",
                before=None,
                after=existing,
            )
        if videos_created:
            audit_mutation(
                connection,
                actor=actor,
                domain="media_collection",
                object_id=videos["id"],
                action="create",
                before=None,
                after=videos,
            )

        changed_collections = []
        if target_collection:
            changed_collections.append(target_collection)
        if videos is not None and (
            target_collection is None or target_collection["id"] != videos["id"]
        ):
            changed_collections.append(videos)
        for before_collection in changed_collections:
            updated = connection.execute(
                """UPDATE collections SET version=version+1
                   WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
                (
                    before_collection["id"], intent["owner_id"],
                    before_collection["version"],
                ),
            )
            if updated.rowcount != 1:
                raise sqlite3.IntegrityError("collection version changed")
            after_collection = connection.execute(
                "SELECT * FROM collections WHERE id=?", (before_collection["id"],)
            ).fetchone()
            audit_mutation(
                connection,
                actor=actor,
                domain="media_collection",
                object_id=before_collection["id"],
                action="membership_update",
                before=before_collection,
                after=after_collection,
            )

        committed_at = utcnow()
        updated = connection.execute(
            """UPDATE media_publish_intents SET state='committed',committed_at=?
               WHERE id=? AND state='prepared'""",
            (committed_at, intent_id),
        )
        if updated.rowcount != 1:
            raise sqlite3.IntegrityError("Media publish intent changed")
        return dict(existing)


def _cleanup_committed_media_source(intent):
    if not intent or intent["state"] != "committed":
        return True
    complete = True
    for artifact in _intent_artifacts(intent):
        source_path = artifact.get("source_path")
        if source_path is None and artifact["kind"] == "original":
            source_path = intent["source_path"]
        if not source_path:
            continue
        target_descriptor = source_descriptor = -1
        try:
            try:
                target_descriptor, target_metadata = MEDIA_ARTIFACT_STORAGE[
                    artifact["kind"]
                ].open_regular_path(
                    artifact["name"], expected_size=artifact["byte_size"]
                )
            except (FileNotFoundError, OSError, StorageSafetyError):
                complete = False
                continue
            expected_source_identity = (
                artifact.get("source_dev"), artifact.get("source_ino")
            )
            target_data_path = {
                "original": "originals",
                "preview": "previews",
                "thumb": "thumbs",
                "playback": "previews",
            }[artifact["kind"]]
            if not hmac.compare_digest(
                descriptor_sha256(target_descriptor), artifact["sha256"]
            ):
                complete = False
                continue
            if source_path == f"{target_data_path}/{artifact['name']}":
                continue
            try:
                source_descriptor, source_metadata = DATA_STORAGE.open_regular_path(
                    source_path, expected_size=artifact["byte_size"]
                )
            except FileNotFoundError:
                # The exact staged source was already cleaned on an earlier
                # successful pass.
                continue
            except (OSError, StorageSafetyError):
                complete = False
                continue
            if (
                None in expected_source_identity
                or storage_identity(source_metadata) != expected_source_identity
            ):
                # A path now naming some other inode is operator evidence, not
                # disposable staging, even when its bytes happen to match.
                complete = False
                continue
            if not hmac.compare_digest(
                descriptor_sha256(source_descriptor), artifact["sha256"]
            ):
                complete = False
                continue
            if not DATA_STORAGE.unlink_if_identity_path(
                source_path, storage_identity(source_metadata)
            ):
                complete = False
        finally:
            if target_descriptor >= 0:
                os.close(target_descriptor)
            if source_descriptor >= 0:
                os.close(source_descriptor)
    return complete


def _recover_media_intent(
    intent_id,
    precommit_validator=None,
    publication_callback=None,
):
    intent = _intent_row(intent_id)
    if not intent:
        raise sqlite3.IntegrityError("Media publish intent is missing")
    verified = {}
    try:
        verified = _ensure_intent_artifacts(intent)
        saved = _finalize_media_intent(
            intent_id,
            verified,
            precommit_validator=precommit_validator,
            publication_callback=publication_callback,
        )
    finally:
        for descriptor, _metadata in verified.values():
            os.close(descriptor)
    cleanup_complete = _cleanup_committed_media_source(_intent_row(intent_id))
    return saved, cleanup_complete


def _media_intent_committed(intent_id):
    intent = _intent_row(intent_id)
    if not intent or intent["state"] != "committed":
        return False
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM photos WHERE id=?", (intent_id,)
        ).fetchone()
    return _photo_matches_intent(row, intent)


def recover_media_publish_intents(limit=1000):
    """Idempotently finalize unambiguous media publications after termination."""
    bounded = min(max(int(limit), 1), 1000)
    with db() as connection:
        prepared = [dict(row) for row in connection.execute(
            """SELECT * FROM media_publish_intents WHERE state='prepared'
               ORDER BY created_at,id LIMIT ?""",
            (bounded,),
        ).fetchall()]
        committed = [dict(row) for row in connection.execute(
            """SELECT * FROM media_publish_intents WHERE state='committed'
               ORDER BY committed_at,id LIMIT ?""",
            (bounded,),
        ).fetchall()]
    recovered = unresolved = 0
    for intent in prepared:
        if intent["requires_live_validator"] or intent["requires_publication_callback"]:
            unresolved += 1
            continue
        try:
            _saved, cleanup_complete = _recover_media_intent(intent["id"])
            recovered += 1
            if not cleanup_complete:
                unresolved += 1
        except (OSError, ValueError, sqlite3.Error, MediaIntentDeferred):
            unresolved += 1
            LOGGER.exception("A durable media publish intent remains queued")
    for intent in committed:
        try:
            if not _cleanup_committed_media_source(intent):
                unresolved += 1
        except (OSError, ValueError, sqlite3.Error):
            unresolved += 1
            LOGGER.exception("Committed media staging cleanup remains queued")
    return {"recovered_intents": recovered, "unresolved_intents": unresolved}


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
    expected_collection_owner_id=None,
    expected_collection_visibility=None,
    expected_collection_version=None,
    loop_playback=False,
    precommit_validator=None,
    publication_callback=None,
    media_id=None,
    expected_staged_identity=None,
):
    """Create one canonical logical media record, reusing physical bytes when safe.

    Browser uploads and device backups both call this function. Physical-object
    deduplication is intentionally independent from logical ownership.
    """
    owner_user_id = str(owner_user_id or "").strip().casefold()
    if not owner_user_id:
        raise ValueError("A verified owner is required for new media.")
    owner_name = str(owner_name or "Owner").strip() or "Owner"
    if visibility not in {"shared", "private"}:
        raise ValueError("Choose Shared or Only me.")
    original_name = secure_filename(original_filename or "media") or "media"
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED:
        raise ValueError("Unsupported photo or video format.")

    source_descriptor = -1
    source_metadata = None
    source_relative = None
    staged_artifact_sources = []
    intent_prepared = False
    physical_reused = False
    photo_id = str(media_id or uuid.uuid4().hex).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", photo_id):
        raise ValueError("The media publication identity is invalid.")
    existing_intent = _intent_row(photo_id) if publication_callback is not None else None
    if existing_intent is not None:
        expected_name = secure_filename(original_filename or "media") or "media"
        expected_digest = str(authoritative_sha256 or "").lower()
        try:
            expected_size = int(authoritative_size or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("Verified physical media is required.") from error
        if (
            existing_intent["owner_id"] != owner_user_id
            or existing_intent["original_name"] != expected_name
            or existing_intent["source_device_id"] != source_device_id
            or existing_intent["ingestion_source"] != ingestion_source
            or existing_intent["content_sha256"] != expected_digest
            or int(existing_intent["byte_size"]) != expected_size
            or not existing_intent["requires_publication_callback"]
        ):
            raise sqlite3.IntegrityError(
                "The device media publication identity conflicts with another intent."
            )
        saved, cleanup_complete = _recover_media_intent(
            photo_id,
            precommit_validator=precommit_validator,
            publication_callback=publication_callback,
        )
        if not cleanup_complete:
            LOGGER.warning(
                "Committed device media staging remains retained for operator review"
            )
        return {
            "id": saved["id"],
            "name": saved["original_name"],
            "content_sha256": saved["content_sha256"],
            "physical_reused": True,
            "recovered_intent": True,
        }
    now = datetime.now(timezone.utc)
    try:
        if staged_path is not None:
            source_relative, source_descriptor, source_metadata = _open_staged_media(
                staged_path, expected_identity=expected_staged_identity
            )
            size = int(source_metadata.st_size)
            if size <= 0 or size > MAX_MEDIA_FILE_BYTES:
                raise ValueError("The media file is outside the supported size limit.")
            checksum = descriptor_sha256(source_descriptor)
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
        if physical:
            physical = dict(physical)
            try:
                artifacts = _artifact_manifest(
                    physical["stored_path"],
                    physical["preview_name"],
                    physical["thumb_name"],
                    physical["playback_name"],
                    original_size=size,
                    original_digest=checksum,
                )
            except (OSError, ValueError, StorageSafetyError):
                if staged_path is None:
                    raise ValueError("The verified physical object is unavailable.")
                physical = None

        if physical:
            physical_reused = True
            stored_path = physical["stored_path"]
            preview_name = physical["preview_name"]
            thumb_name = physical["thumb_name"]
            playback_name = physical["playback_name"]
            content_type = physical["content_type"]
            taken = parse_capture_timestamp(
                capture_timestamp, datetime.fromisoformat(physical["taken_at"])
            )
            if source_relative is None:
                source_relative = f"originals/{stored_path}"
            elif source_metadata is not None:
                # A freshly uploaded duplicate is still exact cleanup
                # evidence. Bind its inode so later cleanup never removes a
                # same-name replacement.
                artifacts[0].update(
                    source_path=source_relative,
                    source_dev=int(source_metadata.st_dev),
                    source_ino=int(source_metadata.st_ino),
                )
        else:
            if source_descriptor < 0:
                raise ValueError("The verified physical object could not be found.")
            required = size * (2 if extension in VIDEO_ALLOWED else 1) + 512 * 1024 * 1024
            if shutil.disk_usage(DATA).free < required:
                raise ValueError("David-Pi does not have enough free space.")

            stored_path = safe_component(f"{photo_id}{extension}")
            preview_name = safe_component(f"{photo_id}.jpg")
            thumb_name = safe_component(f"{photo_id}.jpg")
            playback_name = None
            artifacts = [{
                "kind": "original",
                "name": stored_path,
                "byte_size": size,
                "sha256": checksum,
                "source_path": source_relative,
                "source_dev": int(source_metadata.st_dev),
                "source_ino": int(source_metadata.st_ino),
            }]
            if extension in VIDEO_ALLOWED:
                taken = parse_capture_timestamp(capture_timestamp, now)
                (
                    preview_name,
                    thumb_name,
                    playback_name,
                    video_artifacts,
                ) = prepare_video(
                    f"/proc/self/fd/{source_descriptor}", photo_id, extension
                )
                artifacts.extend(video_artifacts)
                staged_artifact_sources.extend(
                    artifact["source_path"] for artifact in video_artifacts
                )
                content_type = (
                    "video/mp4"
                    if extension in (".mp4", ".m4v")
                    else "video/quicktime"
                )
            else:
                with os.fdopen(os.dup(source_descriptor), "rb") as source:
                    with Image.open(source) as image:
                        if image.width * image.height > MAX_IMAGE_PIXELS:
                            raise ValueError(
                                "This image has too many pixels to process safely."
                            )
                        image.load()
                        taken = parse_capture_timestamp(
                            capture_timestamp, exif_time(image, now)
                        )
                        preview_stage = stage_jpeg(
                            image.copy(), (2200, 2200), 88
                        )
                        staged_artifact_sources.append(
                            f"incoming/{preview_stage}"
                        )
                        thumb_stage = stage_jpeg(image.copy(), (360, 360), 78)
                        staged_artifact_sources.append(f"incoming/{thumb_stage}")
                for kind, name, stage_name in (
                    ("preview", preview_name, preview_stage),
                    ("thumb", thumb_name, thumb_stage),
                ):
                    source_path = f"incoming/{stage_name}"
                    artifact = _staged_artifact_record(kind, name, source_path)
                    artifacts.append(artifact)
                content_type = mime_type or "application/octet-stream"

        timestamp = now.isoformat()
        record = {
            "id": photo_id,
            "owner_id": owner_user_id,
            "owner_name": owner_name,
            "original_name": original_name,
            "stored_path": stored_path,
            "preview_name": preview_name,
            "thumb_name": thumb_name,
            "playback_name": playback_name,
            "content_type": content_type,
            "byte_size": size,
            "content_sha256": checksum,
            # The canonical digest lives in content_sha256.  Keep this legacy
            # unique field collision-free even when owners share physical bytes.
            "legacy_sha256": f"{checksum}:{photo_id}",
            "taken_at": taken.isoformat(),
            "capture_timestamp": taken.isoformat(),
            "uploaded_at": timestamp,
            "uploaded_by": owner_name,
            "visibility": visibility,
            "source_device_id": source_device_id,
            "ingestion_source": ingestion_source,
            "loop_playback": 1 if loop_playback else 0,
        }
        _prepare_media_intent(
            record,
            source_relative,
            artifacts,
            collection_id=collection_id,
            expected_collection_owner_id=expected_collection_owner_id,
            expected_collection_visibility=expected_collection_visibility,
            expected_collection_version=expected_collection_version,
            precommit_validator=precommit_validator,
            publication_callback=publication_callback,
        )
        intent_prepared = True
        _saved, cleanup_complete = _recover_media_intent(
            photo_id,
            precommit_validator=precommit_validator,
            publication_callback=publication_callback,
        )
        if not cleanup_complete:
            LOGGER.warning(
                "Committed media staging remains retained for operator review"
            )
    except Exception as error:
        if intent_prepared:
            try:
                if _media_intent_committed(photo_id):
                    error = None
            except Exception:
                LOGGER.exception("Could not confirm a media intent transaction outcome")
            if error is not None:
                raise MediaIntentDeferred(
                    "The media item is retained for safe recovery."
                ) from error
        else:
            for source_path in staged_artifact_sources:
                _unlink_staged_artifact(source_path)
            raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)

    try:
        ensure_viewer_preview(preview_name)
    except (OSError, ValueError, UnidentifiedImageError):
        # The full preview is a safe fallback and the resumable warmer can retry.
        pass
    return {
        "id": photo_id,
        "name": original_name,
        "content_sha256": checksum,
        "physical_reused": physical_reused,
    }


def rollback_canonical_ingest(photo_id):
    """Hide a failed outer publication without destroying committed content.

    Canonical publication and its audit are already committed when an outer
    device/slideshow transaction calls this compensator.  A destructive unlink
    here can turn a recoverable metadata failure into saved-content loss.
    """
    now_value = datetime.now(timezone.utc)
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM photos WHERE id=? AND deleted_at IS NULL", (photo_id,)
        ).fetchone()
        if not before:
            return False
        changed = connection.execute(
            """UPDATE photos SET deleted_at=?,deleted_by_id=?,purge_after=?,version=version+1
               WHERE id=? AND deleted_at IS NULL AND version=?""",
            (
                now_value.isoformat(),
                before["owner_id"],
                (now_value + timedelta(days=30)).isoformat(),
                photo_id,
                before["version"],
            ),
        )
        if changed.rowcount != 1:
            raise sqlite3.IntegrityError("media changed during compensation")
        after = connection.execute(
            "SELECT * FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
        audit_mutation(
            connection,
            actor=Actor(principal_id=before["owner_id"], kind="system"),
            domain="media",
            object_id=photo_id,
            action="ingest_compensation_trash",
            before=before,
            after=after,
        )
    return True


if not IS_SLIDESHOW_WORKER and substrate_enabled("media"):
    _media_publish_recovery = recover_media_publish_intents()
    if _media_publish_recovery["unresolved_intents"]:
        LOGGER.warning(
            "%s durable media publish intent(s) require a later recovery pass",
            _media_publish_recovery["unresolved_intents"],
        )

if not IS_SLIDESHOW_WORKER:  # Companion pairing is core; backup uploads remain capability-gated.
    from modules.device_backup import init_device_backup
    init_device_backup(app, db, canonical_ingest_media, rollback_canonical_ingest, DATA)

if not IS_SLIDESHOW_WORKER and module_enabled("chat"):
    from modules.chat import init_chat
    init_chat(app, canonical_ingest_media, rollback_canonical_ingest)


def _project_mytube_link(*args, **kwargs):
    return None


def _reconcile_mytube_links():
    return None


if not IS_SLIDESHOW_WORKER and module_enabled("mytube"):
    from modules.mytube import (
        MediaReference, init_mytube, reconcile_media_references,
        register_media_reference, remove_media_reference, media_duration,
    )

    def _mytube_media_row(media_id):
        with db() as connection:
            return connection.execute(
                """SELECT p.*,l.video_id FROM photos p
                   JOIN mytube_media_links l ON l.media_id=p.id
                   WHERE p.id=? AND p.deleted_at IS NULL AND p.content_type LIKE 'video/%'""",
                (media_id,),
            ).fetchone()

    def _mytube_media_authorizer(media_id, actor):
        return row_is_visible(_mytube_media_row(media_id), actor)

    def _mytube_media_resolver(media_id):
        row = _mytube_media_row(media_id)
        if not row:
            raise FileNotFoundError("Media source unavailable")
        return managed_path(ORIGINALS, row["stored_path"])

    def _project_mytube_link(media_id, *, probe_duration=False):
        row = _mytube_media_row(media_id)
        if not row:
            return None
        return register_media_reference(MediaReference(
            video_id=row["video_id"], media_id=row["id"], title=row["original_name"],
            owner_id=row["owner_id"], owner_name=row["owner_name"] or "Owner",
            visibility=row["visibility"], content_type=row["content_type"],
            byte_size=int(row["byte_size"]), sha256=row["content_sha256"] or row["sha256"],
            duration_seconds=media_duration(managed_path(ORIGINALS, row["stored_path"])) if probe_duration else 0.0,
        ))

    def _reconcile_mytube_links():
        with db() as connection:
            rows = connection.execute(
                """SELECT p.*,l.video_id FROM photos p
                   JOIN mytube_media_links l ON l.media_id=p.id
                   WHERE p.deleted_at IS NULL AND p.content_type LIKE 'video/%'
                   ORDER BY l.media_id"""
            ).fetchall()
        reconcile_media_references(MediaReference(
            video_id=row["video_id"], media_id=row["id"], title=row["original_name"],
            owner_id=row["owner_id"], owner_name=row["owner_name"] or "Owner",
            visibility=row["visibility"], content_type=row["content_type"],
            byte_size=int(row["byte_size"]), sha256=row["content_sha256"] or row["sha256"],
        ) for row in rows)

    app.config["MYTUBE_MEDIA_AUTHORIZER"] = _mytube_media_authorizer
    app.config["MYTUBE_MEDIA_DATABASE"] = str(DB_PATH)
    app.config["MYTUBE_MEDIA_RESOLVER"] = _mytube_media_resolver
    app.config["MYTUBE_MEDIA_RECONCILER"] = _reconcile_mytube_links
    init_mytube(app)
    _reconcile_mytube_links()


@app.get("/api/mytube/media-links/<media_id>")
def mytube_media_link_status(media_id):
    _reconcile_mytube_links()
    actor = actor_for_identity(current_device())
    with db() as connection:
        row = connection.execute(
            """SELECT p.*,l.video_id FROM photos p LEFT JOIN mytube_media_links l ON l.media_id=p.id
               WHERE p.id=? AND p.deleted_at IS NULL AND p.content_type LIKE 'video/%'""",
            (media_id,),
        ).fetchone()
    if not row_is_visible(row, actor):
        return jsonify(error="Media item not found."), 404
    return jsonify(linked=bool(row["video_id"]), video_id=row["video_id"])


@app.post("/api/mytube/media-links")
def create_mytube_media_link():
    data = request.get_json(silent=True) or {}
    media_id = str(data.get("media_id") or "").strip()
    actor = actor_for_identity(current_device())
    if not re.fullmatch(r"[0-9a-f]{32}", media_id):
        return jsonify(error="Choose a current Media video."), 422
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM photos WHERE id=? AND deleted_at IS NULL AND content_type LIKE 'video/%'",
            (media_id,),
        ).fetchone()
        if not row or row["owner_id"] != actor.principal_id:
            return jsonify(error="Only the video owner can add it to MyTube."), 403
        linked = connection.execute(
            "SELECT video_id FROM mytube_media_links WHERE media_id=?", (media_id,),
        ).fetchone()
        video_id = linked["video_id"] if linked else uuid.uuid4().hex
        if not linked:
            connection.execute(
                "INSERT INTO mytube_media_links(media_id,video_id,linked_by_id,linked_at) VALUES(?,?,?,?)",
                (media_id, video_id, actor.principal_id, utcnow()),
            )
            audit_mutation(
                connection, actor=actor, domain="mytube_media_link",
                object_id=media_id, action="create", before=None, after=row,
            )
    try:
        _project_mytube_link(media_id, probe_duration=not linked)
    except (OSError, ValueError, sqlite3.Error):
        LOGGER.exception("MyTube projection failed after authoritative Media link commit")
        return jsonify(error="The protected link was saved, but MyTube is still preparing it. Retry safely."), 503
    return jsonify(ok=True, linked=True, video_id=video_id), 200 if linked else 201


@app.delete("/api/mytube/media-links/<media_id>")
def delete_mytube_media_link(media_id):
    actor = actor_for_identity(current_device())
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT p.owner_id,l.video_id FROM photos p JOIN mytube_media_links l ON l.media_id=p.id
               WHERE p.id=?""", (media_id,),
        ).fetchone()
        if not row or row["owner_id"] != actor.principal_id:
            return jsonify(error="MyTube link not found."), 404
        connection.execute("DELETE FROM mytube_media_links WHERE media_id=?", (media_id,))
        audit_mutation(
            connection, actor=actor, domain="mytube_media_link",
            object_id=media_id, action="delete", before=row, after=None,
        )
    try:
        remove_media_reference(media_id, row["video_id"])
    except (OSError, ValueError, sqlite3.Error):
        LOGGER.exception("MyTube projection cleanup failed after authoritative unlink")
    return jsonify(ok=True, linked=False)


@app.get("/")
def home():
    identity_value = current_device()
    return render_template(
        "home.html",
        person=identity_value["name"],
        home_recent_scope=home_recent_storage_scope(identity_value),
    )


@app.get("/photos")
def photos_page():
    visible, parameters = visibility_sql("c")
    with db() as connection:
        organizer_collections = connection.execute(
            f"""SELECT c.id, c.name, c.version FROM collections c
                WHERE c.deleted_at IS NULL AND c.owner_id IS NOT NULL AND {visible}
                ORDER BY lower(c.name)""",
            parameters,
        ).fetchall()
    return render_template(
        "photos.html", person=identity(),
        organizer_collections=[dict(row) for row in organizer_collections],
    )


@app.get("/photos/collections")
def photo_collections_page():
    return render_template("photo_collections.html", person=identity())


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
    draw.rounded_rectangle(box((140, 152, 372, 358)), radius=round(24 * scale), outline="#f4a77c", width=max(round(20 * scale), 2))
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


def readiness_reasons():
    """Return bounded readiness failures without mutating application state."""
    reasons = []
    if read_text(DATA_SENTINEL) != DATA_SENTINEL_VALUE:
        reasons.append("storage_identity")
    try:
        if substrate_enabled("media"):
            with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2) as connection:
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except (OSError, sqlite3.Error):
        reasons.append("database_unavailable")
    try:
        status = server_status_snapshot()
        generated = datetime.fromisoformat(status["generated_at"].replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - generated).total_seconds()
        if age_seconds < -60 or age_seconds > READINESS_MAX_STATUS_AGE_SECONDS:
            reasons.append("status_stale")
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        reasons.append("status_unavailable")
    # stat(2) succeeds for an unreadable bind-mounted secret. Exercise the
    # exact raw/base64 loader used by Chat so readiness cannot report healthy
    # while the unprivileged portal process is unable to decrypt messages.
    if module_enabled("chat"):
        from modules.chat import chat_key_available
        if not chat_key_available():
            reasons.append("secret_unavailable")
    if not app.testing and get_installation() is None:
        reasons.append("setup_incomplete")
    return reasons


@app.get("/ready")
def ready():
    reasons = readiness_reasons()
    return jsonify(ok=not reasons, reasons=reasons), 503 if reasons else 200


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
    if get_installation() is not None:
        try:
            from installer.client import request as host_request
            return jsonify(host_request("metrics_history", {"metric": metric, "range": selected_range}, identity=current_device()["owner_id"]))
        except (OSError, RuntimeError, ValueError):
            return jsonify(error="System history is temporarily unavailable. Recent measurements may still be collecting."), 503
    column = metrics[metric]
    try:
        with history_metrics_db() as connection:
            rows = connection.execute(
                f"SELECT timestamp, {column} FROM system_metrics "
                f"WHERE timestamp >= ? AND {column} IS NOT NULL ORDER BY timestamp",
                (int(time.time()) - ranges[selected_range] * 3600,),
            ).fetchall()
    except sqlite3.Error:
        return jsonify(error="System history is temporarily unavailable."), 503
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
    if not module_enabled("media") and re.search(r"\b(photos?|pictures|videos?|media|gallery|collections?|albums?|trash|recently deleted)\b", normalized):
        return "Media is disabled. An administrator can enable it in Settings; saved media stays preserved.", "module_disabled"
    if not module_enabled("pihole") and re.search(r"\b(pi[ -]?hole|blocked|blocking|ads|queries|dns)\b", normalized):
        return "Pi-hole is not enabled. An administrator can connect a supported local installation using the Pi-hole setup guide.", "module_disabled"
    if re.search(r"\b(recently deleted|deleted photos|deleted media|trash)\b", normalized):
        facts = photo_library_summary()
        return f"Recently Deleted currently has {facts['deleted']} item{'s' if facts['deleted'] != 1 else ''}.", "deleted"
    if re.search(r"\b(collection|collections|album|albums)\b", normalized):
        facts = photo_library_summary()
        return f"You currently have {facts['collections']} collection{'s' if facts['collections'] != 1 else ''}.", "collections"
    if re.search(r"\b(photo|photos|pictures|video|videos|media|gallery)\b", normalized):
        facts = photo_library_summary()
        return f"{display_name()} currently has {facts['photos']} media item{'s' if facts['photos'] != 1 else ''} in the gallery.", "photos"
    if re.search(r"\b(storage|space|disk|room|capacity)\b", normalized):
        snapshot = system_snapshot()
        return (
            f"{display_name()} has {snapshot['disk_free_gb']} GB free out of {snapshot['disk_total_gb']} GB. "
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
        return f"{display_name()} has been running for {format_duration(snapshot['uptime'])}.", "uptime"
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


if not IS_SLIDESHOW_WORKER and module_enabled("assistant"):
    init_assistant(app, answer_assistant)
    _assistant_knowledge = Path(__file__).parent / "knowledge" / "assistant"
    seed_knowledge(
        [
            (path.stem, path.stem.replace("-", " ").title(), path.read_text(encoding="utf-8"))
            for path in sorted(_assistant_knowledge.glob("*.md"))
        ]
    )


MAX_MEDIA_JSON_BODY_BYTES = 16 * 1024
MAX_MEDIA_CAPTION_CHARACTERS = 1_000
_UNSAFE_DIRECTIONAL_CONTROLS = frozenset(
    chr(value)
    for start, end in ((0x202A, 0x202E), (0x2066, 0x2069))
    for value in range(start, end + 1)
)


def _verified_media_actor():
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return None
    return actor


def _clean_media_text(value, *, maximum, label, allow_empty=True, compatibility=False):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    normalized = unicodedata.normalize("NFKC" if compatibility else "NFC", value).strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{label} is required.")
    if len(normalized) > maximum:
        raise ValueError(f"{label} is too long.")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or character in _UNSAFE_DIRECTIONAL_CONTROLS
        for character in normalized
    ):
        raise ValueError(f"{label} contains unsupported control characters.")
    return normalized


def _gallery_period_value(value):
    period = str(value or "").strip()
    if not period:
        return None
    if not re.fullmatch(r"\d{4}(?:-(?:0[1-9]|1[0-2]))?", period):
        raise ValueError("Choose a valid year or month.")
    return period


def _gallery_scope(source, *, collection_id=""):
    raw_scope = source.get("scope") if "scope" in source else None
    scope_present = raw_scope is not None and raw_scope != ""
    if scope_present:
        scope = str(raw_scope).strip().lower()
        if scope not in {"shared", "mine", "visible"}:
            raise ValueError("Choose Shared, Added by me, or All visible media.")
        return scope
    # Compatibility contract: the historic view=all/default endpoint remains
    # the shared library and view=mine remains owner-only. A direct collection
    # historically used canonical row visibility, so preserve that behavior.
    view = str(source.get("view", "all") or "all").strip().lower()
    if view not in {"all", "mine"}:
        raise ValueError("Choose a supported media view.")
    if collection_id:
        return "visible"
    return "mine" if view == "mine" else "shared"


def _gallery_favorite(value):
    if value is None or value == "":
        return None
    if value is True or (isinstance(value, str) and value.lower() == "true"):
        return True
    if value is False or (isinstance(value, str) and value.lower() == "false"):
        return False
    raise ValueError("Choose a valid favorites filter.")


def _gallery_integer(value, *, default, minimum, maximum, label):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is invalid.") from error
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} is invalid.")
    return parsed


def _gallery_options(source):
    collection_id = _clean_media_text(
        str(source.get("collection", "") or ""),
        maximum=128,
        label="Collection",
    )
    kind = str(source.get("kind", "all") or "all").strip().lower()
    if kind not in {"all", "photo", "video"}:
        raise ValueError("Choose Photos, Videos, or All media.")
    return {
        "scope": _gallery_scope(source, collection_id=collection_id),
        "kind": kind,
        "favorite": _gallery_favorite(source.get("favorite")),
        "period": _gallery_period_value(source.get("period")),
        "collection": collection_id,
        "limit": _gallery_integer(
            source.get("limit"), default=30, minimum=1,
            maximum=200, label="Page size",
        ),
        "offset": _gallery_integer(
            source.get("offset"), default=0, minimum=0, maximum=1_000_000,
            label="Gallery position",
        ),
        "cursor": str(source.get("cursor", "") or "").strip(),
    }


def _media_scope_clause(actor, scope):
    if scope == "mine":
        return "p.owner_id = ?", [actor.principal_id]
    if scope == "visible":
        return "(p.visibility = 'shared' OR p.owner_id = ?)", [actor.principal_id]
    return "p.visibility = 'shared'", []


def _media_filter_parts(actor, options):
    scope_sql, scope_parameters = _media_scope_clause(actor, options["scope"])
    clauses = ["p.deleted_at IS NULL", scope_sql]
    parameters = list(scope_parameters)
    if options["collection"]:
        clauses.insert(0, "cp.collection_id = ?")
        parameters.insert(0, options["collection"])
    if options["kind"] == "video":
        clauses.append("p.content_type LIKE 'video/%'")
    elif options["kind"] == "photo":
        clauses.append("p.content_type NOT LIKE 'video/%'")
    if options["favorite"] is True:
        clauses.append("COALESCE(mps.favorite, 0) = 1")
    elif options["favorite"] is False:
        clauses.append("COALESCE(mps.favorite, 0) = 0")
    period_sql, period_parameters = gallery_period_sql(options["period"])
    if period_sql:
        clauses.append(period_sql.removeprefix(" AND "))
        parameters.extend(period_parameters)
    return clauses, parameters


def _gallery_binding(actor, options, *, endpoint):
    payload = {
        "actor": actor.principal_id,
        "collection": options["collection"],
        "endpoint": endpoint,
        "favorite": options["favorite"],
        "kind": options["kind"],
        "period": options["period"],
        "scope": options["scope"],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _decode_bound_gallery_cursor(raw_cursor, binding):
    if not raw_cursor:
        return None
    cursor = decode_gallery_cursor(raw_cursor, 4)
    if not cursor or not hmac.compare_digest(cursor[3], binding):
        return None
    return cursor[:3]


def _private_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _bounded_media_json(invalid_message):
    """Read a small JSON object without allowing metadata requests to grow unbounded."""
    if (
        request.content_length is not None
        and request.content_length > MAX_MEDIA_JSON_BODY_BYTES
    ):
        return None, _private_json({"error": "The media request is too large."}, 413)
    raw_body = request.get_data(cache=True)
    if len(raw_body) > MAX_MEDIA_JSON_BODY_BYTES:
        return None, _private_json({"error": "The media request is too large."}, 413)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, _private_json({"error": invalid_message}, 400)
    return data, None


def _media_listing(
    actor,
    options,
    *,
    endpoint,
):
    binding = _gallery_binding(actor, options, endpoint=endpoint)
    cursor = _decode_bound_gallery_cursor(options["cursor"], binding)
    if options["cursor"] and not cursor:
        return _private_json(
            {"error": "The gallery position is invalid. Refresh and try again."}, 400
        )
    joins = []
    if options["collection"]:
        joins.append("JOIN collection_photos cp ON cp.photo_id = p.id")
    count_joins = list(joins)
    if options["favorite"] is not None:
        count_joins.append(
            "LEFT JOIN media_personal_state mps "
            "ON mps.photo_id = p.id AND mps.principal_id = ?"
        )
    joins.extend((
        "LEFT JOIN media_captions mc ON mc.photo_id = p.id",
        "LEFT JOIN media_personal_state mps "
        "ON mps.photo_id = p.id AND mps.principal_id = ?",
    ))
    clauses, filter_parameters = _media_filter_parts(actor, options)
    count_where = " AND ".join(clauses)
    cursor_parameters = []
    if cursor:
        clauses.append(
            "(p.capture_timestamp,p.uploaded_at,p.id) < (?,?,?)"
        )
        cursor_parameters = [
            cursor[0], cursor[1], cursor[2],
        ]
    gallery_columns = (
        "p.id,p.original_name,p.content_type,p.taken_at,p.capture_timestamp,"
        "p.uploaded_at,p.deleted_at,p.playback_name,p.loop_playback,p.owner_id,"
        "p.owner_name,p.uploaded_by,p.visibility,p.version,mc.caption,"
        "COALESCE(mc.version,0) AS caption_version,"
        "COALESCE(mps.favorite,0) AS favorite,"
        "COALESCE(mps.version,0) AS state_version"
    )
    joined = " ".join(joins)
    where = " AND ".join(clauses)
    with db() as connection:
        if options["collection"]:
            collection = connection.execute(
                """SELECT 1 FROM collections c WHERE c.id=? AND c.deleted_at IS NULL
                   AND (c.visibility='shared' OR c.owner_id=?)""",
                (options["collection"], actor.principal_id),
            ).fetchone()
            if not collection:
                return _private_json({"error": "Collection not found."}, 404)
        parameters = [actor.principal_id, *filter_parameters]
        rows = connection.execute(
            f"SELECT {gallery_columns} FROM photos p {joined} WHERE {where} "
            "ORDER BY p.capture_timestamp DESC,p.uploaded_at DESC,p.id DESC "
            "LIMIT ? OFFSET ?",
            (
                *parameters, *cursor_parameters, options["limit"] + 1,
                0 if cursor else options["offset"],
            ),
        ).fetchall()
        count_parameters = (
            [actor.principal_id, *filter_parameters]
            if options["favorite"] is not None else filter_parameters
        )
        total = None if cursor else connection.execute(
            f"SELECT COUNT(*) FROM photos p {' '.join(count_joins)} WHERE {count_where}", count_parameters,
        ).fetchone()[0]
    has_more = len(rows) > options["limit"]
    rows = rows[: options["limit"]]
    next_cursor = (
        encode_gallery_cursor(
            rows[-1]["capture_timestamp"], rows[-1]["uploaded_at"],
            rows[-1]["id"], binding,
        )
        if has_more and rows else None
    )
    payload = {
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "period": options["period"],
        "scope": options["scope"],
        "kind": options["kind"],
        "favorite": options["favorite"],
        "photos": [photo_json(row, actor_id=actor.principal_id) for row in rows],
    }
    return _private_json(payload)


@app.get("/api/photos")
def list_photos():
    actor = _verified_media_actor()
    if actor is None:
        return _private_json(
            {"error": "Open David-Pi through an approved private Tailscale account."},
            403,
        )
    try:
        options = _gallery_options(request.args)
    except ValueError as error:
        return _private_json({"error": str(error)}, 400)
    return _media_listing(actor, options, endpoint="list")


@app.get("/api/photos/timeline")
def photo_timeline():
    actor = _verified_media_actor()
    if actor is None:
        return _private_json(
            {"error": "Open David-Pi through an approved private Tailscale account."},
            403,
        )
    try:
        options = _gallery_options(request.args)
    except ValueError as error:
        return _private_json({"error": str(error)}, 400)
    collection_id = options["collection"]
    joins = []
    if collection_id:
        joins.append("JOIN collection_photos cp ON cp.photo_id=p.id")
    if options["favorite"] is not None:
        joins.append(
            "LEFT JOIN media_personal_state mps "
            "ON mps.photo_id=p.id AND mps.principal_id=?"
        )
    clauses, parameters = _media_filter_parts(actor, options)
    joined = " ".join(joins)
    where = " AND ".join(clauses)
    with db() as connection:
        if collection_id:
            collection = connection.execute(
                """SELECT 1 FROM collections c WHERE c.id=? AND c.deleted_at IS NULL
                   AND (c.visibility='shared' OR c.owner_id=?)""",
                (collection_id, actor.principal_id),
            ).fetchone()
            if not collection:
                return _private_json({"error": "Collection not found."}, 404)
        rows = connection.execute(
            "SELECT substr(p.capture_timestamp,1,7) AS month,COUNT(*) AS item_count "
            f"FROM photos p {joined} WHERE {where} "
            "GROUP BY substr(p.capture_timestamp,1,7) ORDER BY month DESC",
            ([actor.principal_id, *parameters] if options["favorite"] is not None else parameters),
        ).fetchall()
    months = [dict(row) for row in rows if re.fullmatch(r"\d{4}-\d{2}", row["month"] or "")]
    return _private_json(
        {
            "months": months,
            "total": sum(row["item_count"] for row in months),
            "scope": options["scope"],
            "kind": options["kind"],
            "favorite": options["favorite"],
        }
    )


@app.get("/api/photos/deleted")
def deleted_photos():
    limit = min(max(request.args.get("limit", 30, type=int), 1), 200)
    offset = max(request.args.get("offset", 0, type=int), 0)
    raw_cursor = request.args.get("cursor")
    cursor = decode_gallery_cursor(raw_cursor, 2)
    if raw_cursor and not cursor:
        return _private_json(
            {"error": "The gallery position is invalid. Refresh and try again."}, 400,
        )
    actor = _verified_media_actor()
    if actor is None:
        return _private_json(
            {"error": "Open David-Pi through an approved private Tailscale account."},
            403,
        )
    actor_id = actor.principal_id
    try:
        scope = _gallery_scope(request.args)
    except ValueError as error:
        return _private_json({"error": str(error)}, 400)
    visible, parameters = _media_scope_clause(actor, scope)
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
                      p.owner_name,p.uploaded_by,p.visibility,p.version,mc.caption,
                      COALESCE(mc.version,0) AS caption_version,
                      COALESCE(mps.favorite,0) AS favorite,
                      COALESCE(mps.version,0) AS state_version
               FROM photos p
               LEFT JOIN media_captions mc ON mc.photo_id=p.id
               LEFT JOIN media_personal_state mps
                 ON mps.photo_id=p.id AND mps.principal_id=? """
            f"WHERE p.deleted_at IS NOT NULL AND {visible}{cursor_sql} "
            "ORDER BY p.deleted_at DESC,p.id DESC LIMIT ? OFFSET ?",
            (
                actor_id, *parameters, *cursor_params, limit + 1,
                0 if cursor else offset,
            ),
        ).fetchall()
        total = None if cursor else connection.execute(
            f"SELECT COUNT(*) FROM photos p WHERE p.deleted_at IS NOT NULL AND {visible}", parameters
        ).fetchone()[0]
        owned_total = connection.execute(
            "SELECT COUNT(*) FROM photos WHERE deleted_at IS NOT NULL AND owner_id=?",
            (actor_id,),
        ).fetchone()[0]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_gallery_cursor(rows[-1]["deleted_at"], rows[-1]["id"])
        if has_more and rows else None
    )
    return _private_json(
        {
            "total": total,
            "owned_total": owned_total,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "scope": scope,
            "photos": [photo_json(row, actor_id=actor_id) for row in rows],
        }
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
    return _gallery_period_value(request.args.get("period"))


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


def photo_json(row, actor_id=None):
    values = dict(row)
    is_video = values.get("content_type", "").startswith("video/")
    deleted_prefix = "/media/deleted" if values.get("deleted_at") else "/media"
    is_mine = bool(actor_id and values.get("owner_id") == actor_id)
    payload = {
        "id": values["id"],
        "original_name": values["original_name"],
        "visibility": values.get("visibility") or "shared",
        "version": int(values.get("version") or 1),
        "is_mine": is_mine,
        "can_edit": is_mine,
        "ownership_status": "owned" if values.get("owner_id") else "legacy_unclaimed",
        "owner_display": (
            values.get("owner_name") or "Owner"
            if values.get("owner_id")
            else "Legacy (unclaimed)"
        ),
        "is_video": is_video,
        "captured_at": values.get("capture_timestamp") or values.get("taken_at"),
        "caption": values.get("caption") or "",
        "caption_version": int(values.get("caption_version") or 0),
        "favorite": bool(values.get("favorite", 0)),
        "state_version": int(values.get("state_version") or 0),
        "loop_playback": bool(values.get("loop_playback", 0)),
        "thumb": f"{deleted_prefix}/thumb/{row['id']}",
        "preview": f"{deleted_prefix}/preview/{row['id']}",
        "view": f"{deleted_prefix}/view/{row['id']}",
        "playback": f"{deleted_prefix}/play/{row['id']}" if is_video else None,
        "original": f"{deleted_prefix}/original/{row['id']}",
    }
    if not is_video:
        payload["detail"] = f"{deleted_prefix}/detail/{row['id']}"
        payload["full"] = f"{deleted_prefix}/full/{row['id']}"
    else:
        payload["detail"] = None
        payload["full"] = None
    return payload


def _media_mutation_version(data, name, *, allow_zero=False):
    value = data.get(name)
    if isinstance(value, bool):
        raise ValueError
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError from error
    if parsed < (0 if allow_zero else 1):
        raise ValueError
    return parsed


def _personal_media_object_id(photo_id, principal_id):
    principal_digest = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()[:20]
    return f"{photo_id}:principal-{principal_digest}"


@app.put("/api/photos/<photo_id>/favorite")
def update_photo_favorite(photo_id):
    actor = _verified_media_actor()
    if actor is None:
        return _private_json(
            {"error": "Open David-Pi through an approved private Tailscale account."},
            403,
        )
    data, error_response = _bounded_media_json(
        "Choose whether this item is a favorite."
    )
    if error_response is not None:
        return error_response
    if not isinstance(data.get("favorite"), bool):
        return _private_json({"error": "Choose whether this item is a favorite."}, 400)
    try:
        media_version = _media_mutation_version(data, "media_version")
        state_version = _media_mutation_version(
            data, "state_version", allow_zero=True,
        )
    except ValueError:
        return _private_json({"error": "Reload this media item and try again."}, 400)
    desired = bool(data["favorite"])
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            media = connection.execute(
                "SELECT * FROM photos WHERE id=? AND deleted_at IS NULL", (photo_id,),
            ).fetchone()
            if media is None or not row_is_visible(media, actor):
                return _private_json({"error": "Media item not found."}, 404)
            current = connection.execute(
                """SELECT * FROM media_personal_state
                   WHERE photo_id=? AND principal_id=?""",
                (photo_id, actor.principal_id),
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if int(media["version"]) != media_version or current_version != state_version:
                return _private_json(
                    {"error": "Media state changed elsewhere. Reload and try again.", "conflict": True},
                    409,
                )
            decision = decide_transaction_authorization(
                mutation_policy("media.favorite.update"),
                actor,
                AuthorizationFacts(
                    personal_owner_id=current["principal_id"] if current else None,
                    allow_personal_state_create=current is None,
                ),
            )
            if not decision.allowed:
                return _private_json({"error": "Favorite state is unavailable."}, 403)
            current_value = bool(current["favorite"]) if current else False
            if current_value == desired:
                return _private_json(
                    {
                        "ok": True,
                        "id": photo_id,
                        "favorite": desired,
                        "state_version": current_version,
                        "media_version": int(media["version"]),
                    }
                )
            now = utcnow()
            if current is None:
                result = connection.execute(
                    """INSERT INTO media_personal_state
                       (photo_id,principal_id,favorite,version,created_at,updated_at)
                       SELECT ?,?,?,1,?,?
                       WHERE NOT EXISTS (
                         SELECT 1 FROM media_personal_state
                         WHERE photo_id=? AND principal_id=?
                       )""",
                    (
                        photo_id, actor.principal_id, int(desired), now, now,
                        photo_id, actor.principal_id,
                    ),
                )
            else:
                result = connection.execute(
                    """UPDATE media_personal_state
                       SET favorite=?,version=version+1,updated_at=?
                       WHERE photo_id=? AND principal_id=? AND version=?""",
                    (
                        int(desired), now, photo_id, actor.principal_id,
                        current_version,
                    ),
                )
            if result.rowcount != 1:
                raise sqlite3.IntegrityError("media personal state version changed")
            saved = connection.execute(
                """SELECT * FROM media_personal_state
                   WHERE photo_id=? AND principal_id=?""",
                (photo_id, actor.principal_id),
            ).fetchone()
            audit_mutation(
                connection,
                actor=actor,
                domain="media_personal_state",
                object_id=_personal_media_object_id(photo_id, actor.principal_id),
                action="favorite",
                before=current,
                after=saved,
            )
    except sqlite3.IntegrityError:
        return _private_json(
            {"error": "Media state changed elsewhere. Reload and try again.", "conflict": True},
            409,
        )
    return _private_json(
        {
            "ok": True,
            "id": photo_id,
            "favorite": desired,
            "state_version": int(saved["version"]),
            "media_version": int(media["version"]),
        }
    )


@app.put("/api/photos/<photo_id>/caption")
def update_photo_caption(photo_id):
    actor = _verified_media_actor()
    if actor is None:
        return _private_json(
            {"error": "Open David-Pi through an approved private Tailscale account."},
            403,
        )
    data, error_response = _bounded_media_json("Choose a valid caption.")
    if error_response is not None:
        return error_response
    try:
        caption = _clean_media_text(
            data.get("caption"), maximum=MAX_MEDIA_CAPTION_CHARACTERS,
            label="Caption",
        )
        media_version = _media_mutation_version(data, "media_version")
        caption_version = _media_mutation_version(
            data, "caption_version", allow_zero=True,
        )
    except ValueError:
        return _private_json({"error": "Choose a valid caption and reload the item."}, 400)
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            media = connection.execute(
                "SELECT * FROM photos WHERE id=? AND deleted_at IS NULL", (photo_id,),
            ).fetchone()
            if media is None or not row_is_visible(media, actor):
                return _private_json({"error": "Media item not found."}, 404)
            if media["owner_id"] != actor.principal_id or not authorize(
                "media.caption.update", actor, media,
            ).allowed:
                return _private_json(
                    {"error": "Only the current media owner can edit its caption."}, 403,
                )
            current = connection.execute(
                "SELECT * FROM media_captions WHERE photo_id=?", (photo_id,),
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if int(media["version"]) != media_version or current_version != caption_version:
                return _private_json(
                    {"error": "Caption changed elsewhere. Reload and try again.", "conflict": True},
                    409,
                )
            current_value = current["caption"] if current else ""
            if current_value == caption:
                return _private_json(
                    {
                        "ok": True,
                        "id": photo_id,
                        "caption": caption,
                        "caption_version": current_version,
                        "media_version": int(media["version"]),
                    }
                )
            now = utcnow()
            if current is None:
                result = connection.execute(
                    """INSERT INTO media_captions
                       (photo_id,caption,version,created_at,updated_at)
                       SELECT ?,?,1,?,?
                       WHERE NOT EXISTS (
                         SELECT 1 FROM media_captions WHERE photo_id=?
                       )""",
                    (photo_id, caption, now, now, photo_id),
                )
            else:
                result = connection.execute(
                    """UPDATE media_captions
                       SET caption=?,version=version+1,updated_at=?
                       WHERE photo_id=? AND version=?""",
                    (caption, now, photo_id, current_version),
                )
            if result.rowcount != 1:
                raise sqlite3.IntegrityError("media caption version changed")
            saved = connection.execute(
                "SELECT * FROM media_captions WHERE photo_id=?", (photo_id,),
            ).fetchone()
            audit_mutation(
                connection,
                actor=actor,
                domain="media_caption",
                object_id=photo_id,
                action="caption",
                before=current,
                after=saved,
            )
    except sqlite3.IntegrityError:
        return _private_json(
            {"error": "Caption changed elsewhere. Reload and try again.", "conflict": True},
            409,
        )
    return _private_json(
        {
            "ok": True,
            "id": photo_id,
            "caption": saved["caption"],
            "caption_version": int(saved["version"]),
            "media_version": int(media["version"]),
        }
    )


def managed_path(root, relative):
    root = root.resolve()
    candidate = root / str(relative)
    if candidate.is_symlink():
        raise ValueError("Managed objects cannot be symbolic links.")
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise ValueError("Managed object path escaped its storage root.")
    return resolved


def serve_media_descriptor(
    storage,
    relative,
    *,
    mimetype=None,
    expected_size=None,
    as_attachment=False,
    download_name=None,
    max_age=0,
    cache_scope=None,
    validator_version=None,
):
    """Serve the exact regular inode opened beneath a pinned media root."""
    descriptor = -1
    try:
        descriptor, metadata = storage.open_regular_path(
            relative, expected_size=expected_size
        )
        # Passing a generic file object makes Werkzeug omit the known length
        # from its conditional response machinery, so video Range requests
        # silently degrade to full 200 responses.  /proc/self/fd resolves to
        # the already verified inode and send_file opens its own descriptor
        # synchronously, preserving both descriptor safety and 206 support.
        validator = (
            f"{metadata.st_dev:x}-{metadata.st_ino:x}-"
            f"{metadata.st_size:x}-{metadata.st_mtime_ns:x}"
        )
        if validator_version is not None:
            # Bind conditional requests to the authorization-bearing database
            # row as well as the file inode. Visibility/trash/restore changes
            # increment this version, so an old shared validator cannot become
            # current again after a later transition back to shared.
            validator += f"-v{int(validator_version):x}"
        response = send_file(
            f"/proc/self/fd/{descriptor}",
            mimetype=mimetype,
            as_attachment=as_attachment,
            download_name=download_name,
            conditional=True,
            max_age=max_age,
            # Werkzeug otherwise derives the validator from the ephemeral
            # /proc fd pathname, making resumable video requests restart when
            # the numeric descriptor differs on the next request.
            etag=validator,
        )
        if cache_scope in {"shared", "private"}:
            # Internal marker consumed and removed by the security middleware.
            response.headers["X-David-Pi-Media-Cache-Scope"] = cache_scope
        return response
    except (OSError, ValueError, StorageSafetyError):
        return "Not found", 404
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def destroy_photo(photo_id, cutoff=None, enforce_visibility=False, enforce_owner=False):
    quarantined = []
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sql = "SELECT * FROM photos WHERE id = ? AND deleted_at IS NOT NULL"
        parameters = [photo_id]
        if cutoff is not None:
            sql += " AND deleted_at < ?"
            parameters.append(cutoff)
        if enforce_owner:
            owned, owned_parameters = ownership_sql()
            sql += f" AND {owned}"
            parameters.extend(owned_parameters)
        elif enforce_visibility:
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


def requested_photo_items():
    """Return exact id/version pairs for a saved-media mutation.

    The browser must bind actions to the version it displayed.  Ownership and
    current versions are still re-read under BEGIN IMMEDIATE before mutation.
    """
    data = request.get_json(silent=True) or {}
    values = data.get("items")
    if not isinstance(values, list) or not values or len(values) > 500:
        return None, "Choose between 1 and 500 current media items."
    result = {}
    for value in values:
        if not isinstance(value, dict):
            return None, "Reload the media library before continuing."
        photo_id = str(value.get("id", "")).strip()
        try:
            version = int(value.get("version"))
        except (TypeError, ValueError):
            return None, "Reload the media library before continuing."
        if not photo_id or len(photo_id) > 128 or version < 1:
            return None, "Reload the media library before continuing."
        previous = result.get(photo_id)
        if previous is not None and previous != version:
            return None, "The media selection contains conflicting versions."
        result[photo_id] = version
    return list(result.items()), None


def _media_rows_for_write(
    connection, items, actor, route_id, *, deleted=None, require_owner=False
):
    ids = [photo_id for photo_id, _ in items]
    expected = dict(items)
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM photos WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(ids) or any(
        not row_is_visible(by_id.get(photo_id), actor)
        for photo_id in ids
        if by_id.get(photo_id) is not None
    ):
        return None, (jsonify(error="One or more media items could not be found."), 404)
    for photo_id in ids:
        row = by_id[photo_id]
        if deleted is True and not row["deleted_at"]:
            return None, (jsonify(error="One or more media items are not in Recently Deleted."), 409)
        if deleted is False and row["deleted_at"]:
            return None, (jsonify(error="One or more media items are already deleted."), 409)
        if int(row["version"]) != expected[photo_id]:
            return None, (jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409)
        if require_owner and row["owner_id"] != actor.principal_id:
            return None, (jsonify(error="Only the media owner can restore it."), 403)
        decision = authorize(route_id, actor, row)
        if not decision.allowed:
            return None, (jsonify(error="Shared media is read-only unless you own it."), 403)
    return [by_id[photo_id] for photo_id in ids], None


@app.patch("/api/photos/visibility")
def set_photos_visibility():
    items, item_error = requested_photo_items()
    if item_error:
        return jsonify(error=item_error), 400
    data = request.get_json(silent=True) or {}
    identity = current_device()
    actor = actor_for_identity(identity)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    try:
        visibility = requested_visibility(data.get("visibility"), actor)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows, error = _media_rows_for_write(
                connection, items, actor, "media.visibility.update"
            )
            if error:
                return error
            for row in rows:
                result = connection.execute(
                    """UPDATE photos SET visibility=?, version=version+1
                       WHERE id=? AND owner_id=? AND version=?""",
                    (visibility, row["id"], actor.principal_id, row["version"]),
                )
                if result.rowcount != 1:
                    raise sqlite3.IntegrityError("media version changed")
                saved = connection.execute(
                    "SELECT * FROM photos WHERE id=?", (row["id"],)
                ).fetchone()
                audit_mutation(
                    connection, actor=actor, domain="media", object_id=row["id"],
                    action="visibility", before=row, after=saved,
                )
    except sqlite3.IntegrityError:
        return jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409
    for row in rows:
        try:
            _project_mytube_link(row["id"])
        except (OSError, ValueError, sqlite3.Error):
            LOGGER.exception("MyTube visibility projection refresh failed")
    return jsonify(
        ok=True,
        count=len(items),
        visibility=visibility,
        items=[{"id": row["id"], "version": int(row["version"]) + 1} for row in rows],
    )


@app.post("/api/photos/trash")
def trash_photos():
    items, item_error = requested_photo_items()
    if item_error:
        return jsonify(error=item_error), 400
    actor = actor_for_identity(current_device())
    now_value = datetime.now(timezone.utc)
    deleted_at = now_value.isoformat()
    purge_after = (now_value + timedelta(days=30)).isoformat()
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows, error = _media_rows_for_write(
                connection, items, actor, "media.trash.bulk", deleted=False
            )
            if error:
                return error
            linked = connection.execute(
                f"SELECT media_id FROM mytube_media_links WHERE media_id IN ({','.join('?' for _ in rows)}) LIMIT 1",
                [row["id"] for row in rows],
            ).fetchone()
            if linked:
                return jsonify(error="Remove linked videos from MyTube before moving them to Recently Deleted."), 409
            for row in rows:
                result = connection.execute(
                    """UPDATE photos SET deleted_at=?,deleted_by_id=?,purge_after=?,
                              version=version+1
                       WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
                    (
                        deleted_at, actor.principal_id, purge_after, row["id"],
                        actor.principal_id, row["version"],
                    ),
                )
                if result.rowcount != 1:
                    raise sqlite3.IntegrityError("media version changed")
                saved = connection.execute(
                    "SELECT * FROM photos WHERE id=?", (row["id"],)
                ).fetchone()
                audit_mutation(
                    connection, actor=actor, domain="media", object_id=row["id"],
                    action="trash", before=row, after=saved,
                )
    except sqlite3.IntegrityError:
        return jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, count=len(rows))


@app.post("/api/photos/restore")
def restore_photos():
    items, item_error = requested_photo_items()
    if item_error:
        return jsonify(error=item_error), 400
    actor = actor_for_identity(current_device())
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows, error = _media_rows_for_write(
                connection,
                items,
                actor,
                "media.restore.bulk",
                deleted=True,
                require_owner=True,
            )
            if error:
                return error
            for row in rows:
                result = connection.execute(
                    """UPDATE photos SET deleted_at=NULL,deleted_by_id=NULL,purge_after=NULL,
                              version=version+1
                       WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NOT NULL""",
                    (row["id"], actor.principal_id, row["version"]),
                )
                if result.rowcount != 1:
                    raise sqlite3.IntegrityError("media version changed")
                saved = connection.execute(
                    "SELECT * FROM photos WHERE id=?", (row["id"],)
                ).fetchone()
                audit_mutation(
                    connection, actor=actor, domain="media", object_id=row["id"],
                    action="restore", before=row, after=saved,
                )
    except sqlite3.IntegrityError:
        return jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, count=len(rows))


@app.post("/api/photos/restore-all")
def restore_all_photos():
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned, owned_params = ownership_sql("p")
            rows = connection.execute(
                f"SELECT p.* FROM photos p WHERE p.deleted_at IS NOT NULL AND {owned} "
                "ORDER BY p.deleted_at,p.id",
                owned_params,
            ).fetchall()
            for row in rows:
                if not authorize("media.restore_all", actor, row).allowed:
                    raise sqlite3.IntegrityError("media authorization changed")
                result = connection.execute(
                    """UPDATE photos SET deleted_at=NULL,deleted_by_id=NULL,purge_after=NULL,
                              version=version+1
                       WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NOT NULL""",
                    (row["id"], actor.principal_id, row["version"]),
                )
                if result.rowcount != 1:
                    raise sqlite3.IntegrityError("media version changed")
                saved = connection.execute(
                    "SELECT * FROM photos WHERE id=?", (row["id"],)
                ).fetchone()
                audit_mutation(
                    connection, actor=actor, domain="media", object_id=row["id"],
                    action="restore", before=row, after=saved,
                )
    except sqlite3.IntegrityError:
        return jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, count=len(rows))


@app.post("/api/photos/purge")
def purge_photos():
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != "permanently-delete-media":
        return jsonify(error="Confirmation is required before permanent deletion."), 400
    items, item_error = requested_photo_items()
    if item_error:
        return jsonify(error=item_error), 400
    actor = actor_for_identity(current_device())
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _rows, error = _media_rows_for_write(
            connection, items, actor, "media.purge.bulk", deleted=True
        )
        if error:
            return error
    return jsonify(
        error="Permanent deletion is retained until the backup and retention gate is verified.",
        retained=True,
        count=0,
    ), 503


@app.post("/api/photos/purge-all")
def purge_all_photos():
    data = request.get_json(silent=True) or {}
    if data.get("confirmation") != "empty-recently-deleted":
        return jsonify(error="Confirmation is required before permanently deleting photos."), 400
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT * FROM photos WHERE deleted_at IS NOT NULL AND owner_id=? ORDER BY deleted_at,id",
            (actor.principal_id,),
        ).fetchall()
        if any(not authorize("media.purge_all", actor, row).allowed for row in rows):
            return jsonify(error="Only an item's owner can permanently delete it."), 403
    return jsonify(
        error="Permanent deletion is retained until the backup and retention gate is verified.",
        retained=True,
        count=0,
        retained_count=len(rows),
    ), 503


@app.delete("/api/photos/<photo_id>")
def delete_photo(photo_id):
    data = request.get_json(silent=True) or {}
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload this media item before deleting it."), 400
    if version < 1:
        return jsonify(error="Reload this media item before deleting it."), 400
    actor = actor_for_identity(current_device())
    now_value = datetime.now(timezone.utc)
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows, error = _media_rows_for_write(
                connection, [(photo_id, version)], actor, "media.trash.single",
                deleted=False,
            )
            if error:
                return error
            row = rows[0]
            if connection.execute(
                "SELECT 1 FROM mytube_media_links WHERE media_id=?", (photo_id,),
            ).fetchone():
                return jsonify(error="Remove this video from MyTube before moving it to Recently Deleted."), 409
            result = connection.execute(
                """UPDATE photos SET deleted_at=?,deleted_by_id=?,purge_after=?,
                          version=version+1
                   WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
                (
                    now_value.isoformat(), actor.principal_id,
                    (now_value + timedelta(days=30)).isoformat(), photo_id,
                    actor.principal_id, version,
                ),
            )
            if result.rowcount != 1:
                raise sqlite3.IntegrityError("media version changed")
            saved = connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
            audit_mutation(
                connection, actor=actor, domain="media", object_id=photo_id,
                action="trash", before=row, after=saved,
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, version=version + 1)


def clean_collection_name():
    data = request.get_json(silent=True) or {}
    name = " ".join(str(data.get("name", "")).split()).strip()
    return name[:80]


@app.get("/api/collections")
def list_collections():
    identity_value = current_device()
    actor = actor_for_identity(identity_value)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    actor_id = actor.principal_id
    if request.args.get("view") == "mine":
        collection_visible, collection_params = "c.owner_id = ?", [actor_id]
    else:
        collection_visible, collection_params = "c.visibility = 'shared'", []
    # Reuse the identity already resolved for this read. Besides avoiding
    # duplicate proxy lookups, this keeps collection metadata and cover/count
    # visibility bound to one request snapshot.
    photo_visible, photo_params = (
        "(p.visibility = 'shared' OR p.owner_id = ?)",
        [actor_id],
    )
    with db() as connection:
        rows = connection.execute(
            f"""SELECT c.* FROM collections c
                WHERE c.deleted_at IS NULL AND {collection_visible}
                ORDER BY c.created_at DESC""",
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
            item = {
                "id": row["id"],
                "name": row["name"],
                "visibility": row["visibility"],
                "version": int(row["version"]),
                "ownership_status": "owned" if row["owner_id"] else "legacy_unclaimed",
                "is_mine": bool(actor_id and row["owner_id"] == actor_id),
                "can_edit": bool(actor_id and row["owner_id"] == actor_id),
                "owner_display": (
                    row["owner_name"] or "Owner"
                    if row["owner_id"] else "Legacy (unclaimed)"
                ),
                "photo_count": count,
                "cover": f"/media/thumb/{cover['id']}" if cover else None,
            }
            result.append(item)
    return jsonify(collections=result, current_user=identity_value["name"])


@app.post("/api/collections")
def create_collection():
    name = clean_collection_name()
    if not name:
        return jsonify(error="Give the collection a name."), 400
    collection_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    data = request.get_json(silent=True) or {}
    identity_value = current_device()
    actor = actor_for_identity(identity_value)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    try:
        visibility = requested_visibility(data.get("visibility", "shared"), actor)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not authorize("collection.create", actor).allowed:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        existing = connection.execute(
            """SELECT id FROM collections WHERE lower(name)=lower(?)
               AND owner_id=? AND deleted_at IS NULL""",
            (name, actor.principal_id),
        ).fetchone()
        if existing:
            return jsonify(error="You already have a collection with that name."), 409
        connection.execute(
            """INSERT INTO collections
               (id,name,created_at,created_by,owner_id,owner_name,visibility,version)
               VALUES (?,?,?,?,?,?,?,1)""",
            (
                collection_id, name, created_at, identity_value["name"],
                actor.principal_id, identity_value["name"], visibility,
            ),
        )
        saved = connection.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,)
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="media_collection",
            object_id=collection_id, action="create", before=None, after=saved,
        )
    return jsonify(id=collection_id, name=name, photo_count=0, cover=None,
                   visibility=visibility, version=1, is_mine=True, can_edit=True,
                   ownership_status="owned", owner_display=identity_value["name"]), 201


@app.patch("/api/collections/<collection_id>")
def rename_collection(collection_id):
    name = clean_collection_name()
    if not name:
        return jsonify(error="Give the collection a name."), 400
    data = request.get_json(silent=True) or {}
    try:
        expected_version = int(data.get("version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload this collection before changing it."), 400
    if expected_version < 1:
        return jsonify(error="Reload this collection before changing it."), 400
    visibility = data.get("visibility")
    actor = actor_for_identity(current_device())
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            if not row or row["deleted_at"] or not row_is_visible(row, actor):
                return jsonify(error="Collection not found."), 404
            if not authorize("collection.update", actor, row).allowed:
                return jsonify(error="This shared collection is read-only because you are not its owner."), 403
            if int(row["version"]) != expected_version:
                return jsonify(error="Collection changed elsewhere. Reload before continuing.", conflict=True), 409
            conflict = connection.execute(
                """SELECT id FROM collections WHERE lower(name)=lower(?) AND id!=?
                   AND owner_id=? AND deleted_at IS NULL""",
                (name, collection_id, actor.principal_id),
            ).fetchone()
            if conflict:
                return jsonify(error="You already have a collection with that name."), 409
            if visibility is not None:
                try:
                    visibility = requested_visibility(visibility, actor)
                except ValueError as error:
                    return jsonify(error=str(error)), 400
            else:
                visibility = row["visibility"]
            if visibility == "private" and row["visibility"] != "private":
                foreign_members = connection.execute(
                    """SELECT COUNT(*) FROM collection_photos cp
                       JOIN photos p ON p.id=cp.photo_id
                       WHERE cp.collection_id=?
                         AND (p.owner_id IS NULL OR p.owner_id<>?)""",
                    (collection_id, actor.principal_id),
                ).fetchone()[0]
                if foreign_members:
                    return jsonify(
                        error=(
                            "Remove media owned by other people before making "
                            "this collection private."
                        )
                    ), 409
            result = connection.execute(
                """UPDATE collections SET name=?,visibility=?,version=version+1
                   WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
                (name, visibility, collection_id, actor.principal_id, expected_version),
            )
            if result.rowcount != 1:
                raise sqlite3.IntegrityError("collection version changed")
            saved = connection.execute(
                "SELECT * FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            audit_mutation(
                connection, actor=actor, domain="media_collection",
                object_id=collection_id, action="update", before=row, after=saved,
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Collection changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, name=name, visibility=visibility, version=expected_version + 1)


@app.delete("/api/collections/<collection_id>")
def delete_collection(collection_id):
    data = request.get_json(silent=True) or {}
    try:
        expected_version = int(data.get("version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload this collection before deleting it."), 400
    if expected_version < 1:
        return jsonify(error="Reload this collection before deleting it."), 400
    actor = actor_for_identity(current_device())
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            if not row or row["deleted_at"] or not row_is_visible(row, actor):
                return jsonify(error="Collection not found."), 404
            if not authorize("collection.delete", actor, row).allowed:
                return jsonify(error="This shared collection is read-only because you are not its owner."), 403
            if int(row["version"]) != expected_version:
                return jsonify(error="Collection changed elsewhere. Reload before continuing.", conflict=True), 409
            result = connection.execute(
                """UPDATE collections SET deleted_at=?,deleted_by_id=?,version=version+1
                   WHERE id=? AND owner_id=? AND version=? AND deleted_at IS NULL""",
                (
                    datetime.now(timezone.utc).isoformat(), actor.principal_id,
                    collection_id, actor.principal_id, expected_version,
                ),
            )
            if result.rowcount != 1:
                raise sqlite3.IntegrityError("collection version changed")
            saved = connection.execute(
                "SELECT * FROM collections WHERE id=?", (collection_id,)
            ).fetchone()
            audit_mutation(
                connection, actor=actor, domain="media_collection",
                object_id=collection_id, action="trash", before=row, after=saved,
            )
    except sqlite3.IntegrityError:
        return jsonify(error="Collection changed elsewhere. Reload before continuing.", conflict=True), 409
    return jsonify(ok=True, version=expected_version + 1, retained=True)


SLIDESHOW_TRANSITIONS = {"none", "mixed", "fade", "dissolve", "wipeleft", "slideright", "circleopen"}
SLIDESHOW_MIXED_TRANSITIONS = ("fade", "dissolve", "wipeleft", "slideright", "circleopen")
SLIDESHOW_LAYOUTS = {"balanced", "fill", "fit"}
SLIDESHOW_MAX_ITEMS = 200
try:
    SLIDESHOW_QUEUE_LIMIT = int(os.environ.get("DAVID_PI_SLIDESHOW_QUEUE_LIMIT", "3"))
except ValueError as error:
    raise RuntimeError("DAVID_PI_SLIDESHOW_QUEUE_LIMIT must be an integer") from error
if not 1 <= SLIDESHOW_QUEUE_LIMIT <= 20:
    raise RuntimeError("DAVID_PI_SLIDESHOW_QUEUE_LIMIT must be between 1 and 20")
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


def slideshow_snapshot_digest(parameters):
    """Bind one queued render to the exact authorized source snapshot."""
    if not isinstance(parameters, dict):
        raise ValueError("The slideshow source snapshot is invalid.")
    snapshot = {
        "collection_id": parameters.get("collection_id"),
        "collection_version": parameters.get("collection_version"),
        "collection_owner_id": parameters.get("collection_owner_id"),
        "collection_visibility": parameters.get("collection_visibility"),
        "collection_deleted_at": parameters.get("collection_deleted_at"),
        "source_name": parameters.get("source_name"),
        "result_visibility": parameters.get("result_visibility"),
        "media_items": parameters.get("media_items"),
        "duration_seconds": parameters.get("duration_seconds"),
        "transition": parameters.get("transition"),
        "layout": parameters.get("layout"),
        "music_id": parameters.get("music_id"),
        "loop_playback": parameters.get("loop_playback"),
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def slideshow_source_snapshot(
    connection, job, parameters, expected_versions, *, require_worker_lease=False
):
    """Load a slideshow's exact, still-authorized source snapshot.

    The worker calls this before rendering for an early failure and again from
    canonical_ingest_media's write transaction before publishing the result.
    """
    if require_worker_lease:
        live_job = connection.execute(
            "SELECT * FROM slideshow_jobs WHERE id=?", (job["id"],)
        ).fetchone()
        try:
            lease_expiry = datetime.fromisoformat(
                str(live_job["lease_expires_at"]).replace("Z", "+00:00")
            )
            if lease_expiry.tzinfo is None:
                raise ValueError("naive lease")
            lease_expiry = lease_expiry.astimezone(timezone.utc)
        except (AttributeError, TypeError, ValueError, OverflowError):
            lease_expiry = None
        if (
            not live_job
            or live_job["status"] != "working"
            or int(live_job["generation"]) != int(job["generation"])
            or live_job["lease_owner"] != job["lease_owner"]
            or live_job["lease_token"] != job["lease_token"]
            or live_job["owner_id"] != job["owner_id"]
            or live_job["owner_name"] != job["owner_name"]
            or live_job["visibility"] != job["visibility"]
            or live_job["visibility"] != parameters.get("result_visibility")
            or live_job["target_photo_id"] != job["target_photo_id"]
            or live_job["publish_intent_id"] != job["publish_intent_id"]
            or live_job["target_name"] != job["target_name"]
            or live_job["target_dev"] != job["target_dev"]
            or live_job["target_ino"] != job["target_ino"]
            or live_job["target_size"] != job["target_size"]
            or live_job["target_sha256"] != job["target_sha256"]
            or live_job["source_snapshot_sha256"] != job["source_snapshot_sha256"]
            or lease_expiry is None
            or lease_expiry <= datetime.now(timezone.utc)
        ):
            raise ValueError("The slideshow worker lease changed before publication.")

    ids = list(expected_versions)
    placeholders = ",".join("?" for _ in ids)
    source_collection = connection.execute(
        """SELECT id,name,owner_id,visibility,version,deleted_at FROM collections
           WHERE id=?""",
        (parameters["collection_id"],),
    ).fetchone()
    if (
        not source_collection
        or source_collection["deleted_at"]
        or parameters.get("collection_deleted_at") is not None
        or not source_collection["owner_id"]
        or int(source_collection["version"]) != int(parameters["collection_version"])
        or source_collection["name"] != parameters.get("source_name")
        or source_collection["owner_id"] != parameters.get("collection_owner_id")
        or source_collection["visibility"] != parameters.get("collection_visibility")
        or (
            source_collection["owner_id"] != job["owner_id"]
            and source_collection["visibility"] != "shared"
        )
    ):
        raise ValueError("The source collection changed before rendering.")
    rows = connection.execute(
        f"SELECT p.id, p.preview_name, p.stored_path, p.playback_name, p.byte_size, "
        f"p.content_type, p.content_sha256, p.owner_id, p.visibility, p.version, p.deleted_at "
        f"FROM collection_photos cp JOIN photos p ON p.id=cp.photo_id "
        f"WHERE cp.collection_id=? AND p.id IN ({placeholders}) "
        f"AND p.deleted_at IS NULL",
        (parameters["collection_id"], *ids),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    ordered = [dict(by_id[media_id]) for media_id in ids if media_id in by_id]
    if len(ordered) != len(ids) or any(
        not item["owner_id"]
        or item["deleted_at"]
        or int(item["version"]) != expected_versions[item["id"]]["version"]
        or item["owner_id"] != expected_versions[item["id"]]["owner_id"]
        or item["visibility"] != expected_versions[item["id"]]["visibility"]
        or expected_versions[item["id"]]["deleted_at"] is not None
        or item["content_sha256"] != expected_versions[item["id"]]["content_sha256"]
        or int(item["byte_size"]) != expected_versions[item["id"]]["byte_size"]
        or item["stored_path"] != expected_versions[item["id"]]["stored_path"]
        or item["preview_name"] != expected_versions[item["id"]]["preview_name"]
        or item["playback_name"] != expected_versions[item["id"]]["playback_name"]
        or item["content_type"] != expected_versions[item["id"]]["content_type"]
        or (
            item["owner_id"] != job["owner_id"]
            and item["visibility"] != "shared"
        )
        for item in ordered
    ) or not hmac.compare_digest(
        slideshow_snapshot_digest(parameters),
        str(job["source_snapshot_sha256"] or ""),
    ):
        raise ValueError("One or more source items are no longer available.")
    return ordered


@app.get("/api/slideshows/options")
def slideshow_options():
    collection_visible, collection_params = visibility_sql("c")
    photo_visible, photo_params = visibility_sql("p")
    with db() as connection:
        rows = connection.execute(
            "SELECT c.id, c.name, c.version, COUNT(p.id) AS item_count, "
            "SUM(CASE WHEN p.content_type LIKE 'video/%' THEN 1 ELSE 0 END) AS video_count, "
            "SUM(CASE WHEN p.id IS NOT NULL AND p.content_type NOT LIKE 'video/%' THEN 1 ELSE 0 END) AS image_count, "
            "SUM(CASE WHEN p.id IS NOT NULL AND p.owner_id IS NULL THEN 1 ELSE 0 END) AS legacy_count "
            "FROM collections c "
            "LEFT JOIN collection_photos cp ON cp.collection_id = c.id "
            f"LEFT JOIN photos p ON p.id = cp.photo_id AND p.deleted_at IS NULL AND {photo_visible} "
            f"WHERE c.deleted_at IS NULL AND c.owner_id IS NOT NULL AND {collection_visible} "
            "GROUP BY c.id ORDER BY lower(c.name)",
            (*photo_params, *collection_params),
        ).fetchall()
    return jsonify(
        collections=[
            {
                "id": row["id"], "name": row["name"],
                "version": int(row["version"]),
                "item_count": int(row["item_count"]),
                "video_count": int(row["video_count"] or 0),
                "image_count": int(row["image_count"] or 0),
            }
            for row in rows
            if row["item_count"] and not row["legacy_count"]
        ],
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
    if SLIDESHOW_EXECUTOR_MODE != "queue":
        return jsonify(error="The video queue is not available in this service."), 503
    data = request.get_json(silent=True) or {}
    collection_id = str(data.get("collection_id", "")).strip()
    transition = str(data.get("transition", "fade")).strip().lower()
    layout = str(data.get("layout", "balanced")).strip().lower()
    music_id = str(data.get("music_id", "")).strip()
    try:
        duration_seconds = int(data.get("duration_seconds", 30))
        collection_version = int(data.get("collection_version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload the media collection before creating a video."), 400
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

    if shutil.disk_usage(DATA).free < 1024 * 1024 * 1024:
        return jsonify(error="David-Pi needs at least 1 GB free before creating a slideshow."), 507

    identity_value = current_device()
    actor = actor_for_identity(identity_value)
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    job_id = uuid.uuid4().hex
    target_photo_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        collection = connection.execute(
            "SELECT * FROM collections WHERE id=?", (collection_id,),
        ).fetchone()
        if not collection or collection["deleted_at"] or not row_is_visible(collection, actor):
            return jsonify(error="That collection could not be found."), 404
        if not collection["owner_id"]:
            return jsonify(error="Legacy unclaimed collections are read-only."), 403
        if collection_version < 1 or int(collection["version"]) != collection_version:
            return jsonify(error="The collection changed elsewhere. Reload before continuing.", conflict=True), 409
        photo_visible, photo_params = visibility_sql("p")
        rows = connection.execute(
            "SELECT p.id,p.version,p.owner_id,p.visibility,p.deleted_at,"
            "p.content_sha256,p.byte_size,p.stored_path,p.preview_name,"
            "p.playback_name,p.content_type FROM collection_photos cp "
            "JOIN photos p ON p.id=cp.photo_id "
            f"WHERE cp.collection_id=? AND p.deleted_at IS NULL AND {photo_visible} "
            "ORDER BY p.taken_at ASC,p.uploaded_at ASC,p.id ASC",
            (collection_id, *photo_params),
        ).fetchall()
        if not rows:
            return jsonify(error="This collection does not contain any media."), 400
        if any(not row["owner_id"] for row in rows):
            return jsonify(error="Claim legacy media before using it in a new video."), 403
        if len(rows) > SLIDESHOW_MAX_ITEMS:
            return jsonify(error=f"Choose a collection with no more than {SLIDESHOW_MAX_ITEMS} items."), 400
        decision = decide_transaction_authorization(
            mutation_policy("slideshow.create"), actor,
            AuthorizationFacts(sources_visible=True, legacy=False),
        )
        if not decision.allowed:
            return jsonify(error="The slideshow sources are not available to this account."), 403
        parameters = {
            "collection_id": collection_id,
            "collection_version": collection_version,
            "collection_owner_id": collection["owner_id"],
            "collection_visibility": collection["visibility"],
            "collection_deleted_at": None,
            "source_name": collection["name"],
            "result_visibility": (
                "private"
                if collection["visibility"] == "private"
                or any(row["visibility"] == "private" for row in rows)
                else "shared"
            ),
            "media_items": [
                {
                    "id": row["id"],
                    "version": int(row["version"]),
                    "owner_id": row["owner_id"],
                    "visibility": row["visibility"],
                    "deleted_at": row["deleted_at"],
                    "content_sha256": row["content_sha256"],
                    "byte_size": int(row["byte_size"]),
                    "stored_path": row["stored_path"],
                    "preview_name": row["preview_name"],
                    "playback_name": row["playback_name"],
                    "content_type": row["content_type"],
                }
                for row in rows
            ],
            "duration_seconds": duration_seconds,
            "transition": transition,
            "layout": layout,
            "music_id": music_id or None,
            "loop_playback": bool(data.get("loop_playback")),
            "created_by": identity_value["name"],
        }
        parameters_json = json.dumps(parameters, sort_keys=True)
        source_snapshot_sha256 = slideshow_snapshot_digest(parameters)
        duplicate = connection.execute(
            "SELECT id,status FROM slideshow_jobs "
            "WHERE status IN ('queued','working') AND owner_id = ? "
            "AND parameters_json = ? ORDER BY created_at,id LIMIT 1",
            (actor.principal_id, parameters_json),
        ).fetchone()
        if duplicate:
            return jsonify(
                id=duplicate["id"], status=duplicate["status"], duplicate=True
            ), 202
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM slideshow_jobs WHERE status IN ('queued','working')"
        ).fetchone()[0]
        if pending_count >= SLIDESHOW_QUEUE_LIMIT:
            return jsonify(error="The video queue is full. Try again after one finishes."), 429
        connection.execute(
            """INSERT INTO slideshow_jobs
               (id,status,progress,message,parameters_json,result_photo_id,error,created_at,updated_at,
                owner_id,owner_name,visibility,version,generation,attempt_count,target_photo_id,
                source_snapshot_sha256,publish_intent_id,publish_state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,1,0,?,?,?,'none')""",
            (
                job_id, "queued", 5, "Getting your media ready…", parameters_json, None, None, now, now,
                actor.principal_id, identity_value["name"], parameters["result_visibility"],
                target_photo_id, source_snapshot_sha256, target_photo_id,
            ),
        )
        saved = connection.execute(
            "SELECT * FROM slideshow_jobs WHERE id=?", (job_id,),
        ).fetchone()
        audit_mutation(
            connection, actor=actor, domain="media_slideshow", object_id=job_id,
            action="create", before=None, after=saved,
        )
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
            "SELECT c.id, c.name, c.version, "
            "CASE WHEN cp.photo_id IS NULL THEN 0 ELSE 1 END AS selected "
            "FROM collections c LEFT JOIN collection_photos cp ON cp.collection_id = c.id AND cp.photo_id = ? "
            f"WHERE c.deleted_at IS NULL AND c.owner_id IS NOT NULL AND {visible} "
            "ORDER BY lower(c.name)", (photo_id, *parameters)
        ).fetchall()
    return jsonify(collections=[dict(row) for row in rows])


def validate_active_photos(connection, ids):
    if not ids:
        return False
    placeholders = ",".join("?" for _ in ids)
    visible, parameters = visibility_sql()
    count = connection.execute(
        f"SELECT COUNT(*) FROM photos WHERE id IN ({placeholders}) AND deleted_at IS NULL AND {visible}",
        (*ids, *parameters),
    ).fetchone()[0]
    return count == len(ids)


def _membership_media_rows(connection, items, actor):
    ids = [photo_id for photo_id, _ in items]
    expected = dict(items)
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM photos WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(ids) or any(
        not row_is_visible(by_id.get(photo_id), actor)
        for photo_id in ids if by_id.get(photo_id) is not None
    ):
        return None, (jsonify(error="One or more photos could not be found."), 404)
    for photo_id in ids:
        row = by_id[photo_id]
        if row["deleted_at"]:
            return None, (jsonify(error="Restore deleted media before organizing it."), 409)
        if not row["owner_id"]:
            return None, (jsonify(error="Legacy unclaimed media is read-only."), 403)
        if int(row["version"]) != expected[photo_id]:
            return None, (jsonify(error="Media changed elsewhere. Reload before continuing.", conflict=True), 409)
    return by_id, None


def _apply_collection_membership(items, changes, route_id):
    actor = actor_for_identity(current_device())
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return None, (jsonify(error="Open David-Pi through an approved private Tailscale account."), 403)
    ids = [photo_id for photo_id, _ in items]
    collection_ids = list(changes)
    photo_placeholders = ",".join("?" for _ in ids)
    collection_placeholders = ",".join("?" for _ in collection_ids)
    now = datetime.now(timezone.utc).isoformat()
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            photos, error = _membership_media_rows(connection, items, actor)
            if error:
                return None, error
            collection_rows = connection.execute(
                f"SELECT * FROM collections WHERE id IN ({collection_placeholders})",
                collection_ids,
            ).fetchall()
            collections_by_id = {row["id"]: row for row in collection_rows}
            if len(collections_by_id) != len(collection_ids) or any(
                row["deleted_at"] or not row_is_visible(row, actor)
                for row in collection_rows
            ):
                return None, (jsonify(error="One or more collections could not be found."), 404)

            memberships = connection.execute(
                f"""SELECT collection_id,photo_id,added_by_id,version
                    FROM collection_photos
                    WHERE collection_id IN ({collection_placeholders})
                      AND photo_id IN ({photo_placeholders})""",
                (*collection_ids, *ids),
            ).fetchall()
            membership_by_pair = {
                (row["collection_id"], row["photo_id"]): row for row in memberships
            }

            for collection_id, change in changes.items():
                collection = collections_by_id[collection_id]
                if not collection["owner_id"]:
                    return None, (jsonify(error="Legacy unclaimed collections are read-only."), 403)
                if int(collection["version"]) != change["version"]:
                    return None, (jsonify(error="A collection changed elsewhere. Reload before continuing.", conflict=True), 409)
                if not authorize(route_id, actor, collection).allowed:
                    return None, (jsonify(error="That collection is read-only for this account."), 403)
                if change["action"] == "add" and collection["visibility"] == "private":
                    if any(
                        photos[photo_id]["owner_id"] != actor.principal_id
                        for photo_id in ids
                    ):
                        return None, (
                            jsonify(
                                error=(
                                    "Only media you own can be added to a private collection."
                                )
                            ),
                            403,
                        )
                if change["action"] != "remove":
                    continue
                for photo_id in ids:
                    relation = membership_by_pair.get((collection_id, photo_id))
                    if relation is None:
                        continue
                    contribution_owner = relation["added_by_id"]
                    if photos[photo_id]["owner_id"] == actor.principal_id:
                        contribution_owner = actor.principal_id
                    if route_id == "collection.photo_membership.remove":
                        facts = AuthorizationFacts(
                            owner_id=collection["owner_id"],
                            visibility=collection["visibility"],
                            contribution_owner_id=contribution_owner,
                            legacy=False,
                        )
                        allowed_remove = decide_transaction_authorization(
                            mutation_policy(route_id), actor, facts
                        ).allowed
                    else:
                        allowed_remove = (
                            collection["owner_id"] == actor.principal_id
                            or contribution_owner == actor.principal_id
                        )
                    if not allowed_remove:
                        return None, (jsonify(error="You can only remove media you own or contributed."), 403)

            added = removed = 0
            changed_versions = {}
            for collection_id, change in changes.items():
                collection = collections_by_id[collection_id]
                changed = 0
                if change["action"] == "add":
                    for photo_id in ids:
                        before_changes = connection.total_changes
                        connection.execute(
                            """INSERT OR IGNORE INTO collection_photos
                               (collection_id,photo_id,added_at,added_by_id,version)
                               VALUES (?,?,?,?,1)""",
                            (collection_id, photo_id, now, actor.principal_id),
                        )
                        inserted = connection.total_changes - before_changes
                        added += inserted
                        changed += inserted
                else:
                    result = connection.execute(
                        f"""DELETE FROM collection_photos
                            WHERE collection_id=? AND photo_id IN ({photo_placeholders})""",
                        (collection_id, *ids),
                    )
                    removed += result.rowcount
                    changed += result.rowcount
                if not changed:
                    changed_versions[collection_id] = int(collection["version"])
                    continue
                updated = connection.execute(
                    """UPDATE collections SET version=version+1
                       WHERE id=? AND version=? AND deleted_at IS NULL""",
                    (collection_id, collection["version"]),
                )
                if updated.rowcount != 1:
                    raise sqlite3.IntegrityError("collection version changed")
                saved = connection.execute(
                    "SELECT * FROM collections WHERE id=?", (collection_id,)
                ).fetchone()
                audit_mutation(
                    connection, actor=actor, domain="media_collection_membership",
                    object_id=collection_id, action="membership_update",
                    before=collection, after=saved,
                )
                changed_versions[collection_id] = int(saved["version"])
    except sqlite3.IntegrityError:
        return None, (jsonify(error="Collection membership changed elsewhere. Reload before continuing.", conflict=True), 409)
    return {
        "ok": True,
        "added": added,
        "removed": removed,
        "changed_collections": sum(
            changed_versions[collection_id] != changes[collection_id]["version"]
            for collection_id in collection_ids
        ),
        "collection_versions": changed_versions,
    }, None


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
            "SELECT c.id, c.name, c.version, COUNT(cp.photo_id) AS selected_count "
            "FROM collections c LEFT JOIN collection_photos cp "
            f"ON cp.collection_id = c.id AND cp.photo_id IN ({placeholders}) "
            f"WHERE c.deleted_at IS NULL AND c.owner_id IS NOT NULL "
            f"AND {visibility_sql('c')[0]} GROUP BY c.id ORDER BY lower(c.name)",
            (*ids, *visibility_sql("c")[1]),
        ).fetchall()
    collections = []
    for row in rows:
        selected_count = row["selected_count"]
        state = "all" if selected_count == len(ids) else "none" if selected_count == 0 else "mixed"
        collections.append({
            "id": row["id"], "name": row["name"], "version": int(row["version"]),
            "state": state, "selected_count": selected_count,
        })
    return jsonify(collections=collections, photo_count=len(ids))


@app.post("/api/collections/membership")
def update_collection_membership():
    data = request.get_json(silent=True) or {}
    items, item_error = requested_photo_items()
    changes = data.get("changes", [])
    if item_error:
        return jsonify(error=item_error), 400
    if not isinstance(changes, list) or len(changes) > 500:
        return jsonify(error="Collection changes are invalid."), 400

    normalized = {}
    for change in changes:
        if not isinstance(change, dict):
            return jsonify(error="Collection changes are invalid."), 400
        collection_id = str(change.get("collection_id", "")).strip()
        action = change.get("action")
        try:
            version = int(change.get("collection_version"))
        except (TypeError, ValueError):
            version = 0
        if not collection_id or action not in ("add", "remove") or version < 1:
            return jsonify(error="Collection changes are invalid."), 400
        normalized[collection_id] = {"action": action, "version": version}
    if not normalized:
        return jsonify(ok=True, added=0, removed=0, changed_collections=0)
    result, error = _apply_collection_membership(
        items, normalized, "collection.membership.update"
    )
    return error or jsonify(**result)


@app.put("/api/collections/<collection_id>/photos/<photo_id>")
def add_photo_to_collection(collection_id, photo_id):
    data = request.get_json(silent=True) or {}
    try:
        photo_version = int(data.get("photo_version"))
        collection_version = int(data.get("collection_version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload the media library before continuing."), 400
    result, error = _apply_collection_membership(
        [(photo_id, photo_version)],
        {collection_id: {"action": "add", "version": collection_version}},
        "collection.photo_membership.put",
    )
    return error or jsonify(**result)


@app.post("/api/collections/<collection_id>/photos")
def add_photos_to_collection(collection_id):
    data = request.get_json(silent=True) or {}
    items, item_error = requested_photo_items()
    if item_error:
        return jsonify(error=item_error), 400
    try:
        collection_version = int(data.get("collection_version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload the collection before continuing."), 400
    result, error = _apply_collection_membership(
        items,
        {collection_id: {"action": "add", "version": collection_version}},
        "collection.photo_membership.add",
    )
    return error or jsonify(**result)


@app.delete("/api/collections/<collection_id>/photos/<photo_id>")
def remove_photo_from_collection(collection_id, photo_id):
    data = request.get_json(silent=True) or {}
    try:
        photo_version = int(data.get("photo_version"))
        collection_version = int(data.get("collection_version"))
    except (TypeError, ValueError):
        return jsonify(error="Reload the media library before continuing."), 400
    result, error = _apply_collection_membership(
        [(photo_id, photo_version)],
        {collection_id: {"action": "remove", "version": collection_version}},
        "collection.photo_membership.remove",
    )
    return error or jsonify(**result)


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

    actor = current_device()
    actor_policy = actor_for_identity(actor)
    if not actor_policy.principal_id or actor_policy.role not in {"admin", "household"}:
        return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
    try:
        visibility = requested_visibility(
            request.form.get("visibility", "shared"), actor_policy
        )
    except ValueError as error:
        return jsonify(error=str(error)), 400
    collection_id = request.form.get("collection_id", "").strip()
    collection = None
    if collection_id:
        with db() as connection:
            collection = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,),
            ).fetchone()
        if not collection or collection["deleted_at"] or not row_is_visible(collection, actor_policy):
            return jsonify(error="That collection could not be found."), 404
        if collection["owner_id"] != actor_policy.principal_id:
            return jsonify(
                error="Add the media to your library first, then contribute it to this shared collection."
            ), 403
        if collection["visibility"] == "private":
            visibility = "private"
    added, duplicates, errors = [], [], []
    for item in files:
        original_name = secure_filename(item.filename or "media") or "media"
        extension = Path(original_name).suffix.lower()
        if extension not in ALLOWED:
            errors.append({"name": original_name, "reason": "Unsupported photo or video format"})
            continue

        photo_id = uuid.uuid4().hex
        temp_name = safe_component(f"{photo_id}.part")
        temp_path = INCOMING / temp_name
        created_temp_identity = None
        try:
            temp_descriptor, temp_metadata = INCOMING_STORAGE.create_regular(
                temp_name
            )
            created_temp_identity = storage_identity(temp_metadata)
            with os.fdopen(temp_descriptor, "wb") as target:
                item.save(target)
                target.flush()
                os.fsync(target.fileno())
            _relative, verified_descriptor, verified_metadata = _open_staged_media(
                temp_path
            )
            try:
                if storage_identity(verified_metadata) != created_temp_identity:
                    raise StorageSafetyError("The staged upload identity changed")
                uploaded_size = int(verified_metadata.st_size)
                checksum = descriptor_sha256(verified_descriptor)
            finally:
                os.close(verified_descriptor)
            if uploaded_size <= 0:
                raise ValueError("Empty media files cannot be uploaded.")
            if uploaded_size > MAX_MEDIA_FILE_BYTES:
                raise ValueError("This media file exceeds the 2 GB file limit.")
            free_space = shutil.disk_usage(DATA).free
            required_space = uploaded_size * (2 if extension in VIDEO_ALLOWED else 1) + 512 * 1024 * 1024
            if free_space < required_space:
                raise ValueError("David-Pi does not have enough free space for this file and its preview.")
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
                INCOMING_STORAGE.unlink_if_identity(
                    temp_name, created_temp_identity
                )
                continue
            result = canonical_ingest_media(
                staged_path=temp_path,
                original_filename=original_name,
                mime_type=item.mimetype or "application/octet-stream",
                owner_user_id=actor_policy.principal_id,
                owner_name=actor["name"],
                visibility=visibility,
                ingestion_source="manual_upload",
                authoritative_sha256=checksum,
                authoritative_size=uploaded_size,
                collection_id=collection_id or None,
                expected_collection_owner_id=(
                    collection["owner_id"] if collection is not None else None
                ),
                expected_collection_visibility=(
                    collection["visibility"] if collection is not None else None
                ),
                expected_collection_version=(
                    int(collection["version"])
                    if collection is not None else None
                ),
            )
            added.append({"id": result["id"], "name": original_name})
        except (UnidentifiedImageError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            if created_temp_identity is not None:
                try:
                    INCOMING_STORAGE.unlink_if_identity(
                        temp_name, created_temp_identity
                    )
                except (OSError, StorageSafetyError):
                    pass
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
    return serve_media_descriptor(
        THUMB_STORAGE, row["thumb_name"], mimetype="image/jpeg", max_age=86400,
        cache_scope=row["visibility"], validator_version=row["version"],
    )


@app.get("/media/preview/<photo_id>")
def preview(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return serve_media_descriptor(
        PREVIEW_STORAGE, row["preview_name"], mimetype="image/jpeg", max_age=86400,
        cache_scope=row["visibility"], validator_version=row["version"],
    )


@app.get("/media/detail/<photo_id>")
def photo_detail(photo_id):
    """Serve the ingestion-time 2200px bounded derivative for still images."""
    row = photo_row(photo_id)
    if not row or row["content_type"].startswith("video/"):
        return "Not found", 404
    return _serve_bounded_detail_image(row)


def _serve_bounded_detail_image(row):
    descriptor = -1
    try:
        descriptor, metadata = PREVIEW_STORAGE.open_regular_path(row["preview_name"])
        if (
            metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > MAX_INLINE_FULL_IMAGE_BYTES
        ):
            raise StorageSafetyError("Detail derivative inode is invalid")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with Image.open(source) as image:
                if (
                    image.format != "JPEG"
                    or image.width <= 0
                    or image.height <= 0
                    or max(image.width, image.height) > 2200
                    or image.width * image.height > MAX_IMAGE_PIXELS
                ):
                    raise ValueError("Detail derivative exceeds its image contract")
                image.verify()
        validator = (
            f"{metadata.st_dev:x}-{metadata.st_ino:x}-{metadata.st_size:x}-"
            f"{metadata.st_mtime_ns:x}-v{int(row['version']):x}-detail"
        )
        response = send_file(
            f"/proc/self/fd/{descriptor}",
            mimetype="image/jpeg",
            as_attachment=False,
            conditional=True,
            max_age=0,
            etag=validator,
        )
        if row["visibility"] in {"shared", "private"}:
            response.headers["X-David-Pi-Media-Cache-Scope"] = row["visibility"]
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except (
        OSError,
        ValueError,
        StorageSafetyError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        return "Not found", 404
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _serve_inline_full_image(row):
    """Validate and serve the exact bounded original image inode inline."""
    declared_types = {
        "image/jpeg": "JPEG",
        "image/png": "PNG",
        "image/webp": "WEBP",
    }
    content_type = str(row["content_type"] or "").lower()
    expected_format = declared_types.get(content_type)
    expected_size = int(row["byte_size"] or 0)
    if (
        expected_format is None
        or expected_size <= 0
        or expected_size > MAX_INLINE_FULL_IMAGE_BYTES
    ):
        return "Not found", 404
    descriptor = -1
    try:
        descriptor, metadata = ORIGINAL_STORAGE.open_regular_path(
            row["stored_path"], expected_size=expected_size,
        )
        if metadata.st_nlink != 1 or metadata.st_size > MAX_INLINE_FULL_IMAGE_BYTES:
            raise StorageSafetyError("Inline image inode is invalid")
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with Image.open(source) as image:
                if image.format != expected_format:
                    raise ValueError("Image signature does not match its media type")
                if (
                    image.width <= 0
                    or image.height <= 0
                    or image.width * image.height > MAX_IMAGE_PIXELS
                ):
                    raise ValueError("Image dimensions exceed the inline limit")
                # verify() walks the encoded structure without retaining a
                # decoded full-resolution pixel buffer in the portal worker.
                image.verify()
        validator = (
            f"{metadata.st_dev:x}-{metadata.st_ino:x}-{metadata.st_size:x}-"
            f"{metadata.st_mtime_ns:x}-v{int(row['version']):x}-inline"
        )
        response = send_file(
            f"/proc/self/fd/{descriptor}",
            mimetype=content_type,
            as_attachment=False,
            conditional=True,
            max_age=0,
            etag=validator,
        )
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except (
        OSError,
        ValueError,
        StorageSafetyError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        return "Not found", 404
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@app.get("/media/full/<photo_id>")
def photo_full(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return _serve_inline_full_image(row)


@app.get("/media/view/<photo_id>")
def viewer_preview(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    try:
        viewer_name = viewer_preview_name(row["preview_name"])
        ensure_viewer_preview(row["preview_name"])
        response = serve_media_descriptor(
            VIEWER_PREVIEW_STORAGE,
            viewer_name,
            mimetype="image/webp",
            max_age=2592000,
            cache_scope=row["visibility"],
            validator_version=row["version"],
        )
        if not isinstance(response, tuple):
            return response
        raise FileNotFoundError("The viewer derivative is unavailable")
    except (OSError, ValueError, UnidentifiedImageError):
        response = serve_media_descriptor(
            PREVIEW_STORAGE, row["preview_name"], mimetype="image/jpeg", max_age=0,
            cache_scope=row["visibility"], validator_version=row["version"],
        )
        if isinstance(response, tuple):
            return response
        response.headers["X-David-Pi-Preview-Fallback"] = "1"
        return response


@app.get("/media/original/<photo_id>")
def original(photo_id):
    row = photo_row(photo_id)
    if not row:
        return "Not found", 404
    return serve_media_descriptor(
        ORIGINAL_STORAGE,
        row["stored_path"],
        mimetype=row["content_type"],
        expected_size=row["byte_size"],
        as_attachment=True,
        download_name=row["original_name"],
        cache_scope=row["visibility"],
        validator_version=row["version"],
    )


@app.get("/media/play/<photo_id>")
def play(photo_id):
    row = photo_row(photo_id)
    if not row or not row["content_type"].startswith("video/"):
        return "Not found", 404
    if row["playback_name"]:
        return serve_media_descriptor(
            PREVIEW_STORAGE, row["playback_name"], mimetype="video/mp4",
            cache_scope=row["visibility"], validator_version=row["version"],
        )
    return serve_media_descriptor(
        ORIGINAL_STORAGE,
        row["stored_path"],
        mimetype=row["content_type"],
        expected_size=row["byte_size"],
        cache_scope=row["visibility"],
        validator_version=row["version"],
    )


@app.get("/media/deleted/<kind>/<photo_id>")
def deleted_media(kind, photo_id):
    if kind not in {
        "thumb", "preview", "detail", "view", "full", "original", "play",
    }:
        return "Not found", 404
    row = photo_row(photo_id, include_deleted=True)
    if not row or row["deleted_at"] is None:
        return "Not found", 404
    if kind == "thumb":
        return serve_media_descriptor(
            THUMB_STORAGE, row["thumb_name"], mimetype="image/jpeg", max_age=0
        )
    if kind == "preview":
        return serve_media_descriptor(
            PREVIEW_STORAGE, row["preview_name"], mimetype="image/jpeg", max_age=0
        )
    if kind == "detail":
        if row["content_type"].startswith("video/"):
            return "Not found", 404
        return _serve_bounded_detail_image(row)
    if kind == "view":
        try:
            viewer_name = viewer_preview_name(row["preview_name"])
            ensure_viewer_preview(row["preview_name"])
            response = serve_media_descriptor(
                VIEWER_PREVIEW_STORAGE,
                viewer_name,
                mimetype="image/webp",
                max_age=0,
            )
            if not isinstance(response, tuple):
                return response
            raise FileNotFoundError("The viewer derivative is unavailable")
        except (OSError, ValueError, UnidentifiedImageError):
            return serve_media_descriptor(
                PREVIEW_STORAGE,
                row["preview_name"],
                mimetype="image/jpeg",
                max_age=0,
            )
    if kind == "full":
        return _serve_inline_full_image(row)
    if kind == "original":
        return serve_media_descriptor(
            ORIGINAL_STORAGE,
            row["stored_path"],
            mimetype=row["content_type"],
            expected_size=row["byte_size"],
            as_attachment=True,
            download_name=row["original_name"],
            max_age=0,
        )
    if not row["content_type"].startswith("video/"):
        return "Not found", 404
    if row["playback_name"]:
        return serve_media_descriptor(
            PREVIEW_STORAGE,
            row["playback_name"],
            mimetype="video/mp4",
            max_age=0,
        )
    return serve_media_descriptor(
        ORIGINAL_STORAGE,
        row["stored_path"],
        mimetype=row["content_type"],
        expected_size=row["byte_size"],
        max_age=0,
    )


@app.get("/manifest.webmanifest")
def manifest():
    manifest_data = json.loads((Path(app.static_folder) / "manifest.webmanifest").read_text())
    manifest_data["name"] = display_name()
    manifest_data["short_name"] = display_name()[:24]
    manifest_data["description"] = f"Your private {display_name()} home server"
    manifest_data["shortcuts"] = [item for item in manifest_data.get("shortcuts", []) if any(
        item.get("url", "").startswith(spec["path"]) and module_enabled(name)
        for name, spec in __import__("modules.installation", fromlist=["MODULES"]).MODULES.items()
    )]
    response = jsonify(manifest_data)
    response.mimetype = "application/manifest+json"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
