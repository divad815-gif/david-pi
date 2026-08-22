"""Private household chat using canonical Tailscale identities.

Messages and chat-only attachments are encrypted at rest.  Authorization is
always derived from the verified request identity; client-supplied owners are
never accepted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import secrets
import sqlite3
import tempfile
import uuid
from urllib.parse import urlencode, urlparse
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .identity import current_device
from .platform import PLATFORM_DATA, connect, migrate, utcnow
from .recipes import fetch_chain


DB_PATH = PLATFORM_DATA / "chat.db"
CHAT_ROOT = Path(os.environ.get("DAVID_PI_CHAT_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "chat"))
KEY_FILE = Path(os.environ.get("DAVID_PI_CHAT_KEY_FILE", "/run/secrets/chat-master.key"))
MAX_MESSAGE = 10_000
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT = 25 * 1024 * 1024
MAX_BATCH = 100 * 1024 * 1024
ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}


def _migrate(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS portal_users (
          owner_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
          first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('direct','group')),
          title_cipher BLOB, title_nonce BLOB, direct_key TEXT UNIQUE,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_members (
          conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL, joined_at TEXT NOT NULL,
          PRIMARY KEY(conversation_id, owner_id),
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
          body_cipher BLOB, body_nonce BLOB, client_message_id TEXT,
          created_at TEXT NOT NULL, updated_at TEXT, deleted_at TEXT,
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
          UNIQUE(conversation_id, sender_id, client_message_id)
        );
        CREATE INDEX IF NOT EXISTS messages_conversation_cursor
          ON messages(conversation_id, id DESC);
        CREATE TABLE IF NOT EXISTS chat_attachments (
          id TEXT PRIMARY KEY, message_id INTEGER NOT NULL, conversation_id TEXT NOT NULL,
          object_path TEXT NOT NULL, preview_path TEXT,
          mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
          width INTEGER, height INTEGER, sha256 TEXT NOT NULL,
          original_name_cipher BLOB, original_name_nonce BLOB, created_at TEXT NOT NULL,
          FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS chat_attachments_message ON chat_attachments(message_id);
        CREATE TABLE IF NOT EXISTS conversation_reads (
          conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
          last_message_id INTEGER NOT NULL DEFAULT 0, seen_at TEXT NOT NULL,
          PRIMARY KEY(conversation_id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, platform TEXT NOT NULL,
          endpoint TEXT, p256dh TEXT, auth TEXT, device_token TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(owner_id, platform, endpoint), UNIQUE(owner_id, platform, device_token)
        );
        CREATE TABLE IF NOT EXISTS notification_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          message_id INTEGER NOT NULL, recipient_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at TEXT NOT NULL, created_at TEXT NOT NULL, last_error_code TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_jobs_pending
          ON notification_jobs(status, available_at, id);
        """
    )


migrate(DB_PATH, _migrate)
try:
    DB_PATH.chmod(0o640)
except OSError:
    pass


def _key():
    encoded = os.environ.get("DAVID_PI_CHAT_KEY_B64", "").strip()
    if encoded:
        raw = base64.b64decode(encoded, validate=True)
    else:
        try:
            raw = KEY_FILE.read_bytes().strip()
            if len(raw) != 32:
                raw = base64.b64decode(raw, validate=True)
        except (OSError, ValueError):
            abort(503, description="Chat encryption is not configured.")
    if len(raw) != 32:
        abort(503, description="Chat encryption is not configured.")
    return raw


def _encrypt(value: bytes):
    nonce = secrets.token_bytes(12)
    return AESGCM(_key()).encrypt(nonce, value, b"david-pi-chat-v1"), nonce


def _write_private(path: Path, value: bytes):
    """Atomically place encrypted chat data without making it world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def _decrypt(cipher, nonce):
    if not cipher:
        return b""
    return AESGCM(_key()).decrypt(bytes(nonce), bytes(cipher), b"david-pi-chat-v1")


def _identity(required=True):
    identity = current_device()
    if required and not identity["verified"]:
        abort(403, description="Open Chat through private Tailscale HTTPS.")
    return identity


def _member(connection, conversation_id, owner_id):
    return connection.execute(
        "SELECT 1 FROM conversation_members WHERE conversation_id=? AND owner_id=?",
        (conversation_id, owner_id),
    ).fetchone() is not None


def _reserved_test_identity(owner_id):
    """Keep test fixtures from ever becoming household identities in production."""
    value = str(owner_id or "").strip().casefold()
    return value.endswith("@example.test") or value.endswith(".example.test")


def _conversation_summary(connection, row, me):
    members = connection.execute(
        """SELECT m.owner_id, COALESCE(u.display_name,m.owner_id) display_name
           FROM conversation_members m LEFT JOIN portal_users u USING(owner_id)
           WHERE m.conversation_id=? ORDER BY lower(display_name)""",
        (row["id"],),
    ).fetchall()
    names = [item["display_name"] for item in members if item["owner_id"] != me]
    title = _decrypt(row["title_cipher"], row["title_nonce"]).decode("utf-8") if row["title_cipher"] else ""
    latest = connection.execute(
        "SELECT id,sender_id,body_cipher,body_nonce,created_at,deleted_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    read = connection.execute(
        "SELECT last_message_id FROM conversation_reads WHERE conversation_id=? AND owner_id=?",
        (row["id"], me),
    ).fetchone()
    unread = connection.execute(
        "SELECT count(*) FROM messages WHERE conversation_id=? AND id>? AND sender_id<>?",
        (row["id"], read["last_message_id"] if read else 0, me),
    ).fetchone()[0]
    preview = "No messages yet"
    if latest:
        preview = "Message deleted" if latest["deleted_at"] else _decrypt(latest["body_cipher"], latest["body_nonce"]).decode("utf-8")[:90]
        if not preview:
            preview = "Photo"
    return {
        "id": row["id"], "kind": row["kind"], "title": title or ", ".join(names) or "Chat",
        "members": [dict(item) for item in members], "preview": preview,
        "last_message_id": latest["id"] if latest else 0,
        "updated_at": row["updated_at"], "unread": unread,
    }


def _message_json(connection, row, me):
    deleted = bool(row["deleted_at"])
    attachments = connection.execute(
        "SELECT id,mime_type,byte_size,width,height FROM chat_attachments WHERE message_id=? ORDER BY created_at,id",
        (row["id"],),
    ).fetchall()
    total_other = connection.execute(
        "SELECT count(*) FROM conversation_members WHERE conversation_id=? AND owner_id<>?",
        (row["conversation_id"], row["sender_id"]),
    ).fetchone()[0]
    seen_other = connection.execute(
        """SELECT count(*) FROM conversation_reads r
           WHERE r.conversation_id=? AND r.owner_id<>? AND r.last_message_id>=?""",
        (row["conversation_id"], row["sender_id"], row["id"]),
    ).fetchone()[0]
    return {
        "id": row["id"], "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"], "sender_name": row["sender_name"],
        "mine": row["sender_id"] == me,
        "body": "" if deleted else _decrypt(row["body_cipher"], row["body_nonce"]).decode("utf-8"),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "deleted": deleted,
        "delivery": "seen" if total_other and seen_other == total_other else "sent",
        "attachments": [dict(item) | {
            "preview_url": f"/api/chat/attachments/{item['id']}/preview",
            "download_url": f"/api/chat/attachments/{item['id']}/original",
        } for item in attachments] if not deleted else [],
    }


def _store_attachment(upload, message_id, conversation_id):
    name = (upload.filename or "photo").strip()[:255]
    data = upload.read(MAX_ATTACHMENT + 1)
    if not data or len(data) > MAX_ATTACHMENT:
        raise ValueError("Each chat photo must be between 1 byte and 25 MiB.")
    declared = (upload.mimetype or mimetypes.guess_type(name)[0] or "").lower()
    if declared not in ALLOWED_IMAGE:
        raise ValueError("Chat accepts JPEG, PNG, WebP, HEIC, and GIF images.")
    try:
        with Image.open(BytesIO(data)) as image:
            image.seek(0)
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 40_000_000:
                raise ValueError("This image is too large to process safely.")
            safe = ImageOps.exif_transpose(image.copy()).convert("RGB")
            safe.thumbnail((1280, 1280))
            preview_buffer = BytesIO()
            safe.save(preview_buffer, "JPEG", quality=82, optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ValueError("The selected file is not a valid supported image.") from error
    attachment_id = uuid.uuid4().hex
    object_dir = CHAT_ROOT / conversation_id[:2] / conversation_id
    object_dir.mkdir(parents=True, exist_ok=True)
    cipher, nonce = _encrypt(data)
    preview_cipher, preview_nonce = _encrypt(preview_buffer.getvalue())
    object_path = object_dir / f"{attachment_id}.bin"
    preview_path = object_dir / f"{attachment_id}.preview"
    _write_private(object_path, nonce + cipher)
    _write_private(preview_path, preview_nonce + preview_cipher)
    name_cipher, name_nonce = _encrypt(name.encode("utf-8", errors="replace"))
    return {
        "id": attachment_id, "message_id": message_id, "conversation_id": conversation_id,
        "object_path": str(object_path.relative_to(CHAT_ROOT)),
        "preview_path": str(preview_path.relative_to(CHAT_ROOT)),
        "mime_type": declared, "byte_size": len(data), "width": width, "height": height,
        "sha256": hashlib.sha256(data).hexdigest(), "original_name_cipher": name_cipher,
        "original_name_nonce": name_nonce, "created_at": utcnow(),
    }


def init_chat(app, canonical_ingest_media):
    blueprint = Blueprint("chat", __name__)

    @app.before_request
    def remember_verified_chat_user():
        identity = current_device()
        if not identity["verified"]:
            return None
        if not current_app.testing and _reserved_test_identity(identity["owner_id"]):
            return None
        now = utcnow()
        with connect(DB_PATH) as connection:
            connection.execute(
                """INSERT INTO portal_users(owner_id,display_name,first_seen_at,last_seen_at)
                   VALUES(?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
                   display_name=excluded.display_name,last_seen_at=excluded.last_seen_at""",
                (identity["owner_id"], identity["name"], now, now),
            )
        return None

    @blueprint.get("/chat")
    @blueprint.get("/chat/<conversation_id>")
    def page(conversation_id=None):
        identity = _identity()
        if conversation_id:
            with connect(DB_PATH) as connection:
                if not _member(connection, conversation_id, identity["owner_id"]):
                    abort(404)
        return render_template("chat.html", conversation_id=conversation_id or "", person=identity)

    @blueprint.get("/api/chat/users")
    def users():
        identity = _identity()
        with connect(DB_PATH) as connection:
            where = "owner_id<>?"
            params = [identity["owner_id"]]
            if not current_app.testing:
                where += " AND owner_id NOT LIKE '%@example.test'"
            rows = connection.execute(
                f"SELECT owner_id,display_name FROM portal_users WHERE {where} ORDER BY lower(display_name)",
                params,
            ).fetchall()
        return jsonify(users=[dict(row) for row in rows])

    @blueprint.get("/api/chat/gifs/search")
    def gif_search():
        _identity()
        key = os.environ.get("GIPHY_API_KEY", "").strip()
        query = request.args.get("q", "").strip()[:100]
        if not key:
            return jsonify(configured=False, results=[])
        if not query:
            return jsonify(configured=True, results=[])
        url = "https://api.giphy.com/v1/gifs/search?" + urlencode(
            {"api_key": key, "q": query, "limit": 18, "rating": "r", "lang": "en"}
        )
        try:
            raw, content_type, final_url = fetch_chain(
                url, "application/json", 2 * 1024 * 1024, timeout=8
            )
            if content_type != "application/json" or urlparse(final_url).hostname != "api.giphy.com":
                raise ValueError("invalid response")
            payload = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify(error="GIF search is temporarily unavailable."), 503
        results = []
        for item in payload.get("data", []):
            images = item.get("images") or {}
            preview = (images.get("fixed_width_small") or images.get("fixed_width") or {}).get("url")
            selected = (images.get("fixed_width") or images.get("downsized") or {}).get("url")
            if preview and selected:
                results.append({"id": str(item.get("id") or "")[:80], "preview": preview, "selected": selected})
        return jsonify(configured=True, results=results)

    @blueprint.get("/api/chat/gifs/fetch")
    def gif_fetch():
        _identity()
        target = request.args.get("url", "")[:2048]
        parsed = urlparse(target)
        if parsed.scheme != "https" or parsed.hostname not in {"media.giphy.com", "i.giphy.com"}:
            abort(422)
        try:
            data, content_type, final_url = fetch_chain(
                target, "image/gif", MAX_ATTACHMENT, timeout=10
            )
            if urlparse(final_url).hostname not in {"media.giphy.com", "i.giphy.com"} or content_type != "image/gif":
                raise ValueError("invalid gif")
            with Image.open(BytesIO(data)) as image:
                if image.format != "GIF" or image.width * image.height > 40_000_000:
                    raise ValueError("invalid gif")
        except (OSError, ValueError, UnidentifiedImageError):
            return jsonify(error="That GIF could not be safely imported."), 422
        return send_file(BytesIO(data), mimetype="image/gif", download_name="giphy.gif", max_age=0)

    @blueprint.get("/api/chat/conversations")
    def conversations():
        identity = _identity()
        with connect(DB_PATH) as connection:
            test_guard = "" if current_app.testing else """
                   AND NOT EXISTS (
                     SELECT 1 FROM conversation_members rejected
                     WHERE rejected.conversation_id=c.id AND rejected.owner_id LIKE '%@example.test'
                   )"""
            rows = connection.execute(
                f"""SELECT c.* FROM conversations c JOIN conversation_members m ON m.conversation_id=c.id
                   WHERE m.owner_id=? {test_guard} ORDER BY c.updated_at DESC LIMIT 100""",
                (identity["owner_id"],),
            ).fetchall()
            result = [_conversation_summary(connection, row, identity["owner_id"]) for row in rows]
        return jsonify(conversations=result)

    @blueprint.post("/api/chat/conversations")
    def create_conversation():
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        requested = payload.get("member_ids") or []
        if not isinstance(requested, list):
            abort(422)
        member_ids = sorted({str(item).strip().casefold()[:320] for item in requested if str(item).strip()} | {identity["owner_id"]})
        if len(member_ids) < 2 or len(member_ids) > 12:
            return jsonify(error="Choose between one and eleven other household members."), 422
        with connect(DB_PATH) as connection:
            known = {row[0] for row in connection.execute(
                f"SELECT owner_id FROM portal_users WHERE owner_id IN ({','.join('?' for _ in member_ids)})", member_ids
            )}
            if known != set(member_ids):
                return jsonify(error="One selected household member is not available."), 422
            kind = "direct" if len(member_ids) == 2 else "group"
            direct_key = "|".join(member_ids) if kind == "direct" else None
            if direct_key:
                existing = connection.execute("SELECT id FROM conversations WHERE direct_key=?", (direct_key,)).fetchone()
                if existing:
                    return jsonify(id=existing["id"], existing=True)
            title = str(payload.get("title") or "").strip()[:120]
            title_cipher, title_nonce = _encrypt(title.encode()) if title else (None, None)
            conversation_id = uuid.uuid4().hex
            now = utcnow()
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
                (conversation_id, kind, title_cipher, title_nonce, direct_key, identity["owner_id"], now, now),
            )
            connection.executemany(
                "INSERT INTO conversation_members VALUES(?,?,?)",
                [(conversation_id, owner_id, now) for owner_id in member_ids],
            )
        return jsonify(id=conversation_id, existing=False), 201

    @blueprint.delete("/api/chat/conversations/<conversation_id>")
    def delete_conversation(conversation_id):
        """Delete one shared conversation and its encrypted attachment objects."""
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        if payload.get("confirmation") != "DELETE CHAT" or payload.get("conversation_id") != conversation_id:
            return jsonify(error="Type DELETE CHAT in the confirmation window before deleting."), 422
        attachment_paths = []
        with connect(DB_PATH) as connection:
            if not _member(connection, conversation_id, identity["owner_id"]):
                abort(404)
            attachments = connection.execute(
                "SELECT object_path,preview_path FROM chat_attachments WHERE conversation_id=?",
                (conversation_id,),
            ).fetchall()
            for attachment in attachments:
                attachment_paths.extend(filter(None, (attachment["object_path"], attachment["preview_path"])))
            connection.execute("DELETE FROM notification_jobs WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversation_reads WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM chat_attachments WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversation_members WHERE conversation_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))

        root = CHAT_ROOT.resolve()
        cleanup_pending = 0
        for relative in attachment_paths:
            try:
                path = (CHAT_ROOT / relative).resolve()
                if root in path.parents and path.is_file() and not path.is_symlink():
                    path.unlink(missing_ok=True)
            except OSError:
                cleanup_pending += 1
        return jsonify(ok=True, cleanup_pending=cleanup_pending)

    @blueprint.get("/api/chat/conversations/<conversation_id>/messages")
    def messages(conversation_id):
        identity = _identity()
        before = request.args.get("before", type=int)
        after = request.args.get("after", type=int)
        limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
        with connect(DB_PATH) as connection:
            if not _member(connection, conversation_id, identity["owner_id"]):
                abort(404)
            where = "conversation_id=?"
            params = [conversation_id]
            order = "DESC"
            if before:
                where += " AND id<?"; params.append(before)
            if after:
                where += " AND id>?"; params.append(after); order = "ASC"
            rows = connection.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY id {order} LIMIT ?", (*params, limit)
            ).fetchall()
            result = [_message_json(connection, row, identity["owner_id"]) for row in rows]
            if order == "DESC": result.reverse()
        response = jsonify(messages=result)
        response.headers["ETag"] = f'"chat-{conversation_id}-{result[-1]["id"] if result else 0}"'
        return response

    @blueprint.post("/api/chat/conversations/<conversation_id>/messages")
    def send_message(conversation_id):
        identity = _identity()
        body = str(request.form.get("body") or "").strip()
        client_id = str(request.form.get("client_message_id") or "")[:80] or uuid.uuid4().hex
        uploads = [item for item in request.files.getlist("attachments") if item.filename]
        if len(body) > MAX_MESSAGE or len(uploads) > MAX_ATTACHMENTS:
            return jsonify(error="Message or attachment limit exceeded."), 413
        if sum(int(item.content_length or 0) for item in uploads) > MAX_BATCH:
            return jsonify(error="Attachments exceed the 100 MiB message limit."), 413
        if not body and not uploads:
            return jsonify(error="Write a message or add a photo."), 422
        body_cipher, body_nonce = _encrypt(body.encode("utf-8")) if body else (None, None)
        now = utcnow()
        stored = []
        try:
            with connect(DB_PATH) as connection:
                if not _member(connection, conversation_id, identity["owner_id"]):
                    abort(404)
                existing = connection.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND sender_id=? AND client_message_id=?",
                    (conversation_id, identity["owner_id"], client_id),
                ).fetchone()
                if existing:
                    return jsonify(message=_message_json(connection, existing, identity["owner_id"]), duplicate=True)
                cursor = connection.execute(
                    """INSERT INTO messages(conversation_id,sender_id,sender_name,body_cipher,body_nonce,client_message_id,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (conversation_id, identity["owner_id"], identity["name"], body_cipher, body_nonce, client_id, now),
                )
                message_id = cursor.lastrowid
                for upload in uploads:
                    attachment = _store_attachment(upload, message_id, conversation_id)
                    stored.extend([CHAT_ROOT / attachment["object_path"], CHAT_ROOT / attachment["preview_path"]])
                    connection.execute(
                        """INSERT INTO chat_attachments
                           (id,message_id,conversation_id,object_path,preview_path,mime_type,byte_size,width,height,sha256,
                            original_name_cipher,original_name_nonce,created_at)
                           VALUES(:id,:message_id,:conversation_id,:object_path,:preview_path,:mime_type,:byte_size,:width,:height,:sha256,
                                  :original_name_cipher,:original_name_nonce,:created_at)""", attachment,
                    )
                connection.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
                recipients = connection.execute(
                    "SELECT owner_id FROM conversation_members WHERE conversation_id=? AND owner_id<>?",
                    (conversation_id, identity["owner_id"]),
                ).fetchall()
                connection.executemany(
                    """INSERT INTO notification_jobs(conversation_id,message_id,recipient_id,available_at,created_at)
                       VALUES(?,?,?,?,?)""",
                    [(conversation_id, message_id, row["owner_id"], now, now) for row in recipients],
                )
                row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
                result = _message_json(connection, row, identity["owner_id"])
        except Exception:
            for path in stored: path.unlink(missing_ok=True)
            raise
        return jsonify(message=result), 201

    @blueprint.patch("/api/chat/messages/<int:message_id>")
    def edit_message(message_id):
        identity = _identity()
        body = str((request.get_json(silent=True) or {}).get("body") or "").strip()
        if not body or len(body) > MAX_MESSAGE:
            return jsonify(error="Message text is required and must be under 10,000 characters."), 422
        cipher, nonce = _encrypt(body.encode())
        with connect(DB_PATH) as connection:
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["sender_id"] != identity["owner_id"] or row["deleted_at"]:
                abort(404)
            connection.execute("UPDATE messages SET body_cipher=?,body_nonce=?,updated_at=? WHERE id=?", (cipher, nonce, utcnow(), message_id))
        return jsonify(ok=True)

    @blueprint.delete("/api/chat/messages/<int:message_id>")
    def delete_message(message_id):
        identity = _identity()
        now = utcnow()
        attachment_paths = []
        with connect(DB_PATH) as connection:
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or row["sender_id"] != identity["owner_id"]:
                abort(404)
            attachments = connection.execute(
                "SELECT object_path,preview_path FROM chat_attachments WHERE message_id=?", (message_id,)
            ).fetchall()
            for attachment in attachments:
                attachment_paths.extend(filter(None, (attachment["object_path"], attachment["preview_path"])))
            connection.execute("DELETE FROM chat_attachments WHERE message_id=?", (message_id,))
            connection.execute("UPDATE messages SET body_cipher=NULL,body_nonce=NULL,deleted_at=?,updated_at=? WHERE id=?", (now, now, message_id))
        root = CHAT_ROOT.resolve()
        for relative in attachment_paths:
            path = (CHAT_ROOT / relative).resolve()
            if root in path.parents and path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)
        return jsonify(ok=True)

    @blueprint.post("/api/chat/conversations/<conversation_id>/read")
    def mark_read(conversation_id):
        identity = _identity()
        message_id = max(int((request.get_json(silent=True) or {}).get("message_id") or 0), 0)
        with connect(DB_PATH) as connection:
            if not _member(connection, conversation_id, identity["owner_id"]): abort(404)
            highest = connection.execute("SELECT COALESCE(max(id),0) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            message_id = min(message_id, highest)
            connection.execute(
                """INSERT INTO conversation_reads VALUES(?,?,?,?) ON CONFLICT(conversation_id,owner_id)
                   DO UPDATE SET last_message_id=max(last_message_id,excluded.last_message_id),seen_at=excluded.seen_at""",
                (conversation_id, identity["owner_id"], message_id, utcnow()),
            )
        return jsonify(ok=True)

    def attachment_or_404(connection, attachment_id, owner_id):
        row = connection.execute(
            """SELECT a.* FROM chat_attachments a JOIN messages m ON m.id=a.message_id
               WHERE a.id=? AND m.deleted_at IS NULL""", (attachment_id,)
        ).fetchone()
        if not row or not _member(connection, row["conversation_id"], owner_id): abort(404)
        return row

    @blueprint.get("/api/chat/attachments/<attachment_id>/<kind>")
    def attachment(attachment_id, kind):
        identity = _identity()
        if kind not in {"preview", "original"}: abort(404)
        with connect(DB_PATH) as connection:
            row = attachment_or_404(connection, attachment_id, identity["owner_id"])
            relative = row["preview_path"] if kind == "preview" else row["object_path"]
            path = (CHAT_ROOT / relative).resolve()
            if CHAT_ROOT.resolve() not in path.parents or not path.is_file() or path.is_symlink(): abort(404)
            packed = path.read_bytes()
            data = _decrypt(packed[12:], packed[:12])
            name = "chat-photo.jpg"
            mime = "image/jpeg" if kind == "preview" else row["mime_type"]
            if kind == "original":
                name = _decrypt(row["original_name_cipher"], row["original_name_nonce"]).decode("utf-8", errors="replace")
        return send_file(BytesIO(data), mimetype=mime, as_attachment=False, download_name=name, max_age=0)

    @blueprint.post("/api/chat/attachments/<attachment_id>/save-to-media")
    def save_to_media(attachment_id):
        identity = _identity()
        with connect(DB_PATH) as connection:
            row = attachment_or_404(connection, attachment_id, identity["owner_id"])
            packed = (CHAT_ROOT / row["object_path"]).read_bytes()
            data = _decrypt(packed[12:], packed[:12])
            name = _decrypt(row["original_name_cipher"], row["original_name_nonce"]).decode("utf-8", errors="replace")
        incoming = Path(os.environ.get("PHOTO_DATA", "/data")) / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="chat-save-", suffix=Path(name).suffix, dir=incoming)
        try:
            with os.fdopen(fd, "wb") as stream: stream.write(data)
            result = canonical_ingest_media(
                staged_path=temporary, original_filename=name, mime_type=row["mime_type"],
                owner_user_id=identity["owner_id"], owner_name=identity["name"], visibility="private",
                ingestion_source="chat_save",
            )
        finally:
            Path(temporary).unlink(missing_ok=True)
        return jsonify(ok=True, media_id=result["id"])

    @blueprint.post("/api/chat/push/web")
    def register_web_push():
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        endpoint = str(payload.get("endpoint") or "")[:2048]
        keys = payload.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            return jsonify(error="Invalid push subscription."), 422
        now = utcnow()
        with connect(DB_PATH) as connection:
            # A browser push endpoint belongs to one current portal identity. If
            # the same installed app is later used by another household member,
            # retire the prior mapping so private alerts cannot cross accounts.
            connection.execute(
                "DELETE FROM push_subscriptions WHERE platform='web' AND endpoint=? AND owner_id<>?",
                (endpoint, identity["owner_id"]),
            )
            connection.execute(
                """INSERT INTO push_subscriptions(id,owner_id,platform,endpoint,p256dh,auth,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_id,platform,endpoint) DO UPDATE SET
                   p256dh=excluded.p256dh,auth=excluded.auth,updated_at=excluded.updated_at""",
                (uuid.uuid4().hex, identity["owner_id"], "web", endpoint, str(keys["p256dh"])[:512], str(keys["auth"])[:512], now, now),
            )
        return jsonify(ok=True)

    @blueprint.post("/api/chat/push/android")
    def register_android_push():
        identity = _identity()
        token = str((request.get_json(silent=True) or {}).get("token") or "").strip()
        if len(token) < 32 or len(token) > 4096:
            return jsonify(error="Invalid Android notification token."), 422
        now = utcnow()
        with connect(DB_PATH) as connection:
            connection.execute(
                "DELETE FROM push_subscriptions WHERE platform='android' AND device_token=? AND owner_id<>?",
                (token, identity["owner_id"]),
            )
            connection.execute(
                """INSERT INTO push_subscriptions(id,owner_id,platform,device_token,created_at,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,platform,device_token) DO UPDATE SET
                   updated_at=excluded.updated_at""",
                (uuid.uuid4().hex, identity["owner_id"], "android", token, now, now),
            )
        return jsonify(ok=True)

    @blueprint.get("/api/chat/push/public-key")
    def web_push_public_key():
        _identity()
        return jsonify(public_key=os.environ.get("DAVID_PI_VAPID_PUBLIC_KEY", ""))

    app.register_blueprint(blueprint)
