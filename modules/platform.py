import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


PLATFORM_DATA = Path(os.environ.get("DAVID_PI_PLATFORM_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "platform"))
PLATFORM_DATA.mkdir(parents=True, exist_ok=True)
LOGGER = logging.getLogger(__name__)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path, *, uri=False):
    connection = sqlite3.connect(path, timeout=30, uri=uri)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        try:
            yield connection
        except BaseException:
            # Preserve the application error even if best-effort rollback or
            # descriptor cleanup also fails.  The body never receives a false
            # success response in this branch.
            try:
                connection.rollback()
            except Exception:
                LOGGER.exception("SQLite rollback failed while preserving an earlier error")
            raise
        else:
            try:
                connection.commit()
            except BaseException:
                try:
                    connection.rollback()
                except Exception:
                    LOGGER.exception("SQLite rollback failed after commit failure")
                raise
    finally:
        try:
            connection.close()
        except Exception:
            # commit() returning is the SQLite transaction boundary.  A later
            # close error is a resource-cleanup problem, not evidence that the
            # committed mutation failed.  Propagating it makes callers retry a
            # successful write and, for file uploads, used to delete the now
            # committed object.  Log only content-neutral diagnostics.
            LOGGER.exception("SQLite connection close failed after transaction outcome was decided")


def migrate(path, migration):
    for attempt in range(10):
        try:
            with connect(path) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                # Serialize schema inspection and ALTER TABLE work across
                # Gunicorn workers. Without this, two fresh workers can both
                # observe a missing column and race to add it.
                connection.execute("BEGIN IMMEDIATE")
                migration(connection)
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))


class MigrationChecksumConflict(RuntimeError):
    """A deployed migration version does not match the requested definition."""


class FoundationConflict(RuntimeError):
    """An idempotency key was reused for different metadata."""


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value: str, label: str, maximum: int = 200) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _digest(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{label} must be a SHA-256 digest")
    return normalized


@dataclass(frozen=True)
class MigrationStep:
    """One immutable, checksummed schema migration."""

    version: int
    name: str
    checksum: str
    operation: Callable[[sqlite3.Connection], None]

    @classmethod
    def from_sql(cls, version: int, name: str, statements: Iterable[str]):
        statement_tuple = tuple(str(statement).strip() for statement in statements)
        if not statement_tuple or any(not statement for statement in statement_tuple):
            raise ValueError("A SQL migration needs at least one non-empty statement")
        definition = _canonical_json(
            {"version": version, "name": name, "statements": statement_tuple}
        )

        def apply(connection: sqlite3.Connection) -> None:
            for statement in statement_tuple:
                connection.execute(statement)

        return cls(version, name, _sha256(definition), apply)


def migration_step(
    version: int,
    name: str,
    definition: str,
    operation: Callable[[sqlite3.Connection], None],
) -> MigrationStep:
    """Build a callable migration whose reviewed definition is checksummed."""
    name = _identifier(name, "Migration name")
    definition = str(definition or "").strip()
    if not definition:
        raise ValueError("Migration definition is required")
    checksum = _sha256(
        _canonical_json({"version": version, "name": name, "definition": definition})
    )
    return MigrationStep(version, name, checksum, operation)


def initialize_data_foundation(connection: sqlite3.Connection) -> None:
    """Create metadata-only foundation tables without changing existing rows."""
    statements = (
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            domain TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version > 0),
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            release_id TEXT,
            backup_set TEXT,
            PRIMARY KEY(domain, version)
        )""",
        """CREATE TABLE IF NOT EXISTS mutation_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            actor_id TEXT NOT NULL,
            domain TEXT NOT NULL,
            object_id TEXT NOT NULL,
            action TEXT NOT NULL,
            before_digest TEXT,
            after_digest TEXT,
            request_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            rollback_of TEXT REFERENCES mutation_audit(event_id)
        )""",
        """CREATE INDEX IF NOT EXISTS mutation_audit_object_idx
            ON mutation_audit(domain, object_id, occurred_at, id)""",
        """CREATE TABLE IF NOT EXISTS domain_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            domain TEXT NOT NULL,
            object_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            object_version INTEGER NOT NULL CHECK(object_version >= 0),
            payload_digest TEXT,
            occurred_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS domain_outbox_replay_idx
            ON domain_outbox(domain, id)""",
        """CREATE TRIGGER IF NOT EXISTS mutation_audit_no_update
        BEFORE UPDATE ON mutation_audit
        BEGIN
            SELECT RAISE(ABORT, 'mutation_audit is append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS mutation_audit_no_delete
        BEFORE DELETE ON mutation_audit
        BEGIN
            SELECT RAISE(ABORT, 'mutation_audit is append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS domain_outbox_no_update
        BEFORE UPDATE ON domain_outbox
        BEGIN
            SELECT RAISE(ABORT, 'domain_outbox is append-only');
        END""",
        """CREATE TRIGGER IF NOT EXISTS domain_outbox_no_delete
        BEFORE DELETE ON domain_outbox
        BEGIN
            SELECT RAISE(ABORT, 'domain_outbox is append-only');
        END""",
    )
    for statement in statements:
        connection.execute(statement)


def apply_domain_migrations(
    path: Path | str,
    domain: str,
    steps: Iterable[MigrationStep],
    *,
    release_id: str | None = None,
    backup_set: str | None = None,
) -> tuple[int, ...]:
    """Apply a domain's new migrations once in one concurrent-safe transaction."""
    domain = _identifier(domain, "Migration domain", 100)
    ordered = tuple(sorted(steps, key=lambda step: step.version))
    versions = [step.version for step in ordered]
    if any(version <= 0 for version in versions) or len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique positive integers")
    for step in ordered:
        _identifier(step.name, "Migration name")
        _digest(step.checksum, "Migration checksum")
        if not callable(step.operation):
            raise TypeError("Migration operation must be callable")
    release_id = _identifier(release_id, "Release id") if release_id else None
    backup_set = _identifier(backup_set, "Backup set") if backup_set else None

    for attempt in range(10):
        try:
            applied = []
            with connect(path) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                initialize_data_foundation(connection)
                for step in ordered:
                    current = connection.execute(
                        "SELECT name, checksum FROM schema_migrations WHERE domain=? AND version=?",
                        (domain, step.version),
                    ).fetchone()
                    if current:
                        if current["checksum"] != step.checksum or current["name"] != step.name:
                            raise MigrationChecksumConflict(
                                f"Migration {domain}:{step.version} conflicts with the applied checksum"
                            )
                        continue
                    step.operation(connection)
                    connection.execute(
                        """INSERT INTO schema_migrations
                           (domain,version,name,checksum,applied_at,release_id,backup_set)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            domain, step.version, step.name, step.checksum, utcnow(),
                            release_id, backup_set,
                        ),
                    )
                    applied.append(step.version)
            return tuple(applied)
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))
    return ()


def record_mutation_audit(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    actor_id: str,
    domain: str,
    object_id: str,
    action: str,
    request_id: str,
    before_digest: str | None = None,
    after_digest: str | None = None,
    occurred_at: str | None = None,
    rollback_of: str | None = None,
) -> bool:
    """Append content-neutral mutation metadata; exact retries are idempotent."""
    initialize_data_foundation(connection)
    event_id = _identifier(event_id, "Event id")
    current = connection.execute(
        """SELECT event_id,actor_id,domain,object_id,action,before_digest,after_digest,
                  request_id,occurred_at,rollback_of
           FROM mutation_audit WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    values = (
        event_id,
        _identifier(actor_id, "Actor id", 320),
        _identifier(domain, "Audit domain", 100),
        _identifier(object_id, "Object id"),
        _identifier(action, "Audit action", 100),
        _digest(before_digest, "Before digest"),
        _digest(after_digest, "After digest"),
        _identifier(request_id, "Request id"),
        occurred_at or (current["occurred_at"] if current else utcnow()),
        _identifier(rollback_of, "Rollback event id") if rollback_of else None,
    )
    if current:
        if tuple(current) != values:
            raise FoundationConflict("Audit event id was reused with different metadata")
        return False
    connection.execute(
        """INSERT INTO mutation_audit
           (event_id,actor_id,domain,object_id,action,before_digest,after_digest,
            request_id,occurred_at,rollback_of)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    return True


@dataclass(frozen=True)
class OutboxEvent:
    id: int
    event_key: str
    domain: str
    object_id: str
    event_type: str
    object_version: int
    payload_digest: str | None
    occurred_at: str


def outbox_event_key(domain: str, object_id: str, event_type: str, object_version: int) -> str:
    metadata = {
        "domain": _identifier(domain, "Outbox domain", 100),
        "object_id": _identifier(object_id, "Object id"),
        "event_type": _identifier(event_type, "Event type", 100),
        "object_version": int(object_version),
    }
    if metadata["object_version"] < 0:
        raise ValueError("Object version cannot be negative")
    return _sha256(_canonical_json(metadata))


def emit_outbox_event(
    connection: sqlite3.Connection,
    *,
    domain: str,
    object_id: str,
    event_type: str,
    object_version: int,
    payload_digest: str | None = None,
    occurred_at: str | None = None,
) -> OutboxEvent:
    """Append or return the deterministic event for one object version."""
    initialize_data_foundation(connection)
    event_key = outbox_event_key(domain, object_id, event_type, object_version)
    current = connection.execute(
        "SELECT * FROM domain_outbox WHERE event_key=?", (event_key,)
    ).fetchone()
    payload_digest = _digest(payload_digest, "Payload digest")
    occurred_at = occurred_at or (current["occurred_at"] if current else utcnow())
    expected = (
        domain.strip(), object_id.strip(), event_type.strip(), int(object_version),
        payload_digest, occurred_at,
    )
    if current:
        actual = tuple(current[key] for key in (
            "domain", "object_id", "event_type", "object_version", "payload_digest", "occurred_at"
        ))
        if actual != expected:
            raise FoundationConflict("Outbox event key was reused with different metadata")
        return OutboxEvent(**dict(current))
    cursor = connection.execute(
        """INSERT INTO domain_outbox
           (event_key,domain,object_id,event_type,object_version,payload_digest,occurred_at)
           VALUES(?,?,?,?,?,?,?)""",
        (event_key, *expected),
    )
    row = connection.execute("SELECT * FROM domain_outbox WHERE id=?", (cursor.lastrowid,)).fetchone()
    return OutboxEvent(**dict(row))


def read_outbox(
    connection: sqlite3.Connection,
    *,
    after_id: int = 0,
    domains: Iterable[str] | None = None,
    limit: int = 500,
) -> list[OutboxEvent]:
    """Read a bounded deterministic replay page ordered by immutable sequence."""
    limit = min(max(int(limit), 1), 1000)
    parameters: list[object] = [max(int(after_id), 0)]
    condition = "id > ?"
    selected = tuple(dict.fromkeys(_identifier(domain, "Outbox domain", 100) for domain in (domains or ())))
    if selected:
        condition += f" AND domain IN ({','.join('?' for _ in selected)})"
        parameters.extend(selected)
    parameters.append(limit)
    rows = connection.execute(
        f"SELECT * FROM domain_outbox WHERE {condition} ORDER BY id LIMIT ?", parameters
    ).fetchall()
    return [OutboxEvent(**dict(row)) for row in rows]
