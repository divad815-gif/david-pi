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
import re
import secrets
import sqlite3
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from urllib.parse import urlencode, urlparse
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import Blueprint, abort, current_app, g, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .access_control import IDENTITY_ROLES, normalize_login
from .content_policy import (
    Actor,
    AuthorizationFacts,
    decide_transaction_authorization,
    load_route_policy,
)
from .identity import current_device
from .platform import (
    PLATFORM_DATA,
    connect,
    emit_outbox_event,
    initialize_data_foundation,
    migrate,
    record_mutation_audit,
    utcnow,
)
from .push_endpoint_policy import PushEndpointPolicyError, resolve_push_endpoint
from .public_http import fetch_chain


DB_PATH = PLATFORM_DATA / "chat.db"
CHAT_ROOT = Path(os.environ.get("DAVID_PI_CHAT_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "chat"))
KEY_FILE = Path(os.environ.get("DAVID_PI_CHAT_KEY_FILE", "/run/secrets/chat-master.key"))
MAX_MESSAGE = 10_000
MAX_ATTACHMENTS = 8
MAX_ATTACHMENT = 25 * 1024 * 1024
MAX_BATCH = 100 * 1024 * 1024
ALLOWED_IMAGE = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
PENDING_ATTACHMENT_GRACE_SECONDS = 15 * 60
PENDING_ATTACHMENT_RE = re.compile(r"\.([0-9a-f]{32})\.pending\Z")
GIPHY_HOST = re.compile(r"^(?:i|media(?:[0-9]+)?)\.giphy\.com$")
GIF_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,4096}$")
GIF_TOKEN_TTL_SECONDS = 5 * 60
GIF_PREVIEW_MAX = 3 * 1024 * 1024
GIF_TOKEN_AAD = b"david-pi-giphy-v1"


class ChatPrivateStorageUnavailable(RuntimeError):
    """Chat cryptography is unavailable without exposing secret details."""


_VERIFIED_KEY_FINGERPRINT = None


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
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          deleted_at TEXT, deleted_by TEXT, purge_after TEXT,
          version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS conversation_members (
          conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL, joined_at TEXT NOT NULL,
          left_at TEXT, version INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(conversation_id, owner_id),
          FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
          body_cipher BLOB, body_nonce BLOB, client_message_id TEXT,
          created_at TEXT NOT NULL, updated_at TEXT, deleted_at TEXT,
          deleted_by TEXT, purge_after TEXT, version INTEGER NOT NULL DEFAULT 1,
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
          version INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(conversation_id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS push_subscriptions (
          id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, platform TEXT NOT NULL,
          endpoint TEXT, p256dh TEXT, auth TEXT, device_token TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1,
          UNIQUE(owner_id, platform, endpoint), UNIQUE(owner_id, platform, device_token)
        );
        CREATE TABLE IF NOT EXISTS notification_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          message_id INTEGER NOT NULL, recipient_id TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          available_at TEXT NOT NULL, created_at TEXT NOT NULL, last_error_code TEXT,
          lease_token TEXT, lease_expires_at TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_jobs_pending
          ON notification_jobs(status, available_at, id);
        """
    )
    additions = {
        "conversations": {
            "deleted_at": "ALTER TABLE conversations ADD COLUMN deleted_at TEXT",
            "deleted_by": "ALTER TABLE conversations ADD COLUMN deleted_by TEXT",
            "purge_after": "ALTER TABLE conversations ADD COLUMN purge_after TEXT",
            "version": "ALTER TABLE conversations ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        },
        "conversation_members": {
            "left_at": "ALTER TABLE conversation_members ADD COLUMN left_at TEXT",
            "version": "ALTER TABLE conversation_members ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        },
        "messages": {
            "deleted_by": "ALTER TABLE messages ADD COLUMN deleted_by TEXT",
            "purge_after": "ALTER TABLE messages ADD COLUMN purge_after TEXT",
            "version": "ALTER TABLE messages ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        },
        "conversation_reads": {
            "version": "ALTER TABLE conversation_reads ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        },
        "push_subscriptions": {
            "version": "ALTER TABLE push_subscriptions ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        },
        "notification_jobs": {
            "lease_token": "ALTER TABLE notification_jobs ADD COLUMN lease_token TEXT",
            "lease_expires_at": "ALTER TABLE notification_jobs ADD COLUMN lease_expires_at TEXT",
        },
    }
    for table, statements in additions.items():
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, statement in statements.items():
            if column not in columns:
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError:
                    current = {
                        row["name"]
                        for row in connection.execute(f"PRAGMA table_info({table})")
                    }
                    if column not in current:
                        raise
    initialize_data_foundation(connection)


migrate(DB_PATH, _migrate)
try:
    DB_PATH.chmod(0o640)
except OSError:
    pass

CHAT_ROOT.mkdir(parents=True, exist_ok=True)
try:
    CHAT_ROOT.chmod(0o700)
except OSError:
    pass


def _key():
    """Load either the established raw or base64 key, failing closed.

    Keep the low-level failure content-neutral: filesystem paths, ownership,
    key bytes, and decoder errors must never reach an API response.
    """
    try:
        encoded = os.environ.get("DAVID_PI_CHAT_KEY_B64", "").strip()
        if encoded:
            raw = base64.b64decode(encoded, validate=True)
        else:
            raw = KEY_FILE.read_bytes().strip()
            if len(raw) != 32:
                raw = base64.b64decode(raw, validate=True)
    except (OSError, ValueError):
        raise ChatPrivateStorageUnavailable from None
    if len(raw) != 32:
        raise ChatPrivateStorageUnavailable
    return raw


def _verified_key():
    """Bind a structurally valid key to any encrypted state already present."""
    global _VERIFIED_KEY_FINGERPRINT
    raw = _key()
    fingerprint = hashlib.sha256(raw).digest()
    if _VERIFIED_KEY_FINGERPRINT == fingerprint:
        return raw
    queries = (
        "SELECT body_cipher,body_nonce FROM messages WHERE body_cipher IS NOT NULL LIMIT 1",
        "SELECT title_cipher,title_nonce FROM conversations WHERE title_cipher IS NOT NULL LIMIT 1",
        "SELECT original_name_cipher,original_name_nonce FROM chat_attachments "
        "WHERE original_name_cipher IS NOT NULL LIMIT 1",
    )
    try:
        uri = DB_PATH.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as connection:
            for query in queries:
                row = connection.execute(query).fetchone()
                if row is None:
                    continue
                AESGCM(raw).decrypt(
                    bytes(row[1]), bytes(row[0]), b"david-pi-chat-v1"
                )
                break
    except (InvalidTag, OSError, sqlite3.Error, TypeError, ValueError):
        raise ChatPrivateStorageUnavailable from None
    _VERIFIED_KEY_FINGERPRINT = fingerprint
    return raw


def chat_key_available() -> bool:
    """Return whether the key is readable, valid, and unlocks existing state."""
    try:
        _verified_key()
    except ChatPrivateStorageUnavailable:
        return False
    return True


def _encrypt(value: bytes):
    nonce = secrets.token_bytes(12)
    return AESGCM(_verified_key()).encrypt(
        nonce, value, b"david-pi-chat-v1"
    ), nonce


def _safe_giphy_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not GIPHY_HOST.fullmatch(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        return None
    return value


def _gif_media_token(target, owner_id, purpose):
    safe_target = _safe_giphy_url(target)
    if safe_target is None or purpose not in {"preview", "selected"}:
        return None
    payload = json.dumps(
        {
            "expires": int(time.time()) + GIF_TOKEN_TTL_SECONDS,
            "owner": owner_id,
            "purpose": purpose,
            "url": safe_target,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    nonce = secrets.token_bytes(12)
    cipher = AESGCM(_verified_key()).encrypt(nonce, payload, GIF_TOKEN_AAD)
    return base64.urlsafe_b64encode(nonce + cipher).rstrip(b"=").decode("ascii")


def _gif_media_target(token, owner_id):
    if not isinstance(token, str) or not GIF_TOKEN.fullmatch(token):
        return None
    try:
        padded = token + "=" * ((4 - len(token) % 4) % 4)
        packed = base64.b64decode(padded, altchars=b"-_", validate=True)
        if len(packed) < 29:
            return None
        raw = AESGCM(_verified_key()).decrypt(
            packed[:12], packed[12:], GIF_TOKEN_AAD
        )
        payload = json.loads(raw)
    except (InvalidTag, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "expires", "owner", "purpose", "url"
    }:
        return None
    expires = payload.get("expires")
    now = int(time.time())
    if (
        payload.get("owner") != owner_id
        or payload.get("purpose") not in {"preview", "selected"}
        or not isinstance(expires, int)
        or expires < now
        or expires > now + GIF_TOKEN_TTL_SECONDS
    ):
        return None
    target = _safe_giphy_url(payload.get("url"))
    if target is None:
        return None
    limit = GIF_PREVIEW_MAX if payload["purpose"] == "preview" else MAX_ATTACHMENT
    return target, limit


def _fetch_giphy_gif(target, limit):
    data, content_type, final_url = fetch_chain(
        target, "image/gif", limit, timeout=10
    )
    if _safe_giphy_url(final_url) is None or content_type != "image/gif":
        raise ValueError("invalid gif")
    with Image.open(BytesIO(data)) as image:
        if image.format != "GIF" or image.width * image.height > 40_000_000:
            raise ValueError("invalid gif")
        image.verify()
    return data


def _directory_fd(parent_fd: int, name: str, *, create: bool = False) -> int:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ValueError("invalid chat storage component")
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )


def _object_directory_fd(conversation_id: str, *, create: bool = False) -> int:
    if (
        not conversation_id
        or len(conversation_id) > 80
        or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in conversation_id)
    ):
        raise ValueError("invalid conversation storage id")
    root_fd = os.open(
        CHAT_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    prefix_fd = None
    try:
        prefix_fd = _directory_fd(root_fd, conversation_id[:2], create=create)
        return _directory_fd(prefix_fd, conversation_id, create=create)
    finally:
        if prefix_fd is not None:
            os.close(prefix_fd)
        os.close(root_fd)


def _write_private(directory_fd: int, name: str, value: bytes):
    """Atomically place encrypted data through a pinned, non-symlink directory."""
    if not name or "/" in name or "\x00" in name:
        raise ValueError("invalid chat object name")
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _unlink_private_names(directory_fd: int, names) -> None:
    changed = False
    for name in names:
        if not name or "/" in name or "\x00" in name:
            continue
        try:
            os.unlink(name, dir_fd=directory_fd)
            changed = True
        except FileNotFoundError:
            pass
    if changed:
        os.fsync(directory_fd)


def _close_attachment_handle(handle) -> None:
    descriptor = handle.get("directory_fd") if handle else None
    if descriptor is not None:
        os.close(descriptor)
        handle["directory_fd"] = None


def _rollback_attachment_handle(handle) -> None:
    descriptor = handle.get("directory_fd") if handle else None
    if descriptor is None:
        return
    _unlink_private_names(
        descriptor,
        (handle["object_name"], handle["preview_name"], handle["pending_name"]),
    )


def _commit_attachment_handle(handle) -> None:
    descriptor = handle.get("directory_fd") if handle else None
    if descriptor is None:
        return
    # The database row is committed before this marker is removed. If the
    # process dies here, startup reconciliation sees the row and preserves the
    # two encrypted objects while removing only the stale marker.
    _unlink_private_names(descriptor, (handle["pending_name"],))


@contextmanager
def _attachment_write_scope():
    """Keep object-directory descriptors pinned until the DB outcome is known."""
    handles = []
    try:
        yield handles
    except BaseException:
        for handle in handles:
            try:
                _rollback_attachment_handle(handle)
            except OSError:
                # The durable pending marker lets the next reconciliation pass
                # finish cleanup if this best-effort rollback is interrupted.
                pass
        raise
    else:
        for handle in handles:
            try:
                _commit_attachment_handle(handle)
            except OSError:
                # A committed row makes a leftover marker harmless; startup
                # reconciliation will remove only the marker.
                pass
    finally:
        for handle in handles:
            try:
                _close_attachment_handle(handle)
            except OSError:
                pass


def _reconcile_pending_attachments(*, now: float | None = None) -> None:
    """Recover crash-left attachment journals without following symlinks.

    A grace period prevents a second process starting at the same time from
    touching an upload that is still inside its database transaction.
    """
    cutoff = (time.time() if now is None else now) - PENDING_ATTACHMENT_GRACE_SECONDS
    try:
        root_fd = os.open(
            CHAT_ROOT,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError:
        return
    try:
        with connect(DB_PATH) as connection:
            for prefix in os.listdir(root_fd):
                if len(prefix) != 2 or any(
                    character
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                    for character in prefix
                ):
                    continue
                prefix_fd = None
                try:
                    prefix_fd = _directory_fd(root_fd, prefix)
                    for conversation_id in os.listdir(prefix_fd):
                        if (
                            conversation_id[:2] != prefix
                            or len(conversation_id) > 80
                            or any(
                                character
                                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                                for character in conversation_id
                            )
                        ):
                            continue
                        object_fd = None
                        try:
                            object_fd = _directory_fd(prefix_fd, conversation_id)
                            entries = os.listdir(object_fd)
                            for pending_name in entries:
                                match = PENDING_ATTACHMENT_RE.fullmatch(pending_name)
                                if not match:
                                    continue
                                try:
                                    details = os.stat(
                                        pending_name,
                                        dir_fd=object_fd,
                                        follow_symlinks=False,
                                    )
                                except FileNotFoundError:
                                    continue
                                if not stat.S_ISREG(details.st_mode) or details.st_mtime > cutoff:
                                    continue
                                attachment_id = match.group(1)
                                committed = connection.execute(
                                    "SELECT 1 FROM chat_attachments "
                                    "WHERE id=? AND conversation_id=?",
                                    (attachment_id, conversation_id),
                                ).fetchone()
                                temporary = re.compile(
                                    rf"\.{attachment_id}\.(?:bin|preview)\.[0-9a-f]{{24}}\.tmp\Z"
                                )
                                cleanup = [
                                    item for item in entries if temporary.fullmatch(item)
                                ]
                                if committed is None:
                                    cleanup.extend(
                                        (f"{attachment_id}.bin", f"{attachment_id}.preview")
                                    )
                                cleanup.append(pending_name)
                                _unlink_private_names(object_fd, cleanup)
                        except (OSError, ValueError):
                            continue
                        finally:
                            if object_fd is not None:
                                os.close(object_fd)
                except (OSError, ValueError):
                    continue
                finally:
                    if prefix_fd is not None:
                        os.close(prefix_fd)
    finally:
        os.close(root_fd)


def _read_private(relative: str, maximum: int = MAX_ATTACHMENT + 1024) -> bytes:
    """Read one object through pinned directory descriptors without path races."""
    parts = str(relative or "").split("/")
    if len(parts) != 3 or any(
        not part or part in {".", ".."} or "\x00" in part for part in parts
    ):
        raise FileNotFoundError(relative)
    root_fd = os.open(
        CHAT_ROOT,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    prefix_fd = object_fd = descriptor = None
    try:
        prefix_fd = _directory_fd(root_fd, parts[0])
        object_fd = _directory_fd(prefix_fd, parts[1])
        descriptor = os.open(
            parts[2],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=object_fd,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size < 13 or details.st_size > maximum:
            raise FileNotFoundError(relative)
        chunks = []
        remaining = details.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise OSError("chat object changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        for item in (descriptor, object_fd, prefix_fd, root_fd):
            if item is not None:
                os.close(item)


def _decrypt(cipher, nonce):
    if not cipher:
        return b""
    try:
        return AESGCM(_verified_key()).decrypt(
            bytes(nonce), bytes(cipher), b"david-pi-chat-v1"
        )
    except (InvalidTag, TypeError, ValueError):
        # A missing/wrong key or malformed encrypted row is never treated as
        # plaintext and never partially rendered.
        raise ChatPrivateStorageUnavailable from None


_TEST_IDENTITY_ROLES = {
    "david@example.test": "admin",
    "diana@example.test": "household",
}


def _identity_role(identity) -> str | None:
    principal_id = normalize_login(identity.get("owner_id"))
    role = IDENTITY_ROLES.get(principal_id)
    request_access = getattr(g, "portal_access", None)
    if isinstance(request_access, dict):
        role = request_access.get("role") or role
    if current_app.testing:
        role = _TEST_IDENTITY_ROLES.get(principal_id, role)
    return role


def _identity(required=True):
    identity = current_device()
    role = _identity_role(identity)
    if required and (
        not identity["verified"] or role not in {"admin", "household"}
    ):
        abort(
            403,
            description="Open Chat through an approved private Tailscale account.",
        )
    identity = dict(identity)
    identity["role"] = role
    return identity


def _actor(identity) -> Actor:
    return Actor(
        principal_id=normalize_login(identity.get("owner_id")),
        role=identity.get("role"),
        kind="human",
    )


def _authorize(route_id: str, identity, facts: AuthorizationFacts, *, action=None):
    return decide_transaction_authorization(
        load_route_policy().by_id[route_id], _actor(identity), facts, action=action
    )


def _metadata_digest(row) -> str | None:
    if row is None:
        return None
    keys = set(row.keys())
    payload = {
        "version": int(row["version"]) if "version" in keys else 0,
        "deleted": bool(row["deleted_at"]) if "deleted_at" in keys else False,
        "left": bool(row["left_at"]) if "left_at" in keys else False,
        "owner": hashlib.sha256(
            str(
                row["owner_id"]
                if "owner_id" in keys
                else row["sender_id"]
                if "sender_id" in keys
                else row["created_by"]
                if "created_by" in keys
                else ""
            ).encode("utf-8")
        ).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _audit_mutation(connection, identity, *, domain, object_id, action, before, after):
    occurred_at = utcnow()
    actor = _actor(identity)
    record_mutation_audit(
        connection,
        event_id=uuid.uuid4().hex,
        actor_id=actor.principal_id,
        domain=domain,
        object_id=str(object_id),
        action=action,
        request_id=uuid.uuid4().hex,
        before_digest=_metadata_digest(before),
        after_digest=_metadata_digest(after),
        occurred_at=occurred_at,
    )
    if after is not None and "version" in set(after.keys()):
        emit_outbox_event(
            connection,
            domain=domain,
            object_id=str(object_id),
            event_type=action,
            object_version=int(after["version"]),
            payload_digest=_metadata_digest(after),
            occurred_at=occurred_at,
        )


def _member(connection, conversation_id, owner_id):
    return connection.execute(
        "SELECT 1 FROM conversation_members m "
        "JOIN conversations c ON c.id=m.conversation_id "
        "WHERE m.conversation_id=? AND m.owner_id=? AND m.left_at IS NULL "
        "AND c.deleted_at IS NULL",
        (conversation_id, owner_id),
    ).fetchone() is not None


def _reserved_test_identity(owner_id):
    """Keep test fixtures from ever becoming household identities in production."""
    value = str(owner_id or "").strip().casefold()
    return value.endswith("@example.test") or value.endswith(".example.test")


def _allowed_household_ids() -> frozenset[str]:
    allowed = {normalize_login(owner_id) for owner_id in IDENTITY_ROLES}
    if current_app.testing:
        allowed.update(_TEST_IDENTITY_ROLES)
    return frozenset(allowed)


def _delivery_in_progress(
    connection,
    *,
    owner_id: str | None = None,
    conversation_id: str | None = None,
    message_id: int | None = None,
) -> bool:
    """Return whether a still-valid provider lease overlaps this mutation.

    Mutating routes call this only after ``BEGIN IMMEDIATE``. A worker therefore
    cannot claim a new job between this check and the mutation commit, while an
    already claimed worker keeps its authorization stable until provider I/O
    finishes or its bounded lease expires.
    """
    where = [
        "status='working'",
        "lease_token IS NOT NULL",
        "lease_expires_at>?",
    ]
    parameters = [utcnow()]
    if owner_id is not None:
        where.append("recipient_id=?")
        parameters.append(owner_id)
    if conversation_id is not None:
        where.append("conversation_id=?")
        parameters.append(conversation_id)
    if message_id is not None:
        where.append("message_id=?")
        parameters.append(message_id)
    return connection.execute(
        f"SELECT 1 FROM notification_jobs WHERE {' AND '.join(where)} LIMIT 1",
        parameters,
    ).fetchone() is not None


def _delivery_conflict():
    return jsonify(
        error="A notification is finishing delivery. Try again in a moment.",
        code="notification_delivery_in_progress",
        conflict=True,
    ), 409


def _conversation_summary(connection, row, me):
    members = connection.execute(
        """SELECT m.owner_id, COALESCE(u.display_name,m.owner_id) display_name,
                  CASE WHEN m.left_at IS NULL THEN 1 ELSE 0 END active
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
        "version": int(row["version"]),
    }


def _message_json(connection, row, me):
    deleted = bool(row["deleted_at"])
    attachments = connection.execute(
        "SELECT id,mime_type,byte_size,width,height FROM chat_attachments WHERE message_id=? ORDER BY created_at,id",
        (row["id"],),
    ).fetchall()
    total_other = connection.execute(
        "SELECT count(*) FROM conversation_members "
        "WHERE conversation_id=? AND owner_id<>? AND left_at IS NULL",
        (row["conversation_id"], row["sender_id"]),
    ).fetchone()[0]
    seen_other = connection.execute(
        """SELECT count(*) FROM conversation_reads r
           JOIN conversation_members m
             ON m.conversation_id=r.conversation_id AND m.owner_id=r.owner_id
           WHERE r.conversation_id=? AND r.owner_id<>? AND r.last_message_id>=?
             AND m.left_at IS NULL""",
        (row["conversation_id"], row["sender_id"], row["id"]),
    ).fetchone()[0]
    return {
        "id": row["id"], "conversation_id": row["conversation_id"],
        "sender_id": row["sender_id"], "sender_name": row["sender_name"],
        "mine": row["sender_id"] == me,
        "body": "" if deleted else _decrypt(row["body_cipher"], row["body_nonce"]).decode("utf-8"),
        "created_at": row["created_at"], "updated_at": row["updated_at"], "deleted": deleted,
        "version": int(row["version"]),
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
    cipher, nonce = _encrypt(data)
    preview_cipher, preview_nonce = _encrypt(preview_buffer.getvalue())
    name_cipher, name_nonce = _encrypt(name.encode("utf-8", errors="replace"))
    object_name = f"{attachment_id}.bin"
    preview_name = f"{attachment_id}.preview"
    pending_name = f".{attachment_id}.pending"
    directory_fd = _object_directory_fd(conversation_id, create=True)
    try:
        _write_private(directory_fd, pending_name, b"david-pi-chat-pending-v1")
        _write_private(directory_fd, object_name, nonce + cipher)
        _write_private(directory_fd, preview_name, preview_nonce + preview_cipher)
    except BaseException:
        try:
            _unlink_private_names(
                directory_fd, (object_name, preview_name, pending_name)
            )
        except OSError:
            # Preserve the durable marker for reconciliation when immediate
            # rollback itself is interrupted or the filesystem is unavailable.
            pass
        finally:
            os.close(directory_fd)
        raise
    attachment = {
        "id": attachment_id, "message_id": message_id, "conversation_id": conversation_id,
        "object_path": f"{conversation_id[:2]}/{conversation_id}/{object_name}",
        "preview_path": f"{conversation_id[:2]}/{conversation_id}/{preview_name}",
        "mime_type": declared, "byte_size": len(data), "width": width, "height": height,
        "sha256": hashlib.sha256(data).hexdigest(), "original_name_cipher": name_cipher,
        "original_name_nonce": name_nonce, "created_at": utcnow(),
    }
    handle = {
        "directory_fd": directory_fd,
        "object_name": object_name,
        "preview_name": preview_name,
        "pending_name": pending_name,
    }
    return attachment, handle


def init_chat(app, canonical_ingest_media, rollback_ingest_media):
    _reconcile_pending_attachments()
    blueprint = Blueprint("chat", __name__)

    @blueprint.errorhandler(ChatPrivateStorageUnavailable)
    def private_storage_unavailable(_error):
        return jsonify(
            error=(
                "Chat's private storage is temporarily unavailable. "
                "No messages were changed."
            ),
            code="chat_private_storage_unavailable",
        ), 503

    @app.before_request
    def require_private_storage_for_chat_api():
        if request.path.startswith("/api/chat/"):
            # Authenticate first so secret availability is never disclosed to
            # an unapproved caller. Register this as an app-level guard before
            # identity bookkeeping: Flask runs all app before_request handlers
            # ahead of blueprint handlers, so a blueprint guard would be too
            # late to guarantee a mutation-free locked-storage response.
            _identity()
            try:
                _verified_key()
            except ChatPrivateStorageUnavailable as error:
                return private_storage_unavailable(error)
        return None

    @app.before_request
    def remember_verified_chat_user():
        if request.path in {"/health", "/ready"}:
            return None
        identity = current_device()
        if (
            not identity["verified"]
            or _identity_role(identity) not in {"admin", "household"}
        ):
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
        allowed = sorted(_allowed_household_ids() - {identity["owner_id"]})
        if not allowed:
            return jsonify(users=[])
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT owner_id,display_name FROM portal_users "
                f"WHERE owner_id IN ({','.join('?' for _ in allowed)}) "
                "ORDER BY lower(display_name)",
                allowed,
            ).fetchall()
        return jsonify(users=[dict(row) for row in rows])

    @blueprint.get("/api/chat/gifs/search")
    def gif_search():
        identity = _identity()
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
            preview_token = _gif_media_token(
                preview, identity["owner_id"], "preview"
            )
            selected_token = _gif_media_token(
                selected, identity["owner_id"], "selected"
            )
            if preview_token and selected_token:
                results.append({
                    "id": str(item.get("id") or "")[:80],
                    "preview": f"/api/chat/gifs/media/{preview_token}",
                    "selected": f"/api/chat/gifs/media/{selected_token}",
                })
        return jsonify(configured=True, results=results)

    @blueprint.get("/api/chat/gifs/media/<token>")
    def gif_media(token):
        identity = _identity()
        resolved = _gif_media_target(token, identity["owner_id"])
        if resolved is None:
            abort(404)
        target, limit = resolved
        try:
            data = _fetch_giphy_gif(target, limit)
        except (
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ):
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
                   WHERE m.owner_id=? AND m.left_at IS NULL AND c.deleted_at IS NULL
                   {test_guard} ORDER BY c.updated_at DESC LIMIT 100""",
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
        if not set(member_ids).issubset(_allowed_household_ids()):
            return jsonify(error="One selected household member is not available."), 422
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            decision = _authorize(
                "chat.conversation.create", identity, AuthorizationFacts()
            )
            if not decision.allowed:
                abort(403)
            known = {row[0] for row in connection.execute(
                f"SELECT owner_id FROM portal_users WHERE owner_id IN ({','.join('?' for _ in member_ids)})", member_ids
            )}
            if known != set(member_ids):
                return jsonify(error="One selected household member is not available."), 422
            kind = "direct" if len(member_ids) == 2 else "group"
            direct_key = "|".join(member_ids) if kind == "direct" else None
            if direct_key:
                existing = connection.execute(
                    "SELECT id,version FROM conversations "
                    "WHERE direct_key=? AND deleted_at IS NULL",
                    (direct_key,),
                ).fetchone()
                if existing:
                    return jsonify(
                        id=existing["id"],
                        version=int(existing["version"]),
                        existing=True,
                    )
            title = str(payload.get("title") or "").strip()[:120]
            title_cipher, title_nonce = _encrypt(title.encode()) if title else (None, None)
            conversation_id = uuid.uuid4().hex
            now = utcnow()
            connection.execute(
                """INSERT INTO conversations
                   (id,kind,title_cipher,title_nonce,direct_key,created_by,
                    created_at,updated_at,version)
                   VALUES(?,?,?,?,?,?,?,?,1)""",
                (conversation_id, kind, title_cipher, title_nonce, direct_key, identity["owner_id"], now, now),
            )
            connection.executemany(
                """INSERT INTO conversation_members
                   (conversation_id,owner_id,joined_at,version) VALUES(?,?,?,1)""",
                [(conversation_id, owner_id, now) for owner_id in member_ids],
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="chat_conversation",
                object_id=conversation_id,
                action="create",
                before=None,
                after=row,
            )
        return jsonify(id=conversation_id, version=1, existing=False), 201

    @blueprint.delete("/api/chat/conversations/<conversation_id>")
    def delete_conversation(conversation_id):
        """Leave personally; shared-history deletion remains disabled."""
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip().casefold()
        if action == "delete_for_all":
            return jsonify(
                error=(
                    "Delete for everyone is disabled until every active member can "
                    "approve the same versioned proposal. No history was changed."
                ),
                code="unanimous_approval_not_available",
            ), 503
        if (
            action != "leave"
            or payload.get("confirmation") != "LEAVE CHAT"
            or payload.get("conversation_id") != conversation_id
        ):
            return jsonify(
                error="Confirm that you want to leave this chat. Shared history will remain."
            ), 422
        try:
            expected_version = int(payload.get("version"))
        except (TypeError, ValueError):
            expected_version = 0
        if expected_version < 1:
            return jsonify(error="Reload this chat before leaving it."), 400
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        purge_after = (now_value + timedelta(days=30)).isoformat()
        last_member = False
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            membership = connection.execute(
                "SELECT * FROM conversation_members "
                "WHERE conversation_id=? AND owner_id=? AND left_at IS NULL",
                (conversation_id, identity["owner_id"]),
            ).fetchone()
            if before is None or membership is None:
                abort(404)
            active_members = frozenset(
                row["owner_id"]
                for row in connection.execute(
                    "SELECT owner_id FROM conversation_members "
                    "WHERE conversation_id=? AND left_at IS NULL",
                    (conversation_id,),
                )
            )
            decision = _authorize(
                "chat.conversation.delete",
                identity,
                AuthorizationFacts(
                    creator_id=before["created_by"],
                    member_ids=active_members,
                    active_member_ids=active_members,
                    legacy=not bool(before["created_by"]),
                ),
                action="leave",
            )
            if not decision.allowed:
                abort(404)
            if int(before["version"]) != expected_version:
                return jsonify(
                    error="This chat changed elsewhere. Reload it before leaving.",
                    conflict=True,
                    latest_version=int(before["version"]),
                ), 409
            if _delivery_in_progress(
                connection,
                owner_id=identity["owner_id"],
                conversation_id=conversation_id,
            ):
                return _delivery_conflict()
            member_changed = connection.execute(
                "UPDATE conversation_members SET left_at=?,version=version+1 "
                "WHERE conversation_id=? AND owner_id=? AND left_at IS NULL",
                (now, conversation_id, identity["owner_id"]),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM conversation_members "
                "WHERE conversation_id=? AND left_at IS NULL",
                (conversation_id,),
            ).fetchone()[0]
            last_member = remaining == 0
            if last_member:
                conversation_changed = connection.execute(
                    "UPDATE conversations SET direct_key=NULL,deleted_at=?,deleted_by=?,"
                    "purge_after=?,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND deleted_at IS NULL",
                    (
                        now,
                        identity["owner_id"],
                        purge_after,
                        now,
                        conversation_id,
                        expected_version,
                    ),
                )
            else:
                conversation_changed = connection.execute(
                    "UPDATE conversations SET direct_key=NULL,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=? AND deleted_at IS NULL",
                    (now, conversation_id, expected_version),
                )
            if not member_changed.rowcount or not conversation_changed.rowcount:
                connection.rollback()
                return jsonify(
                    error="This chat changed elsewhere. Reload it before leaving.",
                    conflict=True,
                ), 409
            connection.execute(
                "DELETE FROM notification_jobs "
                "WHERE conversation_id=? AND recipient_id=? AND status='pending'",
                (conversation_id, identity["owner_id"]),
            )
            after = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="chat_conversation",
                object_id=conversation_id,
                action="leave",
                before=before,
                after=after,
            )
        if last_member:
            return jsonify(
                ok=True,
                action="leave",
                retained_until=purge_after,
                message=(
                    "Chat closed after the last member left and retained for at least 30 days."
                ),
            )
        return jsonify(
            ok=True,
            action="leave",
            message="Chat removed from your list. Other members and shared history were unchanged.",
        )

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
        if uploads:
            _reconcile_pending_attachments()
        body_cipher, body_nonce = _encrypt(body.encode("utf-8")) if body else (None, None)
        now = utcnow()
        with _attachment_write_scope() as stored_attachments:
            with connect(DB_PATH) as connection:
                connection.execute("BEGIN IMMEDIATE")
                conversation = connection.execute(
                    "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL",
                    (conversation_id,),
                ).fetchone()
                active_members = frozenset(
                    row["owner_id"]
                    for row in connection.execute(
                        "SELECT owner_id FROM conversation_members "
                        "WHERE conversation_id=? AND left_at IS NULL",
                        (conversation_id,),
                    )
                )
                if conversation is None or identity["owner_id"] not in active_members:
                    abort(404)
                decision = _authorize(
                    "chat.message.create",
                    identity,
                    AuthorizationFacts(member_ids=active_members),
                )
                if not decision.allowed:
                    abort(404)
                existing = connection.execute(
                    "SELECT * FROM messages WHERE conversation_id=? AND sender_id=? AND client_message_id=?",
                    (conversation_id, identity["owner_id"], client_id),
                ).fetchone()
                if existing:
                    return jsonify(
                        message=_message_json(connection, existing, identity["owner_id"]),
                        duplicate=True,
                    )
                cursor = connection.execute(
                    """INSERT INTO messages
                       (conversation_id,sender_id,sender_name,body_cipher,body_nonce,
                        client_message_id,created_at,version)
                       VALUES(?,?,?,?,?,?,?,1)""",
                    (
                        conversation_id,
                        identity["owner_id"],
                        identity["name"],
                        body_cipher,
                        body_nonce,
                        client_id,
                        now,
                    ),
                )
                message_id = cursor.lastrowid
                for upload in uploads:
                    attachment, handle = _store_attachment(
                        upload, message_id, conversation_id
                    )
                    stored_attachments.append(handle)
                    connection.execute(
                        """INSERT INTO chat_attachments
                           (id,message_id,conversation_id,object_path,preview_path,mime_type,
                            byte_size,width,height,sha256,original_name_cipher,
                            original_name_nonce,created_at)
                           VALUES(:id,:message_id,:conversation_id,:object_path,
                                  :preview_path,:mime_type,:byte_size,:width,:height,:sha256,
                                  :original_name_cipher,:original_name_nonce,:created_at)""",
                        attachment,
                    )
                connection.execute(
                    "UPDATE conversations SET updated_at=?,version=version+1 WHERE id=?",
                    (now, conversation_id),
                )
                recipients = connection.execute(
                    "SELECT owner_id FROM conversation_members "
                    "WHERE conversation_id=? AND owner_id<>? AND left_at IS NULL",
                    (conversation_id, identity["owner_id"]),
                ).fetchall()
                connection.executemany(
                    """INSERT INTO notification_jobs
                       (conversation_id,message_id,recipient_id,available_at,created_at)
                       VALUES(?,?,?,?,?)""",
                    [
                        (conversation_id, message_id, row["owner_id"], now, now)
                        for row in recipients
                    ],
                )
                row = connection.execute(
                    "SELECT * FROM messages WHERE id=?", (message_id,)
                ).fetchone()
                _audit_mutation(
                    connection,
                    identity,
                    domain="chat_message",
                    object_id=message_id,
                    action="create",
                    before=None,
                    after=row,
                )
                result = _message_json(connection, row, identity["owner_id"])
        return jsonify(message=result), 201

    @blueprint.patch("/api/chat/messages/<int:message_id>")
    def edit_message(message_id):
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        body = str(payload.get("body") or "").strip()
        if not body or len(body) > MAX_MESSAGE:
            return jsonify(error="Message text is required and must be under 10,000 characters."), 422
        try:
            expected_version = int(payload.get("version"))
        except (TypeError, ValueError):
            expected_version = 0
        cipher, nonce = _encrypt(body.encode())
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if (
                not row
                or row["deleted_at"]
                or not _member(connection, row["conversation_id"], identity["owner_id"])
            ):
                abort(404)
            decision = _authorize(
                "chat.message.update",
                identity,
                AuthorizationFacts(contribution_owner_id=row["sender_id"]),
            )
            if not decision.allowed:
                abort(404)
            if expected_version < 1:
                return jsonify(error="Reload this message before editing it."), 400
            if int(row["version"]) != expected_version:
                return jsonify(
                    error="This message changed elsewhere. Reload it before editing.",
                    conflict=True,
                    latest_version=int(row["version"]),
                ), 409
            now = utcnow()
            changed = connection.execute(
                "UPDATE messages SET body_cipher=?,body_nonce=?,updated_at=?,version=version+1 "
                "WHERE id=? AND sender_id=? AND version=? AND deleted_at IS NULL",
                (
                    cipher,
                    nonce,
                    now,
                    message_id,
                    identity["owner_id"],
                    expected_version,
                ),
            )
            if not changed.rowcount:
                return jsonify(
                    error="This message changed elsewhere. Reload it before editing.",
                    conflict=True,
                ), 409
            connection.execute(
                "UPDATE conversations SET updated_at=?,version=version+1 WHERE id=?",
                (now, row["conversation_id"]),
            )
            saved = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="chat_message",
                object_id=message_id,
                action="update",
                before=row,
                after=saved,
            )
            result = _message_json(connection, saved, identity["owner_id"])
        return jsonify(ok=True, message=result)

    @blueprint.delete("/api/chat/messages/<int:message_id>")
    def delete_message(message_id):
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        try:
            expected_version = int(payload.get("version"))
        except (TypeError, ValueError):
            expected_version = 0
        now_value = datetime.now(timezone.utc)
        now = now_value.isoformat()
        purge_after = (now_value + timedelta(days=30)).isoformat()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if (
                not row
                or row["deleted_at"]
                or not _member(connection, row["conversation_id"], identity["owner_id"])
            ):
                abort(404)
            decision = _authorize(
                "chat.message.delete",
                identity,
                AuthorizationFacts(contribution_owner_id=row["sender_id"]),
            )
            if not decision.allowed:
                abort(404)
            if expected_version < 1:
                return jsonify(error="Reload this message before removing it."), 400
            if int(row["version"]) != expected_version:
                return jsonify(
                    error="This message changed elsewhere. Reload it before removing it.",
                    conflict=True,
                    latest_version=int(row["version"]),
                ), 409
            if _delivery_in_progress(connection, message_id=message_id):
                return _delivery_conflict()
            changed = connection.execute(
                "UPDATE messages SET deleted_at=?,deleted_by=?,purge_after=?,"
                "updated_at=?,version=version+1 "
                "WHERE id=? AND sender_id=? AND version=? AND deleted_at IS NULL",
                (
                    now,
                    identity["owner_id"],
                    purge_after,
                    now,
                    message_id,
                    identity["owner_id"],
                    expected_version,
                ),
            )
            if not changed.rowcount:
                return jsonify(
                    error="This message changed elsewhere. Reload it before removing it.",
                    conflict=True,
                ), 409
            connection.execute(
                "UPDATE conversations SET updated_at=?,version=version+1 WHERE id=?",
                (now, row["conversation_id"]),
            )
            saved = connection.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="chat_message",
                object_id=message_id,
                action="trash",
                before=row,
                after=saved,
            )
        return jsonify(ok=True, retained_until=purge_after)

    @blueprint.post("/api/chat/conversations/<conversation_id>/read")
    def mark_read(conversation_id):
        identity = _identity()
        try:
            message_id = max(
                int((request.get_json(silent=True) or {}).get("message_id") or 0), 0
            )
        except (TypeError, ValueError):
            return jsonify(error="Message position is invalid."), 422
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            members = frozenset(
                row["owner_id"]
                for row in connection.execute(
                    "SELECT owner_id FROM conversation_members "
                    "WHERE conversation_id=? AND left_at IS NULL",
                    (conversation_id,),
                )
            )
            if identity["owner_id"] not in members:
                abort(404)
            before = connection.execute(
                "SELECT * FROM conversation_reads WHERE conversation_id=? AND owner_id=?",
                (conversation_id, identity["owner_id"]),
            ).fetchone()
            decision = _authorize(
                "chat.read_state.update",
                identity,
                AuthorizationFacts(
                    member_ids=members,
                    personal_owner_id=before["owner_id"] if before else None,
                    allow_personal_state_create=before is None,
                ),
            )
            if not decision.allowed:
                abort(404)
            highest = connection.execute(
                "SELECT COALESCE(max(id),0) FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            message_id = min(message_id, highest)
            if before is not None and message_id <= int(before["last_message_id"]):
                return jsonify(ok=True, unchanged=True)
            connection.execute(
                """INSERT INTO conversation_reads
                   (conversation_id,owner_id,last_message_id,seen_at,version)
                   VALUES(?,?,?,?,1) ON CONFLICT(conversation_id,owner_id)
                   DO UPDATE SET
                     last_message_id=max(last_message_id,excluded.last_message_id),
                     seen_at=excluded.seen_at,
                     version=conversation_reads.version+1""",
                (conversation_id, identity["owner_id"], message_id, utcnow()),
            )
            after = connection.execute(
                "SELECT * FROM conversation_reads WHERE conversation_id=? AND owner_id=?",
                (conversation_id, identity["owner_id"]),
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="chat_read_state",
                object_id=f"{conversation_id}:{identity['owner_id']}",
                action="update",
                before=before,
                after=after,
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
            try:
                packed = _read_private(relative)
                data = _decrypt(packed[12:], packed[:12])
                name = "chat-photo.jpg"
                if kind == "original":
                    name = _decrypt(
                        row["original_name_cipher"], row["original_name_nonce"]
                    ).decode("utf-8", errors="replace")
            except (OSError, ValueError, InvalidTag):
                abort(404)
            mime = "image/jpeg" if kind == "preview" else row["mime_type"]
        return send_file(BytesIO(data), mimetype=mime, as_attachment=False, download_name=name, max_age=0)

    @blueprint.post("/api/chat/attachments/<attachment_id>/save-to-media")
    def save_to_media(attachment_id):
        identity = _identity()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN")
            row = attachment_or_404(connection, attachment_id, identity["owner_id"])
            active_members = frozenset(
                item["owner_id"]
                for item in connection.execute(
                    "SELECT owner_id FROM conversation_members "
                    "WHERE conversation_id=? AND left_at IS NULL",
                    (row["conversation_id"],),
                )
            )
            decision = _authorize(
                "chat.attachment.copy_to_media",
                identity,
                AuthorizationFacts(member_ids=active_members),
            )
            if not decision.allowed:
                abort(404)
            try:
                packed = _read_private(row["object_path"])
                data = _decrypt(packed[12:], packed[:12])
                name = _decrypt(
                    row["original_name_cipher"], row["original_name_nonce"]
                ).decode("utf-8", errors="replace")
            except (OSError, ValueError, InvalidTag):
                abort(404)
        incoming = Path(os.environ.get("PHOTO_DATA", "/data")) / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="chat-save-", suffix=Path(name).suffix, dir=incoming)
        result = None
        preserve_temporary = False
        try:
            with os.fdopen(fd, "wb") as stream: stream.write(data)
            result = canonical_ingest_media(
                staged_path=temporary, original_filename=name, mime_type=row["mime_type"],
                owner_user_id=identity["owner_id"], owner_name=identity["name"], visibility="private",
                ingestion_source="chat_save",
                authoritative_sha256=hashlib.sha256(data).hexdigest(),
                authoritative_size=len(data),
            )
        except BaseException as error:
            preserve_temporary = bool(
                getattr(error, "preserve_staged_media", False)
            )
            raise
        finally:
            if not preserve_temporary:
                Path(temporary).unlink(missing_ok=True)
        try:
            with connect(DB_PATH) as connection:
                connection.execute("BEGIN IMMEDIATE")
                _audit_mutation(
                    connection,
                    identity,
                    domain="chat_media_copy",
                    object_id=result["id"],
                    action="create",
                    before=None,
                    after={
                        "owner_id": identity["owner_id"],
                        "version": 1,
                        "deleted_at": None,
                    },
                )
        except BaseException:
            rollback_ingest_media(result["id"])
            raise
        return jsonify(ok=True, media_id=result["id"])

    @blueprint.post("/api/chat/push/web")
    def register_web_push():
        identity = _identity()
        payload = request.get_json(silent=True) or {}
        keys = payload.get("keys") or {}
        if (
            not isinstance(keys, dict)
            or not keys.get("p256dh")
            or not keys.get("auth")
        ):
            return jsonify(error="Invalid push subscription."), 422
        try:
            endpoint = resolve_push_endpoint(payload.get("endpoint")).endpoint
        except PushEndpointPolicyError:
            return jsonify(error="Invalid push subscription."), 422
        p256dh = str(keys["p256dh"])[:512]
        auth = str(keys["auth"])[:512]
        now = utcnow()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            matches = connection.execute(
                "SELECT * FROM push_subscriptions "
                "WHERE platform='web' AND endpoint=? ORDER BY id",
                (endpoint,),
            ).fetchall()
            conflict = next(
                (row for row in matches if row["owner_id"] != identity["owner_id"]),
                None,
            )
            before = next(
                (row for row in matches if row["owner_id"] == identity["owner_id"]),
                None,
            )
            decision = _authorize(
                "chat.push.web.update",
                identity,
                AuthorizationFacts(
                    personal_owner_id=(conflict or before)["owner_id"]
                    if conflict or before
                    else None,
                    allow_personal_state_create=not matches,
                ),
            )
            if not decision.allowed:
                if conflict is not None:
                    return jsonify(
                        error=(
                            "This browser notification endpoint is already bound to "
                            "another portal identity. Remove it there before reusing it."
                        ),
                        code="push_credential_conflict",
                    ), 409
                abort(403)
            if _delivery_in_progress(connection, owner_id=identity["owner_id"]):
                return _delivery_conflict()
            if before is None:
                credential_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO push_subscriptions
                       (id,owner_id,platform,endpoint,p256dh,auth,created_at,updated_at,version)
                       VALUES(?,?,?,?,?,?,?,?,1)""",
                    (
                        credential_id,
                        identity["owner_id"],
                        "web",
                        endpoint,
                        p256dh,
                        auth,
                        now,
                        now,
                    ),
                )
                action = "create"
            else:
                credential_id = before["id"]
                changed = connection.execute(
                    "UPDATE push_subscriptions SET p256dh=?,auth=?,updated_at=?,"
                    "version=version+1 WHERE id=? AND owner_id=? AND version=?",
                    (
                        p256dh,
                        auth,
                        now,
                        credential_id,
                        identity["owner_id"],
                        before["version"],
                    ),
                )
                if not changed.rowcount:
                    connection.rollback()
                    return jsonify(error="Notification settings changed. Try again."), 409
                action = "update"
            after = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id=?", (credential_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="push_credential",
                object_id=credential_id,
                action=action,
                before=before,
                after=after,
            )
        return jsonify(ok=True, credential_version=int(after["version"]))

    @blueprint.post("/api/chat/push/android")
    def register_android_push():
        identity = _identity()
        token = str((request.get_json(silent=True) or {}).get("token") or "").strip()
        if len(token) < 32 or len(token) > 4096:
            return jsonify(error="Invalid Android notification token."), 422
        now = utcnow()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            matches = connection.execute(
                "SELECT * FROM push_subscriptions "
                "WHERE platform='android' AND device_token=? ORDER BY id",
                (token,),
            ).fetchall()
            conflict = next(
                (row for row in matches if row["owner_id"] != identity["owner_id"]),
                None,
            )
            before = next(
                (row for row in matches if row["owner_id"] == identity["owner_id"]),
                None,
            )
            decision = _authorize(
                "chat.push.android.update",
                identity,
                AuthorizationFacts(
                    personal_owner_id=(conflict or before)["owner_id"]
                    if conflict or before
                    else None,
                    allow_personal_state_create=not matches,
                ),
            )
            if not decision.allowed:
                if conflict is not None:
                    return jsonify(
                        error=(
                            "This Android notification token is already bound to "
                            "another portal identity. Remove it there before reusing it."
                        ),
                        code="push_credential_conflict",
                    ), 409
                abort(403)
            if _delivery_in_progress(connection, owner_id=identity["owner_id"]):
                return _delivery_conflict()
            if before is None:
                credential_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO push_subscriptions
                       (id,owner_id,platform,device_token,created_at,updated_at,version)
                       VALUES(?,?,?,?,?,?,1)""",
                    (
                        credential_id,
                        identity["owner_id"],
                        "android",
                        token,
                        now,
                        now,
                    ),
                )
                action = "create"
            else:
                credential_id = before["id"]
                changed = connection.execute(
                    "UPDATE push_subscriptions SET updated_at=?,version=version+1 "
                    "WHERE id=? AND owner_id=? AND version=?",
                    (
                        now,
                        credential_id,
                        identity["owner_id"],
                        before["version"],
                    ),
                )
                if not changed.rowcount:
                    connection.rollback()
                    return jsonify(error="Notification settings changed. Try again."), 409
                action = "update"
            after = connection.execute(
                "SELECT * FROM push_subscriptions WHERE id=?", (credential_id,)
            ).fetchone()
            _audit_mutation(
                connection,
                identity,
                domain="push_credential",
                object_id=credential_id,
                action=action,
                before=before,
                after=after,
            )
        return jsonify(ok=True, credential_version=int(after["version"]))

    @blueprint.get("/api/chat/push/public-key")
    def web_push_public_key():
        _identity()
        return jsonify(public_key=os.environ.get("DAVID_PI_VAPID_PUBLIC_KEY", ""))

    app.register_blueprint(blueprint)
