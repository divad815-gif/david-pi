"""Owner-bound phone backup APIs integrated with the canonical media library."""

from __future__ import annotations

import hashlib
import hmac
import base64
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from flask import Blueprint, current_app, jsonify, render_template, request, send_file
from PIL import UnidentifiedImageError
import qrcode

from .identity import current_device


TOKEN_TTL_SECONDS = 600
MAX_CHUNK_BYTES = int(os.environ.get("DAVID_PI_BACKUP_CHUNK_MAX", 8 * 1024 * 1024))
IOS_MAX_FILE_BYTES = int(os.environ.get("DAVID_PI_IOS_BACKUP_FILE_MAX", 8 * 1024 * 1024 * 1024))
MIN_FREE_BYTES = int(os.environ.get("DAVID_PI_BACKUP_MIN_FREE", 1024 * 1024 * 1024))
PART_RETENTION_SECONDS = int(os.environ.get("DAVID_PI_BACKUP_PART_RETENTION", 14 * 86400))
PRIMARY_SENTINEL = Path(os.environ.get("DAVID_PI_DATA_SENTINEL", "/data/.david-pi-storage"))
PRIMARY_SENTINEL_ID = os.environ.get("DAVID_PI_DATA_ID", "david-pi-family-storage-v1")
SECONDARY_ROOT = os.environ.get("DAVID_PI_SECONDARY_BACKUP_ROOT", "").strip()
SECONDARY_SENTINEL_ID = os.environ.get("DAVID_PI_SECONDARY_DATA_ID", "")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._() -]+")


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
            consumed_at TEXT
        );
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
            local_source_visible INTEGER NOT NULL DEFAULT 1,
            ingested_at TEXT NOT NULL,
            UNIQUE(device_id, client_item_id)
        );
        CREATE INDEX IF NOT EXISTS device_media_owner_idx
            ON device_media_records(owner_user_id, ingested_at DESC);
        CREATE INDEX IF NOT EXISTS device_media_hash_idx
            ON device_media_records(content_sha256);
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
        """
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


def bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def authenticate_device(connection: sqlite3.Connection):
    credential = bearer_token()
    if len(credential) < 32:
        return None
    digest = token_hash(credential)
    row = connection.execute(
        "SELECT * FROM backup_devices WHERE credential_hash=? AND revoked_at IS NULL",
        (digest,),
    ).fetchone()
    if row:
        connection.execute(
            "UPDATE backup_devices SET last_contact_at=? WHERE id=?", (utcnow(), row["id"])
        )
    return row


def error(code: str, message: str, status: int, retry_after: int | None = None):
    response = jsonify(error={"code": code, "message": message})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def init_device_backup(
    app, db_context, canonical_ingest, rollback_ingest, data_root: Path
) -> None:
    blueprint = Blueprint("device_backup", __name__)
    incoming_root = data_root / "incoming" / "device-backup"
    apk_root = Path(app.root_path) / "static" / "apk"

    with db_context() as connection:
        initialize_device_backup(connection)

    def cleanup_stale_parts() -> int:
        cutoff = time.time() - PART_RETENTION_SECONDS
        removed = 0
        if not incoming_root.is_dir():
            return removed
        for path in incoming_root.glob("*/*.part"):
            try:
                if (
                    path.is_file() and not path.is_symlink()
                    and path.stat().st_mtime < cutoff
                ):
                    with db_context() as connection:
                        active = connection.execute(
                            """SELECT 1 FROM device_uploads
                               WHERE part_path=? AND state='uploading' AND updated_at>?""",
                            (
                                str(path),
                                datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
                            ),
                        ).fetchone()
                    if not active:
                        path.unlink()
                        removed += 1
            except OSError:
                continue
        return removed

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

    def secondary_verify_one() -> bool:
        if not SECONDARY_ROOT or not SECONDARY_SENTINEL_ID:
            return False
        root = Path(SECONDARY_ROOT)
        sentinel = root / ".david-pi-secondary-storage"
        try:
            if sentinel.read_text(encoding="utf-8").strip() != SECONDARY_SENTINEL_ID:
                return False
            if not root.resolve().is_dir():
                return False
        except OSError:
            return False
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT d.id,d.content_sha256,p.stored_path
                   FROM device_media_records d JOIN photos p ON p.id=d.media_id
                   WHERE d.secondary_verification_state='secondary_pending'
                   ORDER BY d.ingested_at LIMIT 1"""
            ).fetchone()
            if not row:
                return False
            connection.execute(
                """UPDATE device_media_records SET secondary_verification_state='secondary_copying'
                   WHERE content_sha256=? AND secondary_verification_state='secondary_pending'""",
                (row["content_sha256"],),
            )
        source = (data_root / "originals" / row["stored_path"]).resolve()
        originals = (data_root / "originals").resolve()
        destination = (root / "originals" / row["stored_path"]).resolve()
        if not source.is_relative_to(originals) or source.is_symlink() or not source.is_file():
            state = "secondary_error"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".part")
            digest = hashlib.sha256()
            try:
                with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                    for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                        outgoing.write(chunk)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                with temporary.open("rb") as copied:
                    for chunk in iter(lambda: copied.read(1024 * 1024), b""):
                        digest.update(chunk)
                if hmac.compare_digest(digest.hexdigest(), row["content_sha256"]):
                    os.replace(temporary, destination)
                    state = "fully_protected"
                else:
                    temporary.unlink(missing_ok=True)
                    state = "secondary_hash_mismatch"
            except OSError:
                temporary.unlink(missing_ok=True)
                state = "secondary_error"
        with db_context() as connection:
            connection.execute(
                """UPDATE device_media_records SET secondary_verification_state=?
                   WHERE content_sha256=? AND secondary_verification_state='secondary_copying'""",
                (state, row["content_sha256"]),
            )
            connection.execute(
                """UPDATE photos SET secondary_verification_state=?
                   WHERE COALESCE(content_sha256,sha256)=?""",
                (state, row["content_sha256"]),
            )
        return state == "fully_protected"

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
        return actor if actor["verified"] and actor["owner_id"] else None

    @blueprint.get("/device-backup")
    def setup_page():
        actor = require_portal_identity()
        if not actor:
            return "Open this page through private Tailscale HTTPS.", 403
        with db_context() as connection:
            devices = connection.execute(
                """SELECT d.id,d.display_name,d.platform,d.created_at,d.last_contact_at,
                          d.last_reconciliation_at,d.revoked_at,d.uploaded_items,d.uploaded_bytes,
                          SUM(CASE WHEN u.state='uploading' THEN 1 ELSE 0 END) AS active_uploads,
                          SUM(CASE WHEN u.state IN ('queued','retryable_error') THEN 1 ELSE 0 END)
                              AS pending_uploads,
                          SUM(CASE WHEN u.state IN ('retryable_error','permanent_error') THEN 1 ELSE 0 END)
                              AS failed_uploads
                   FROM backup_devices d LEFT JOIN device_uploads u ON u.device_id=d.id
                   WHERE d.owner_user_id=? AND d.revoked_at IS NULL GROUP BY d.id
                   ORDER BY d.created_at DESC""",
                (actor["owner_id"],),
            ).fetchall()
        apk = apk_root / "david-pi-backup.apk"
        checksum = None
        build_date = None
        if apk.is_file():
            checksum = hashlib.sha256(apk.read_bytes()).hexdigest()
            build_date = datetime.fromtimestamp(
                apk.stat().st_mtime, timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
        return render_template(
            "device_backup.html", devices=[dict(row) for row in devices],
            apk_available=apk.is_file(), apk_sha256=checksum,
            apk_version="1.1.4-chat-lifecycle", apk_build_date=build_date,
            ios_shortcut_icloud_url=configured_ios_shortcut_url(),
            public_server_url=os.environ.get(
                "DAVID_PI_PUBLIC_URL", "https://localhost"
            ).rstrip("/"),
        )

    @blueprint.post("/api/device-backup/pairing-token")
    def create_pairing_token():
        actor = require_portal_identity()
        if not actor:
            return error("tailscale_identity_required", "Open through private Tailscale HTTPS.", 403)
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
        server_url = f"https://{request.host.split(':', 1)[0]}"
        deep_link = (
            f"davidpibackup://pair?token={quote(token)}"
            f"&server={quote(server_url, safe='')}"
        )
        qr_image = qrcode.make(deep_link)
        qr_bytes = io.BytesIO()
        qr_image.save(qr_bytes, format="PNG")
        return jsonify(
            pairing_token=token, manual_code=manual,
            expires_at=(now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(),
            deep_link=deep_link,
            qr_data_uri=(
                "data:image/png;base64,"
                + base64.b64encode(qr_bytes.getvalue()).decode("ascii")
            ),
        )

    @blueprint.post("/api/ios-backup/credential-file")
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

    @blueprint.post("/api/ios-backup/shortcut")
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
    @blueprint.post("/api/v1/ios-backup/pair")
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
        now = utcnow()
        credential = secrets.token_urlsafe(48)
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            digest = token_hash(supplied)
            row = connection.execute(
                """SELECT * FROM device_pairing_tokens
                   WHERE (token_hash=? OR manual_code_hash=?)
                     AND consumed_at IS NULL AND expires_at>?""",
                (digest, digest, now),
            ).fetchone()
            if not row:
                return error("pairing_invalid", "That pairing code is invalid or expired.", 401)
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
        return jsonify(
            device_id=device_id, device_credential=credential,
            owner_name=row["owner_name"], api_version=1,
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
        dedupe_device = None
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device:
                return error("device_unauthorized", "This phone is not paired.", 401)
            existing_item = connection.execute(
                """SELECT media_id FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (device["id"], client_item_id),
            ).fetchone()
            if existing_item:
                return jsonify(state="already_present", media_id=existing_item["media_id"])
            session = connection.execute(
                "SELECT * FROM device_uploads WHERE device_id=? AND client_item_id=?",
                (device["id"], client_item_id),
            ).fetchone()
            if session and session["state"] in ("uploading", "queued"):
                part = Path(session["part_path"])
                offset = part.stat().st_size if part.is_file() and not part.is_symlink() else 0
                connection.execute(
                    "UPDATE device_uploads SET accepted_offset=?,updated_at=? WHERE id=?",
                    (offset, utcnow(), session["id"]),
                )
                return jsonify(upload_id=session["id"], state="uploading", offset=offset)
            # Physical dedupe does not reveal another owner's item; it only avoids retransmission.
            physical = connection.execute(
                """SELECT id FROM photos
                   WHERE COALESCE(content_sha256, sha256)=? AND byte_size=?
                   ORDER BY uploaded_at LIMIT 1""",
                (expected_hash, expected_size),
            ).fetchone()
            upload_id = str(uuid.uuid4())
            now = utcnow()
            if physical:
                dedupe_device = dict(device)
            else:
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
                device_root.mkdir(parents=True, exist_ok=True)
                part = device_root / f"{upload_id}.part"
                part.touch(exist_ok=False)
                connection.execute(
                    """INSERT INTO device_uploads
                       (id,device_id,client_item_id,original_filename,expected_size,expected_sha256,
                        mime_type,capture_timestamp,accepted_offset,part_path,state,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        upload_id, device["id"], client_item_id,
                        safe_filename(str(payload.get("original_filename") or "media")),
                        expected_size, expected_hash,
                        str(payload.get("mime_type") or "application/octet-stream")[:120],
                        payload.get("capture_timestamp"), 0, str(part), "uploading", now, now,
                    ),
                )
        if dedupe_device:
            result = canonical_ingest(
                staged_path=None,
                original_filename=safe_filename(str(payload.get("original_filename") or "media")),
                mime_type=str(payload.get("mime_type") or "application/octet-stream")[:120],
                owner_user_id=dedupe_device["owner_user_id"],
                owner_name=dedupe_device["owner_name"],
                visibility="shared",
                capture_timestamp=payload.get("capture_timestamp"),
                source_device_id=dedupe_device["id"],
                ingestion_source="android_backup",
                authoritative_sha256=expected_hash,
                authoritative_size=expected_size,
            )
            try:
                with db_context() as connection:
                    connection.execute(
                        """INSERT INTO device_media_records
                           (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                            content_sha256,byte_size,capture_timestamp,primary_verification_state,
                            secondary_verification_state,ingested_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            uuid.uuid4().hex, dedupe_device["id"], client_item_id, result["id"],
                            dedupe_device["owner_user_id"], safe_filename(payload.get("original_filename") or "media"),
                            expected_hash, expected_size, payload.get("capture_timestamp"),
                            "primary_verified", "secondary_pending", now,
                        ),
                    )
            except Exception:
                rollback_ingest(result["id"])
                raise
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

    @blueprint.route("/api/v1/device-backup/uploads/<upload_id>", methods=["HEAD"])
    def upload_offset(upload_id):
        with db_context() as connection:
            _, upload = owned_upload(connection, upload_id)
            if not upload:
                return "", 404
            if upload["state"] == "primary_verified":
                # Completion is durable even if the phone missed the response. The
                # staging path is intentionally cleared after ingestion, so report
                # the verified length rather than attempting to inspect that path.
                offset = upload["expected_size"]
            elif upload["state"] == "uploading" and upload["part_path"]:
                part = Path(upload["part_path"])
                offset = part.stat().st_size if part.is_file() and not part.is_symlink() else 0
            elif upload["state"] == "permanent_error":
                response = current_app.response_class(status=422)
                response.headers["Upload-Error-Code"] = upload["error_code"] or "invalid_media"
                response.headers["Upload-State"] = upload["state"]
                return response
            else:
                return "", 409
        response = current_app.response_class(status=204)
        response.headers["Upload-Offset"] = str(offset)
        response.headers["Upload-Length"] = str(upload["expected_size"])
        response.headers["Upload-State"] = upload["state"]
        return response

    @blueprint.patch("/api/v1/device-backup/uploads/<upload_id>")
    def append_upload(upload_id):
        try:
            supplied_offset = int(request.headers.get("Upload-Offset", "-1"))
        except ValueError:
            return error("offset_required", "Upload-Offset is required.", 400)
        content_length = request.content_length
        if content_length is None or content_length < 1 or content_length > MAX_CHUNK_BYTES:
            return error("chunk_size_invalid", "Use a non-empty chunk no larger than 8 MiB.", 413)
        with db_context() as connection:
            connection.execute("BEGIN IMMEDIATE")
            device, upload = owned_upload(connection, upload_id)
            if not upload or upload["state"] != "uploading":
                return error("upload_not_found", "This upload is not active.", 404)
            part = Path(upload["part_path"])
            expected_root = (incoming_root / device["id"]).resolve()
            try:
                resolved = part.resolve()
                if part.is_symlink() or not resolved.is_relative_to(expected_root):
                    raise ValueError
            except (OSError, ValueError):
                return error("unsafe_upload_path", "The upload staging path is invalid.", 500)
            current_offset = part.stat().st_size
            if supplied_offset != current_offset:
                response = error("offset_mismatch", "Resume from the server offset.", 409)
                response.headers["Upload-Offset"] = str(current_offset)
                return response
            if current_offset + content_length > upload["expected_size"]:
                return error("upload_too_long", "This chunk exceeds the expected file size.", 409)
            written = 0
            with part.open("ab", buffering=0) as stream:
                while written < content_length:
                    chunk = request.stream.read(min(1024 * 1024, content_length - written))
                    if not chunk:
                        break
                    stream.write(chunk)
                    written += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if written != content_length:
                return error("chunk_incomplete", "The full chunk was not received.", 400)
            new_offset = current_offset + written
            connection.execute(
                "UPDATE device_uploads SET accepted_offset=?,updated_at=? WHERE id=?",
                (new_offset, utcnow(), upload_id),
            )
        response = current_app.response_class(status=204)
        response.headers["Upload-Offset"] = str(new_offset)
        return response

    @blueprint.post("/api/v1/device-backup/uploads/<upload_id>/complete")
    def complete_upload(upload_id):
        upload_data = None
        device_data = None
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
            if upload["state"] != "uploading":
                return error("upload_not_found", "This upload is not active.", 404)
            if not primary_storage_ready(data_root):
                return error("primary_storage_unavailable", "Primary storage is unavailable.", 503, 60)
            part = Path(upload["part_path"])
            if not part.is_file() or part.is_symlink() or part.stat().st_size != upload["expected_size"]:
                return error("size_mismatch", "The uploaded size does not match.", 409)
            digest = hashlib.sha256()
            with part.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual_hash = digest.hexdigest()
            if not hmac.compare_digest(actual_hash, upload["expected_sha256"]):
                connection.execute(
                    "UPDATE device_uploads SET state='retryable_error',error_code=?,updated_at=? WHERE id=?",
                    ("sha256_mismatch", utcnow(), upload_id),
                )
                return error("sha256_mismatch", "The received file did not verify.", 422)
            upload_data, device_data = dict(upload), dict(device)
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
            )
            now = utcnow()
            with db_context() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO device_media_records
                       (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                        content_sha256,byte_size,capture_timestamp,primary_verification_state,
                        secondary_verification_state,ingested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex, device_data["id"], upload_data["client_item_id"], result["id"],
                        device_data["owner_user_id"], upload_data["original_filename"], actual_hash,
                        upload_data["expected_size"], upload_data["capture_timestamp"], "primary_verified",
                        "secondary_pending" if not SECONDARY_ROOT else "secondary_pending", now,
                    ),
                )
                connection.execute(
                    """UPDATE device_uploads SET state='primary_verified',media_id=?,
                       accepted_offset=expected_size,updated_at=?,part_path=NULL WHERE id=?""",
                    (result["id"], now, upload_id),
                )
                connection.execute(
                    """UPDATE backup_devices SET uploaded_items=uploaded_items+1,
                       uploaded_bytes=uploaded_bytes+?,last_contact_at=? WHERE id=?""",
                    (upload_data["expected_size"], now, device_data["id"]),
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
                connection.execute(
                    """UPDATE device_uploads
                       SET state='permanent_error',error_code=?,updated_at=?,part_path=NULL
                       WHERE id=?""",
                    ("invalid_media", utcnow(), upload_id),
                )
            return error(
                "invalid_media",
                "Android returned an unreadable media item. It was skipped safely.",
                422,
            )
        except Exception:
            if "result" in locals() and result.get("id"):
                try:
                    rollback_ingest(result["id"])
                except Exception:
                    current_app.logger.exception(
                        "Device backup compensation failed [%s]", upload_id
                    )
            current_app.logger.exception("Device backup ingestion failed [%s]", upload_id)
            return error("ingestion_failed", "The file verified but could not be added safely.", 500)
        return jsonify(
            state="primary_verified", media_id=result["id"],
            primary_verification_state="primary_verified",
            secondary_verification_state="secondary_pending",
        )

    @blueprint.get("/api/v1/device-backup/status")
    @blueprint.get("/api/v1/ios-backup/status")
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
            uploads=counts, protection=protection,
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
            upload_endpoint="/api/v1/ios-backup/upload-file",
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
        device_root.mkdir(parents=True, exist_ok=True)
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
            )
            now = utcnow()
            try:
                with db_context() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """INSERT INTO device_media_records
                           (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                            content_sha256,byte_size,capture_timestamp,ingestion_source,
                            primary_verification_state,secondary_verification_state,ingested_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            uuid.uuid4().hex, device_data["id"], client_item_id, result["id"],
                            device_data["owner_user_id"], original_name, content_hash, byte_size,
                            capture_timestamp or None, "ios_shortcut_backup", "primary_verified",
                            "secondary_pending", now,
                        ),
                    )
                    connection.execute(
                        """UPDATE backup_devices SET uploaded_items=uploaded_items+1,
                           uploaded_bytes=uploaded_bytes+?,last_contact_at=? WHERE id=?""",
                        (byte_size, now, device_data["id"]),
                    )
                    cursor = advance_ios_cursor(
                        connection, device_data["id"], capture_timestamp, now
                    )
            except Exception:
                rollback_ingest(result)
                raise
            return jsonify(
                state="primary_verified", media_id=result["id"], byte_size=byte_size,
                content_sha256=content_hash, capture_cursor=cursor,
            ), 201
        except ValueError as exc:
            part.unlink(missing_ok=True)
            if str(exc) == "file_too_large":
                return error("file_too_large", "That item exceeds the iPhone backup limit.", 413)
            return error("invalid_media", "That item could not be backed up.", 422)
        except (OSError, UnidentifiedImageError):
            part.unlink(missing_ok=True)
            current_app.logger.exception("iPhone backup upload failed")
            return error("upload_failed", "That item could not be backed up safely.", 500)
        except Exception:
            part.unlink(missing_ok=True)
            current_app.logger.exception("iPhone backup ingestion failed")
            return error("ingestion_failed", "David-Pi could not finish that backup item.", 500)

    @blueprint.post("/api/v1/ios-backup/upload")
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

    @blueprint.post("/api/v1/ios-backup/upload-file")
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

    @blueprint.post("/api/v1/ios-backup/checkpoint")
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
        payload = request.get_json(silent=True) or {}
        visible_ids = payload.get("visible_client_item_ids")
        with db_context() as connection:
            device = authenticate_device(connection)
            if not device:
                return error("device_unauthorized", "This phone is not paired.", 401)
            now = utcnow()
            if isinstance(visible_ids, list) and len(visible_ids) <= 5000:
                connection.execute(
                    "UPDATE device_media_records SET local_source_visible=0 WHERE device_id=?",
                    (device["id"],),
                )
                for item_id in visible_ids:
                    connection.execute(
                        """UPDATE device_media_records SET local_source_visible=1
                           WHERE device_id=? AND client_item_id=?""",
                        (device["id"], str(item_id)[:200]),
                    )
            connection.execute(
                "UPDATE backup_devices SET last_reconciliation_at=?,last_contact_at=? WHERE id=?",
                (now, now, device["id"]),
            )
        return jsonify(ok=True, reconciled_at=now)

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
        apk = apk_root / "david-pi-backup.apk"
        if not actor or not apk.is_file():
            return "Not found", 404
        return send_file(apk, as_attachment=True, download_name="David-Pi.apk")

    app.register_blueprint(blueprint)
