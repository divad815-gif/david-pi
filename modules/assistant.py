"""Provider-neutral, read-only Assistant for David-Pi."""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from flask import jsonify, request

from .identity import current_device
from .platform import PLATFORM_DATA, connect, migrate, utcnow


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
WINDOWS_ENABLED = os.environ.get("ASSISTANT_WINDOWS_ENABLED", "false").lower() == "true"
SERVER_STATUS = Path(
    os.environ.get(
        "DAVID_PI_SERVER_STATUS",
        "/run/david-pi/server-status.json",
    )
)
MAX_QUESTION = 2000
MAX_CONTEXT_MESSAGES = 10
MAX_CONVERSATIONS = 100


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


def _migrate(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deleted_at TEXT
        );
        CREATE INDEX IF NOT EXISTS assistant_conversation_owner_idx
            ON conversations(owner_id, updated_at DESC);
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL,
            provider TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS assistant_message_conversation_idx
            ON messages(conversation_id, created_at, id);
        CREATE TABLE IF NOT EXISTS remote_tasks (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('inspect','plan','implement')),
            state TEXT NOT NULL,
            task TEXT NOT NULL,
            repository_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES remote_tasks(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            decision TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
            document_id UNINDEXED,
            title,
            body
        );
        CREATE TABLE IF NOT EXISTS provider_status (
            provider TEXT PRIMARY KEY,
            available INTEGER NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assistant_audit (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            intent TEXT NOT NULL,
            provider TEXT NOT NULL,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS assistant_audit_created_idx
            ON assistant_audit(created_at DESC);
        """
    )


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


def _owner() -> tuple[str, str]:
    actor = current_device()
    return (actor["owner_id"] or "unverified-home", actor["name"])


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


def _conversation(owner_id: str, conversation_id: str | None, question: str) -> str:
    now = utcnow()
    with connect(DB_PATH) as connection:
        if conversation_id:
            row = connection.execute(
                "SELECT id FROM conversations WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                (conversation_id, owner_id),
            ).fetchone()
            if not row:
                raise PermissionError("conversation_not_found")
            return row["id"]
        count = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE owner_id=? AND deleted_at IS NULL",
            (owner_id,),
        ).fetchone()[0]
        if count >= MAX_CONVERSATIONS:
            oldest = connection.execute(
                "SELECT id FROM conversations WHERE owner_id=? AND deleted_at IS NULL "
                "ORDER BY updated_at LIMIT 1",
                (owner_id,),
            ).fetchone()
            if oldest:
                connection.execute(
                    "UPDATE conversations SET deleted_at=? WHERE id=?",
                    (now, oldest["id"]),
                )
        conversation_id = uuid.uuid4().hex
        title = question[:60] + ("…" if len(question) > 60 else "")
        connection.execute(
            "INSERT INTO conversations(id,owner_id,title,created_at,updated_at) VALUES (?,?,?,?,?)",
            (conversation_id, owner_id, title, now, now),
        )
    return conversation_id


def _store_message(conversation_id: str, role: str, content: str, provider=None):
    now = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute(
            "INSERT INTO messages(id,conversation_id,role,content,provider,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, conversation_id, role, content, provider, now),
        )
        connection.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?",
            (now, conversation_id),
        )


def _recent_messages(owner_id: str, conversation_id: str) -> list[dict]:
    with connect(DB_PATH) as connection:
        rows = connection.execute(
            """
            SELECT m.role,m.content FROM messages m
            JOIN conversations c ON c.id=m.conversation_id
            WHERE c.id=? AND c.owner_id=? AND c.deleted_at IS NULL
            ORDER BY m.created_at DESC,m.id DESC LIMIT ?
            """,
            (conversation_id, owner_id, MAX_CONTEXT_MESSAGES),
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


def _provider_messages(owner_id: str, conversation_id: str) -> list[dict]:
    """Return bounded conversation context without granting remote Pi capabilities."""
    return _recent_messages(owner_id, conversation_id)[-MAX_CONTEXT_MESSAGES:]


def init_assistant(app, deterministic_answer):
    ubuntu = UbuntuBrokerProvider()
    windows = WindowsCodexProvider()

    @app.get("/api/assistant/providers")
    def assistant_providers():
        windows_status = windows.health()
        _record_provider_status(windows_status)
        return jsonify(
            providers=[
                {
                    "available": True,
                    "name": "David-Pi Rules",
                    "capabilities": [ProviderCapability.SERVER_DIAGNOSTICS.value],
                    "detail": "Fast deterministic household and server answers.",
                },
                asdict(windows_status),
                asdict(ubuntu.health()),
            ]
        )

    @app.get("/api/assistant/conversations")
    def assistant_conversations():
        owner_id, _ = _owner()
        limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT id,title,created_at,updated_at FROM conversations "
                "WHERE owner_id=? AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return jsonify(conversations=[dict(row) for row in rows])

    @app.get("/api/assistant/conversations/<conversation_id>")
    def assistant_conversation(conversation_id):
        owner_id, _ = _owner()
        with connect(DB_PATH) as connection:
            conversation = connection.execute(
                "SELECT id,title,created_at,updated_at FROM conversations "
                "WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                (conversation_id, owner_id),
            ).fetchone()
            if not conversation:
                return jsonify(error="Conversation not found."), 404
            rows = connection.execute(
                "SELECT id,role,content,provider,created_at FROM messages "
                "WHERE conversation_id=? ORDER BY created_at,id LIMIT 200",
                (conversation_id,),
            ).fetchall()
        return jsonify(conversation=dict(conversation), messages=[dict(row) for row in rows])

    @app.delete("/api/assistant/conversations/<conversation_id>")
    def assistant_delete_conversation(conversation_id):
        owner_id, _ = _owner()
        with connect(DB_PATH) as connection:
            changed = connection.execute(
                "UPDATE conversations SET deleted_at=? "
                "WHERE id=? AND owner_id=? AND deleted_at IS NULL",
                (utcnow(), conversation_id, owner_id),
            )
        if not changed.rowcount:
            return jsonify(error="Conversation not found."), 404
        return jsonify(ok=True)

    @app.get("/api/assistant/tasks")
    def assistant_tasks():
        owner_id, _ = _owner()
        with connect(DB_PATH) as connection:
            rows = connection.execute(
                "SELECT id,provider,mode,state,task,repository_id,created_at,updated_at "
                "FROM remote_tasks WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100",
                (owner_id,),
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
        owner_id, _ = _owner()
        try:
            conversation_id = _conversation(
                owner_id,
                str(data.get("conversation_id") or "").strip() or None,
                question,
            )
        except PermissionError:
            return jsonify(error="Conversation not found."), 404
        _store_message(conversation_id, "user", question)

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
        elif CODE_RE.search(question):
            try:
                result = windows.submit(
                    _provider_messages(owner_id, conversation_id),
                    mode="coding_readonly",
                )
                answer, intent, provider = (
                    result["answer"],
                    "windows_coding_readonly",
                    "Windows Codex",
                )
            except AssistantBusy:
                answer, intent, provider = (
                    "The Windows coding helper is busy. Please try again shortly.",
                    "windows_busy",
                    "Windows Codex",
                )
            except (AssistantUnavailable, OSError, ValueError, json.JSONDecodeError):
                answer = (
                    "The Windows coding helper is offline. Nothing was changed or submitted."
                )
                intent, provider = "windows_unavailable", "Windows Codex"
        elif REMOTE_RE.search(question) or WINDOWS_RE.search(question):
            forwarded = WINDOWS_RE.sub("", question).strip() or question
            try:
                result = windows.submit(
                    _provider_messages(owner_id, conversation_id)[:-1]
                    + [{"role": "user", "content": forwarded}],
                    mode="general",
                )
                answer, intent, provider = (
                    result["answer"],
                    "windows_general",
                    "Windows Codex",
                )
            except AssistantBusy:
                answer, intent, provider = (
                    "The Windows helper is busy. Please try again shortly.",
                    "windows_busy",
                    "Windows Codex",
                )
            except (AssistantUnavailable, OSError, ValueError, json.JSONDecodeError):
                answer = (
                    "The Windows helper is offline. David-Pi's deterministic answers still work."
                )
                intent, provider = "windows_unavailable", "Windows Codex"
        elif DIAGNOSTIC_RE.search(question):
            try:
                answer, intent = deterministic_answer(question)
            except (OSError, ValueError, IndexError, sqlite3.Error):
                answer, intent = (
                    "That read-only diagnostic information is unavailable right now.",
                    "diagnostic_unavailable",
                )
            provider = "David-Pi diagnostics"
        else:
            try:
                result = windows.submit(
                    _provider_messages(owner_id, conversation_id),
                    mode="general",
                )
                answer, intent, provider = (
                    result["answer"],
                    "windows_general",
                    "Windows Codex",
                )
            except AssistantBusy:
                answer, intent, provider = (
                    "The Windows helper is busy. Please try again shortly.",
                    "windows_busy",
                    "Windows Codex",
                )
            except (AssistantUnavailable, OSError, ValueError, json.JSONDecodeError):
                answer, intent, provider = (
                    "That question is outside David-Pi's deterministic answers, and the "
                    "Windows helper is offline. Nothing was submitted or changed.",
                    "windows_unavailable",
                    "Windows Codex",
                )

        _store_message(conversation_id, "assistant", answer, provider)
        _audit(owner_id, intent, provider, "answered")
        return jsonify(
            ok=True,
            answer=answer,
            intent=intent,
            provider=provider,
            conversation_id=conversation_id,
        )
