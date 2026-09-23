"""Provider-neutral, read-only Assistant for David-Pi."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from flask import current_app, g, jsonify, request

from .access_control import IDENTITY_ROLES, normalize_login
from .content_policy import (
    Actor,
    AuthorizationFacts,
    decide_transaction_authorization,
    load_route_policy,
)
from .identity import current_device
from .installation import get_installation, display_name
from .platform import (
    PLATFORM_DATA,
    connect,
    emit_outbox_event,
    initialize_data_foundation,
    migrate,
    record_mutation_audit,
    utcnow,
)


ASSISTANT_DATA = Path(
    os.environ.get(
        "DAVID_PI_ASSISTANT_DATA",
        PLATFORM_DATA / "assistant",
    )
)
DB_PATH = ASSISTANT_DATA / "assistant.db"
WINDOWS_BRIDGE_SOCKET = os.environ.get(
    "DAVID_PI_WINDOWS_SOCKET",
    "/run/david-pi-windows/windows-broker.sock",
)
WINDOWS_ENABLED = False  # External AI bridges are outside the portable release.
SERVER_STATUS = Path(
    os.environ.get(
        "DAVID_PI_SERVER_STATUS",
        "/run/david-pi/server-status.json",
    )
)
MAX_QUESTION = 2000
MAX_CONTEXT_MESSAGES = 10
MAX_CONVERSATIONS = 100
def household_names():
    config = get_installation()
    return {m["login"]: m["name"] for m in config["members"]} if config else {}

KNOWN_SENDER_NAMES = household_names()



class ProviderCapability(str, Enum):
    CHAT = "chat"
    REASONING = "reasoning"
    SERVER_DIAGNOSTICS = "server_diagnostics"
    REPOSITORY_READ = "repository_read"
    REPOSITORY_WRITE = "repository_write"
    COMMAND_EXECUTION = "command_execution"
    TEST_EXECUTION = "test_execution"
    DIFFS = "diffs"
    SESSION_CONTINUATION = "session_continuation"


@dataclass(frozen=True)
class ProviderStatus:
    available: bool
    name: str
    capabilities: tuple[str, ...]
    detail: str | None = None


class AssistantProvider:
    def health(self) -> ProviderStatus:
        raise NotImplementedError

    def submit(self, messages: list[dict]) -> dict:
        raise NotImplementedError

    def cancel(self, task_id: str) -> bool:
        return False


class UbuntuBrokerProvider(AssistantProvider):
    def health(self) -> ProviderStatus:
        return ProviderStatus(
            available=False,
            name="Ubuntu Local AI",
            capabilities=(),
            detail="The Ubuntu broker is not connected yet.",
        )

    def submit(self, messages: list[dict]) -> dict:
        raise AssistantUnavailable("ubuntu_provider_disabled")


class WindowsCodexProvider(AssistantProvider):
    """Use the allowlisted host bridge; credentials never enter the container."""

    def health(self) -> ProviderStatus:
        available = WINDOWS_ENABLED and Path(WINDOWS_BRIDGE_SOCKET).exists()
        if available:
            try:
                result = self._request({"action": "health"}, timeout=5)
                available = bool(result.get("ok") and result.get("available"))
            except (OSError, ValueError, json.JSONDecodeError):
                available = False
        return ProviderStatus(
            available=available,
            name="Windows Codex",
            capabilities=(
                ProviderCapability.CHAT.value,
                ProviderCapability.REASONING.value,
                ProviderCapability.REPOSITORY_READ.value,
                ProviderCapability.DIFFS.value,
            ) if available else (),
            detail=None if available else "The Windows fallback is offline.",
        )

    def _request(self, payload: dict, timeout: int = 620) -> dict:
        wire = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(wire) > 32_000:
            raise AssistantUnavailable("windows_request_too_large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(WINDOWS_BRIDGE_SOCKET)
            client.sendall(wire)
            response = bytearray()
            while len(response) <= 65_536:
                chunk = client.recv(8192)
                if not chunk:
                    break
                response.extend(chunk)
        if len(response) > 65_536:
            raise AssistantUnavailable("windows_response_too_large")
        return json.loads(response.decode("utf-8"))

    def submit(self, messages: list[dict], mode: str = "general") -> dict:
        if not self.health().available:
            raise AssistantUnavailable("windows_provider_unavailable")
        result = self._request(
            {
                "action": "run",
                "mode": mode,
                "messages": messages[-MAX_CONTEXT_MESSAGES:],
            }
        )
        if not result.get("ok"):
            if result.get("error") in {"busy", "queue_full"}:
                raise AssistantBusy(result["error"])
            raise AssistantUnavailable(result.get("error", "windows_provider_failed"))
        answer = str(result.get("answer", "")).strip()
        if not answer:
            raise AssistantUnavailable("empty_windows_response")
        return {"answer": answer[:12_000], "model": result.get("model", "Codex")}


class AssistantUnavailable(RuntimeError):
    pass


class AssistantBusy(RuntimeError):
    pass


class ConversationLimitReached(RuntimeError):
    pass


class ConversationChanged(RuntimeError):
    def __init__(self, latest_version: int):
        super().__init__("conversation_changed")
        self.latest_version = latest_version


def _migrate(connection):
    # ``sqlite3.Connection.executescript`` commits any transaction already in
    # progress before running its script.  Assistant startup is serialized by
    # platform.migrate's BEGIN IMMEDIATE, so execute each statement separately
    # to keep that lock for the entire schema inspection/backfill transaction.
    # Direct maintenance callers receive the same serialization guarantee.
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    schema_statements = (
        """CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_by TEXT,
            purge_after TEXT,
            version INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE INDEX IF NOT EXISTS assistant_conversation_owner_idx
            ON conversations(owner_id, updated_at DESC)""",
        """CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            provider TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS assistant_message_conversation_idx
            ON messages(conversation_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS remote_tasks (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('inspect','plan','implement')),
            state TEXT NOT NULL,
            task TEXT NOT NULL,
            repository_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES remote_tasks(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            document_id UNINDEXED,
            title,
            body
        )""",
        """CREATE TABLE IF NOT EXISTS provider_status (
            provider TEXT PRIMARY KEY,
            available INTEGER NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS assistant_audit (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            intent TEXT NOT NULL,
            provider TEXT NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS assistant_audit_created_idx
            ON assistant_audit(created_at DESC)""",
    )
    for statement in schema_statements:
        connection.execute(statement)

    def add_column(table, column, statement):
        current = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column in current:
            return
        try:
            connection.execute(statement)
        except sqlite3.OperationalError as error:
            verified = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "duplicate column" not in str(error).lower() or column not in verified:
                raise

    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
    }
    additions = {
        "deleted_by": "ALTER TABLE conversations ADD COLUMN deleted_by TEXT",
        "purge_after": "ALTER TABLE conversations ADD COLUMN purge_after TEXT",
        "version": (
            "ALTER TABLE conversations ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        ),
    }
    for column, statement in additions.items():
        if column not in columns:
            add_column("conversations", column, statement)
    message_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(messages)")
    }
    message_additions = {
        "sender_id": "ALTER TABLE messages ADD COLUMN sender_id TEXT",
        "sender_name": "ALTER TABLE messages ADD COLUMN sender_name TEXT",
    }
    for column, statement in message_additions.items():
        if column not in message_columns:
            add_column("messages", column, statement)
    # Before this migration a conversation was writable only by its creator, so
    # the creator is the exact provenance for every historical user message.
    connection.execute(
        """UPDATE messages
           SET sender_id=(
               SELECT owner_id FROM conversations
               WHERE conversations.id=messages.conversation_id
           )
           WHERE role='user' AND NULLIF(TRIM(sender_id),'') IS NULL"""
    )
    for principal_id, display_name in household_names().items():
        connection.execute(
            """UPDATE messages SET sender_name=?
               WHERE role='user' AND LOWER(TRIM(sender_id))=?
                 AND NULLIF(TRIM(sender_name),'') IS NULL""",
            (display_name, principal_id),
        )
    initialize_data_foundation(connection)


ASSISTANT_DATA.mkdir(parents=True, exist_ok=True)
migrate(DB_PATH, _migrate)


UNSAFE_RE = re.compile(
    r"\b(restart|reboot|delete|remove|install|upgrade|deploy|modify|change|run "
    r"(?:this |a )?(?:shell )?command|docker (?:start|stop|restart)|firewall)\b",
    re.I,
)
CODE_RE = re.compile(
    r"\b(repository|source code|code review|implement|patch|diff|run the tests|"
    r"write code|fix (?:a |the )?bug|debug|opencode|codex|pagination)\b",
    re.I,
)
REMOTE_RE = re.compile(
    r"\b(latest news|today'?s news|long pdf|analy[sz]e (?:this )?(?:image|document)|"
    r"image understanding|browse the (?:web|internet))\b",
    re.I,
)
WINDOWS_RE = re.compile(r"^(?:ask windows|use windows|ask codex)\s*[:,\-]?\s*", re.I)
DIAGNOSTIC_RE = re.compile(
    r"\b(storage|space|drive|mount|health|healthy|status|backup|memory|ram|"
    r"temperature|thermal|throttl|service|pi[ -]?hole|tailscale|uptime|media tab)\b",
    re.I,
)


_TEST_IDENTITY_ROLES = {
    "david@example.test": "admin",
    "diana@example.test": "household",
}


def _actor() -> tuple[Actor, str]:
    """Return only a verified, allowlisted human identity for saved content."""
    identity = current_device()
    principal_id = normalize_login(identity.get("owner_id"))
    role = IDENTITY_ROLES.get(principal_id)
    request_access = getattr(g, "portal_access", None)
    if isinstance(request_access, dict):
        role = request_access.get("role") or role
    if current_app.testing:
        role = _TEST_IDENTITY_ROLES.get(principal_id, role)
    return Actor(principal_id=principal_id, role=role, kind="human"), identity["name"]


def _allowed_actor() -> tuple[Actor | None, str]:
    actor, name = _actor()
    if not actor.principal_id or actor.role not in {"admin", "household"}:
        return None, name
    return actor, name


def _household_display_name(principal_id: str | None) -> str:
    """Return a non-sensitive household label without exposing a login address."""
    return household_names().get(normalize_login(principal_id), "Household member")


def _authorize(route_id: str, actor: Actor, owner_id: str | None = None):
    policy = load_route_policy().by_id[route_id]
    return decide_transaction_authorization(
        policy,
        actor,
        AuthorizationFacts(owner_id=owner_id, legacy=not bool(owner_id)),
    )


def _metadata_digest(row) -> str | None:
    if row is None:
        return None
    import hashlib

    payload = {
        "owner_id": row["owner_id"],
        "version": int(row["version"]),
        "deleted": bool(row["deleted_at"]),
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _audit_mutation(connection, actor: Actor, conversation_id: str, action: str, before, after):
    """Append content-neutral audit and outbox metadata in the write transaction."""
    occurred_at = utcnow()
    record_mutation_audit(
        connection,
        event_id=uuid.uuid4().hex,
        actor_id=actor.principal_id,
        domain="assistant_conversation",
        object_id=conversation_id,
        action=action,
        request_id=uuid.uuid4().hex,
        before_digest=_metadata_digest(before),
        after_digest=_metadata_digest(after),
        occurred_at=occurred_at,
    )
    if after is not None:
        emit_outbox_event(
            connection,
            domain="assistant_conversation",
            object_id=conversation_id,
            event_type=action,
            object_version=int(after["version"]),
            payload_digest=_metadata_digest(after),
            occurred_at=occurred_at,
        )


def _resource_gate() -> tuple[bool, str | None]:
    sentinel = Path(os.environ.get("DAVID_PI_DATA_SENTINEL", "/data/.david-pi-storage"))
    if not sentinel.is_file():
        return False, "The local assistant is unavailable because the external drive is not verified."
    try:
        status = json.loads(SERVER_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False, "The local assistant is waiting for a fresh server-health snapshot."
    subsystems = status.get("subsystems", {})
    drive = subsystems.get("external_drive", {})
    thermal = subsystems.get("temperature_power", {})
    portal = subsystems.get("portal", {})
    jobs = subsystems.get("background_jobs", {})
    if drive.get("state") == "critical" or portal.get("state") == "critical":
        return False, "The local assistant is paused while David-Pi needs attention."
    details = thermal.get("details", {})
    temperature = details.get("temperature_c")
    if isinstance(temperature, (int, float)) and temperature >= 78:
        return False, "The local assistant is paused because the Pi is too warm."
    if details.get("active_throttling"):
        return False, "The local assistant is paused because the Pi is throttling."
    job_details = jobs.get("details", {})
    if job_details.get("slideshow_active"):
        return False, "The local assistant is temporarily unavailable while David-Pi renders media."
    return True, None


def _conversation(
    actor: Actor,
    conversation_id: str | None,
    question: str,
) -> tuple[str, int]:
    """Create/continue a conversation and persist its user message atomically."""
    now = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if conversation_id:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if not row:
                raise PermissionError("conversation_not_found")
            decision = _authorize("assistant.prompt.create", actor)
            if not decision.allowed:
                raise PermissionError("identity_not_allowed")
            before = row
            changed = connection.execute(
                "UPDATE conversations SET updated_at=?, version=version+1 "
                "WHERE id=? AND version=? AND deleted_at IS NULL",
                (now, conversation_id, row["version"]),
            )
            if not changed.rowcount:
                raise sqlite3.OperationalError("conversation_changed")
            action = "message_append"
        else:
            decision = _authorize("assistant.prompt.create", actor)
            if not decision.allowed:
                raise PermissionError("identity_not_allowed")
            count = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL",
            ).fetchone()[0]
            if count >= MAX_CONVERSATIONS:
                raise ConversationLimitReached("conversation_limit_reached")
            conversation_id = uuid.uuid4().hex
            title = question[:60] + ("…" if len(question) > 60 else "")
            connection.execute(
                "INSERT INTO conversations"
                "(id,owner_id,title,created_at,updated_at,version) VALUES (?,?,?,?,?,1)",
                (conversation_id, actor.principal_id, title, now, now),
            )
            before = None
            action = "create"
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,provider,created_at,"
            "sender_id,sender_name) VALUES (?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex, conversation_id, "user", question, None, now,
                actor.principal_id,
                _household_display_name(actor.principal_id),
            ),
        )
        after = connection.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        _audit_mutation(connection, actor, conversation_id, action, before, after)
    return conversation_id, int(after["version"])


def _store_message(
    actor: Actor,
    conversation_id: str,
    role: str,
    content: str,
    provider=None,
    *,
    expected_version: int,
):
    now = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL",
            (conversation_id,),
        ).fetchone()
        if before is None:
            return None
        if int(before["version"]) != expected_version:
            raise ConversationChanged(int(before["version"]))
        changed = connection.execute(
            "UPDATE conversations SET updated_at=?,version=version+1 "
            "WHERE id=? AND version=? AND deleted_at IS NULL",
            (now, conversation_id, expected_version),
        )
        if not changed.rowcount:
            latest = connection.execute(
                "SELECT version FROM conversations WHERE id=? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if latest is None:
                return None
            raise ConversationChanged(int(latest["version"]))
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,provider,created_at,"
            "sender_id,sender_name) VALUES (?,?,?,?,?,?,NULL,NULL)",
            (uuid.uuid4().hex, conversation_id, role, content, provider, now),
        )
        after = connection.execute(
            "SELECT * FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        _audit_mutation(connection, actor, conversation_id, "message_append", before, after)
    return int(after["version"])


def _recent_messages(conversation_id: str) -> list[dict]:
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT m.role,m.content FROM messages m
            JOIN conversations c ON c.id=m.conversation_id
            WHERE c.id=? AND c.deleted_at IS NULL
            ORDER BY m.created_at DESC,m.id DESC LIMIT ?
            """,
            (conversation_id, MAX_CONTEXT_MESSAGES),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def _safe_snapshot_context() -> str:
    try:
        payload = json.loads(SERVER_STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return "Current diagnostic snapshot is unavailable."
    allowed = {}
    for name in (
        "portal",
        "external_drive",
        "storage",
        "backups",
        "temperature_power",
        "tailscale",
        "pihole",
        "background_jobs",
        "services",
        "updates",
    ):
        item = payload.get("subsystems", {}).get(name, {})
        allowed[name] = {
            "state": item.get("state", "unavailable"),
            "summary": str(item.get("summary", ""))[:120],
            "updated_at": item.get("updated_at"),
        }
    return json.dumps(allowed, separators=(",", ":"))


def _knowledge_context(question: str) -> str:
    terms = " ".join(re.findall(r"[a-z0-9_-]{3,}", question.lower())[:12])
    if not terms:
        return ""
    try:
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT title,body FROM knowledge_fts WHERE knowledge_fts MATCH ? "
                "ORDER BY bm25(knowledge_fts) LIMIT 2",
                (terms,),
            ).fetchall()
    except sqlite3.OperationalError:
        return ""
    return "\n\n".join(f"{row['title']}: {row['body'][:700]}" for row in rows)


def seed_knowledge(documents: list[tuple[str, str, str]]) -> None:
    with connect(DB_PATH) as connection:
        connection.execute("DELETE FROM knowledge_fts")
        connection.executemany(
            "INSERT INTO knowledge_fts(document_id,title,body) VALUES (?,?,?)",
            documents,
        )


def _record_provider_status(status: ProviderStatus) -> None:
    with connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO provider_status(provider,available,detail,checked_at)
            VALUES (?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
                available=excluded.available,
                detail=excluded.detail,
                checked_at=excluded.checked_at
            """,
            (status.name, int(status.available), status.detail, utcnow()),
        )


def _audit(owner_id: str, intent: str, provider: str, outcome: str) -> None:
    """Store routing metadata only; prompts and answers stay out of the audit log."""
    with connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT INTO assistant_audit
                (id,owner_id,event_type,intent,provider,outcome,created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                uuid.uuid4().hex,
                owner_id,
                "assistant_request",
                intent[:80],
                provider[:80],
                outcome[:40],
                utcnow(),
            ),
        )


def _provider_messages(conversation_id: str) -> list[dict]:
    """Return bounded conversation context without granting remote Pi capabilities."""
    return _recent_messages(conversation_id)[-MAX_CONTEXT_MESSAGES:]


def init_assistant(app, deterministic_answer):
    ubuntu = UbuntuBrokerProvider()
    windows = WindowsCodexProvider()

    @app.before_request
    def require_allowlisted_assistant_page():
        if request.path != "/assistant":
            return None
        actor, _ = _allowed_actor()
        if actor is None:
            return "Open David-Pi through an approved private Tailscale account.", 403
        return None

    @app.get("/api/assistant/providers")
    def assistant_providers():
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        windows_status = windows.health()
        return jsonify(
            providers=[
                {
                    "available": True,
                    "name": "David-Pi Rules",
                    "capabilities": [ProviderCapability.SERVER_DIAGNOSTICS.value],
                    "detail": "Fast deterministic household and server answers.",
                },

            ]
        )

    @app.get("/api/assistant/conversations")
    def assistant_conversations():
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT id,owner_id,title,created_at,updated_at,version FROM conversations "
                "WHERE deleted_at IS NULL ORDER BY updated_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return jsonify(
            conversations=[
                {
                    **{key: row[key] for key in row.keys() if key != "owner_id"},
                    "creator_name": _household_display_name(row["owner_id"]),
                }
                for row in rows
            ]
        )

    @app.get("/api/assistant/conversations/<conversation_id>")
    def assistant_conversation(conversation_id):
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        with connect(DB_PATH) as connection:
            conversation = connection.execute(
                "SELECT id,owner_id,title,created_at,updated_at,version FROM conversations "
                "WHERE id=? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                return jsonify(error="Conversation not found."), 404
            rows = connection.execute(
                "SELECT id,role,content,provider,created_at,sender_id,sender_name "
                "FROM messages WHERE conversation_id=? ORDER BY created_at,id LIMIT 200",
                (conversation_id,),
            ).fetchall()
        conversation_payload = {
            key: conversation[key]
            for key in conversation.keys()
            if key != "owner_id"
        }
        conversation_payload["creator_name"] = _household_display_name(
            conversation["owner_id"]
        )
        return jsonify(
            conversation=conversation_payload,
            messages=[
                {
                    **{
                        key: row[key]
                        for key in row.keys()
                        if key != "sender_id"
                    },
                    "mine": bool(
                        row["role"] == "user"
                        and row["sender_id"] == actor.principal_id
                    ),
                }
                for row in rows
            ],
        )

    @app.delete("/api/assistant/conversations/<conversation_id>")
    def assistant_delete_conversation(conversation_id):
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        data = request.get_json(silent=True) or {}
        try:
            expected_version = int(data.get("version"))
        except (TypeError, ValueError):
            expected_version = 0
        if expected_version < 1:
            return jsonify(error="Reload this conversation before removing it."), 400
        now = datetime.now(timezone.utc)
        deleted_at = now.isoformat()
        purge_after = (now + timedelta(days=30)).isoformat()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL",
                (conversation_id,),
            ).fetchone()
            if before is None:
                return jsonify(error="Conversation not found."), 404
            decision = _authorize("assistant.conversation.delete", actor)
            if not decision.allowed:
                return jsonify(error="Conversation not found."), 404
            if int(before["version"]) != expected_version:
                return jsonify(
                    error="This conversation changed elsewhere. Reload it before removing it.",
                    conflict=True,
                    latest_version=int(before["version"]),
                ), 409
            changed = connection.execute(
                "UPDATE conversations SET deleted_at=?,deleted_by=?,purge_after=?,"
                "updated_at=?,version=version+1 "
                "WHERE id=? AND version=? AND deleted_at IS NULL",
                (
                    deleted_at,
                    actor.principal_id,
                    purge_after,
                    deleted_at,
                    conversation_id,
                    expected_version,
                ),
            )
            if not changed.rowcount:
                return jsonify(
                    error="This conversation changed elsewhere. Reload it before removing it.",
                    conflict=True,
                ), 409
            after = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            _audit_mutation(connection, actor, conversation_id, "trash", before, after)
        return jsonify(
            ok=True,
            retained_until=purge_after,
            message=(
                "Conversation moved out of shared household history and retained "
                "for at least 30 days."
            ),
        )

    @app.get("/api/assistant/tasks")
    def assistant_tasks():
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT id,provider,mode,state,task,repository_id,created_at,updated_at "
                "FROM remote_tasks WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100",
                (actor.principal_id,),
            ).fetchall()
        return jsonify(tasks=[dict(row) for row in rows])

    @app.post("/api/assistant")
    def assistant_api():
        data = request.get_json(silent=True) or {}
        question = " ".join(str(data.get("question", "")).split()).strip()
        if not question:
            return jsonify(error="Ask David-Pi a question first."), 400
        if len(question) > MAX_QUESTION:
            return jsonify(error="Please keep the question under 2,000 characters."), 422
        actor, _ = _allowed_actor()
        if actor is None:
            return jsonify(error="Open David-Pi through an approved private Tailscale account."), 403
        try:
            conversation_id, pending_version = _conversation(
                actor,
                str(data.get("conversation_id") or "").strip() or None,
                question,
            )
        except ConversationLimitReached:
            return jsonify(
                error=(
                    "The household has 100 active conversations. Remove one from "
                    "shared history before starting another; nothing was deleted "
                    "automatically."
                ),
                code="conversation_limit_reached",
            ), 409
        except PermissionError:
            return jsonify(error="Conversation not found."), 404

        try:
            deterministic_result = deterministic_answer(question)
        except (OSError, ValueError, IndexError, sqlite3.Error):
            deterministic_result = (None, "unsupported")

        if deterministic_result[1] != "unsupported":
            answer, intent = deterministic_result
            provider = "David-Pi diagnostics"
        elif UNSAFE_RE.search(question):
            answer = (
                "I can inspect and explain read-only information, but I cannot change, "
                "restart, delete, install, deploy, or run commands on David-Pi."
            )
            intent, provider = "refused", "Policy"
        else:
            answer = "I can explain this server's storage, health, backups, and library. Ask about one of those topics; outside AI and computer control are not enabled."
            intent, provider = "unsupported", "Local server help"

        try:
            conversation_version = _store_message(
                actor,
                conversation_id,
                "assistant",
                answer,
                provider,
                expected_version=pending_version,
            )
        except ConversationChanged as exc:
            return jsonify(
                error=(
                    "This conversation changed while the answer was being prepared. "
                    "Reload it before continuing."
                ),
                code="conversation_changed",
                conflict=True,
                latest_version=exc.latest_version,
            ), 409
        if conversation_version is None:
            return jsonify(
                error="This conversation was removed while the answer was being prepared.",
                code="conversation_removed",
            ), 409
        _audit(actor.principal_id, intent, provider, "answered")
        return jsonify(
            ok=True,
            answer=answer,
            intent=intent,
            provider=provider,
            conversation_id=conversation_id,
            conversation_version=conversation_version,
        )
