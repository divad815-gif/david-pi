"""Rebuildable, ACL-aware search catalog primitives.

This module owns no source content and registers no HTTP routes. Callers project
canonical module rows into this database and may discard/rebuild it at any time.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from .platform import OutboxEvent, PLATFORM_DATA, utcnow


DB_PATH = PLATFORM_DATA / "catalog.db"
VISIBILITIES = frozenset({"shared", "private"})
WORD = re.compile(r"[^\W_]+", re.UNICODE)


class CatalogConflict(RuntimeError):
    """A projection tried to rewrite an already-seen object version."""


@dataclass(frozen=True)
class CatalogItem:
    domain: str
    item_id: str
    owner_id: str | None
    visibility: str
    title: str
    subtitle: str = ""
    search_text: str = ""
    sort_time: str = ""
    deleted_at: str | None = None
    object_version: int = 0
    metadata: Mapping[str, object] | None = None
    updated_at: str = ""


def _text(value, label: str, maximum: int, *, required: bool = False) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > maximum or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _search_text(value) -> str:
    # Source modules commonly store ingredients, notes, and descriptions as
    # multi-line text. Collapse benign whitespace before indexing while still
    # rejecting control characters that should never reach FTS query storage.
    normalized = " ".join(str(value or "").split())
    if len(normalized) > 16 * 1024 or any(ord(character) < 32 for character in normalized):
        raise ValueError("Catalog search text is invalid")
    return normalized


def _normalize(item: CatalogItem) -> tuple:
    domain = _text(item.domain, "Catalog domain", 100, required=True)
    item_id = _text(item.item_id, "Catalog item id", 200, required=True)
    owner_id = _text(item.owner_id, "Catalog owner id", 320) or None
    visibility = str(item.visibility or "").strip().lower()
    if visibility not in VISIBILITIES:
        raise ValueError("Catalog visibility must be shared or private")
    if visibility == "private" and not owner_id:
        raise ValueError("Private catalog items require an owner")
    version = int(item.object_version)
    if version < 0:
        raise ValueError("Catalog object version cannot be negative")
    metadata_json = json.dumps(
        dict(item.metadata or {}), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    if len(metadata_json.encode("utf-8")) > 16 * 1024:
        raise ValueError("Catalog metadata is too large")
    return (
        domain,
        item_id,
        owner_id,
        visibility,
        _text(item.title, "Catalog title", 500, required=True),
        _text(item.subtitle, "Catalog subtitle", 1000),
        _search_text(item.search_text),
        _text(item.sort_time, "Catalog sort time", 100),
        _text(item.deleted_at, "Catalog deletion time", 100) or None,
        version,
        metadata_json,
        _text(item.updated_at, "Catalog update time", 100),
    )


def initialize_catalog(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS catalog_items (
            domain TEXT NOT NULL,
            item_id TEXT NOT NULL,
            owner_id TEXT,
            visibility TEXT NOT NULL CHECK(visibility IN ('shared','private')),
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL DEFAULT '',
            search_text TEXT NOT NULL DEFAULT '',
            sort_time TEXT NOT NULL DEFAULT '',
            deleted_at TEXT,
            object_version INTEGER NOT NULL CHECK(object_version >= 0),
            metadata_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(domain,item_id)
        )""",
        """CREATE INDEX IF NOT EXISTS catalog_shared_sort_idx
            ON catalog_items(visibility,deleted_at,sort_time DESC,domain,item_id)""",
        """CREATE INDEX IF NOT EXISTS catalog_owner_sort_idx
            ON catalog_items(owner_id,deleted_at,sort_time DESC,domain,item_id)""",
        """CREATE TABLE IF NOT EXISTS catalog_checkpoints (
            domain TEXT PRIMARY KEY,
            last_event_id INTEGER NOT NULL CHECK(last_event_id >= 0),
            index_version INTEGER NOT NULL CHECK(index_version > 0),
            indexed_at TEXT NOT NULL
        )""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS catalog_fts USING fts5(
            domain UNINDEXED,
            item_id UNINDEXED,
            title,
            subtitle,
            search_text,
            tokenize='unicode61 remove_diacritics 2'
        )""",
    )
    for statement in statements:
        connection.execute(statement)


def _row_values(row: sqlite3.Row) -> tuple:
    return tuple(
        row[key]
        for key in (
            "domain", "item_id", "owner_id", "visibility", "title", "subtitle",
            "search_text", "sort_time", "deleted_at", "object_version", "metadata_json",
            "updated_at",
        )
    )


def upsert_catalog_item(connection: sqlite3.Connection, item: CatalogItem) -> bool:
    """Project a monotonic source version; exact replays are idempotent."""
    initialize_catalog(connection)
    values = _normalize(item)
    current = connection.execute(
        "SELECT rowid,* FROM catalog_items WHERE domain=? AND item_id=?", values[:2]
    ).fetchone()
    if current:
        if current["object_version"] > values[9]:
            return False
        if current["object_version"] == values[9]:
            if _row_values(current) != values:
                raise CatalogConflict(
                    f"Catalog item {values[0]}:{values[1]} changed without a new version"
                )
            return False
        rowid = current["rowid"]
        connection.execute("DELETE FROM catalog_fts WHERE rowid=?", (rowid,))
        connection.execute(
            """UPDATE catalog_items SET owner_id=?,visibility=?,title=?,subtitle=?,search_text=?,
                      sort_time=?,deleted_at=?,object_version=?,metadata_json=?,updated_at=?
               WHERE rowid=?""",
            (*values[2:], rowid),
        )
    else:
        cursor = connection.execute(
            """INSERT INTO catalog_items
               (domain,item_id,owner_id,visibility,title,subtitle,search_text,sort_time,
                deleted_at,object_version,metadata_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )
        rowid = cursor.lastrowid
    connection.execute(
        """INSERT INTO catalog_fts(rowid,domain,item_id,title,subtitle,search_text)
           VALUES(?,?,?,?,?,?)""",
        (rowid, values[0], values[1], values[4], values[5], values[6]),
    )
    return True


def remove_catalog_item(connection: sqlite3.Connection, domain: str, item_id: str) -> bool:
    """Remove only a derivative catalog row when its canonical source is absent."""
    initialize_catalog(connection)
    keys = (
        _text(domain, "Catalog domain", 100, required=True),
        _text(item_id, "Catalog item id", 200, required=True),
    )
    current = connection.execute(
        "SELECT rowid FROM catalog_items WHERE domain=? AND item_id=?", keys
    ).fetchone()
    if not current:
        return False
    connection.execute("DELETE FROM catalog_fts WHERE rowid=?", (current["rowid"],))
    connection.execute("DELETE FROM catalog_items WHERE rowid=?", (current["rowid"],))
    return True


def rebuild_catalog(
    connection: sqlite3.Connection,
    items: Iterable[CatalogItem],
    *,
    checkpoints: Mapping[str, int] | None = None,
    index_version: int = 1,
    indexed_at: str | None = None,
) -> int:
    """Atomically replace the derivative catalog in stable key order."""
    initialize_catalog(connection)
    index_version = int(index_version)
    if index_version <= 0:
        raise ValueError("Catalog index version must be positive")
    normalized_items = sorted(items, key=lambda item: (item.domain, item.item_id))
    keys = [(item.domain.strip(), item.item_id.strip()) for item in normalized_items]
    if len(keys) != len(set(keys)):
        raise ValueError("Catalog rebuild contains duplicate item keys")
    indexed_at = indexed_at or utcnow()
    connection.execute("SAVEPOINT catalog_rebuild")
    try:
        connection.execute("DELETE FROM catalog_fts")
        connection.execute("DELETE FROM catalog_items")
        connection.execute("DELETE FROM catalog_checkpoints")
        for item in normalized_items:
            upsert_catalog_item(connection, item)
        for domain, last_event_id in sorted((checkpoints or {}).items()):
            connection.execute(
                """INSERT INTO catalog_checkpoints(domain,last_event_id,index_version,indexed_at)
                   VALUES(?,?,?,?)""",
                (
                    _text(domain, "Catalog domain", 100, required=True),
                    max(int(last_event_id), 0), index_version, indexed_at,
                ),
            )
        connection.execute("RELEASE SAVEPOINT catalog_rebuild")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT catalog_rebuild")
        connection.execute("RELEASE SAVEPOINT catalog_rebuild")
        raise
    return len(normalized_items)


def replay_catalog_events(
    connection: sqlite3.Connection,
    events: Iterable[OutboxEvent],
    resolver: Callable[[str, str], CatalogItem | None],
    *,
    index_version: int = 1,
    indexed_at: str | None = None,
) -> int:
    """Replay immutable outbox events in sequence using canonical source rows."""
    initialize_catalog(connection)
    ordered = sorted(events, key=lambda event: event.id)
    if len({event.id for event in ordered}) != len(ordered):
        raise ValueError("Catalog replay contains duplicate event ids")
    indexed_at = indexed_at or utcnow()
    applied = 0
    connection.execute("SAVEPOINT catalog_replay")
    try:
        for event in ordered:
            checkpoint = connection.execute(
                "SELECT last_event_id FROM catalog_checkpoints WHERE domain=?", (event.domain,)
            ).fetchone()
            if checkpoint and event.id <= checkpoint["last_event_id"]:
                continue
            item = resolver(event.domain, event.object_id)
            if item is None:
                remove_catalog_item(connection, event.domain, event.object_id)
            else:
                if item.domain != event.domain or item.item_id != event.object_id:
                    raise CatalogConflict("Catalog resolver returned the wrong canonical object")
                if int(item.object_version) < event.object_version:
                    raise CatalogConflict("Catalog source version is older than its outbox event")
                upsert_catalog_item(connection, item)
            connection.execute(
                """INSERT INTO catalog_checkpoints(domain,last_event_id,index_version,indexed_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                     last_event_id=excluded.last_event_id,
                     index_version=excluded.index_version,
                     indexed_at=excluded.indexed_at""",
                (event.domain, event.id, int(index_version), indexed_at),
            )
            applied += 1
        connection.execute("RELEASE SAVEPOINT catalog_replay")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT catalog_replay")
        connection.execute("RELEASE SAVEPOINT catalog_replay")
        raise
    return applied


def _fts_query(value: str) -> str:
    words = WORD.findall(str(value or "").casefold())[:12]
    return " AND ".join(f'"{word}"*' for word in words)


def search_catalog(
    connection: sqlite3.Connection,
    query: str,
    *,
    actor_id: str | None = None,
    domains: Iterable[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return only shared or actor-owned active results in deterministic order."""
    initialize_catalog(connection)
    actor_id = _text(actor_id, "Catalog actor id", 320) or None
    selected = tuple(dict.fromkeys(
        _text(domain, "Catalog domain", 100, required=True) for domain in (domains or ())
    ))
    limit = min(max(int(limit), 1), 100)
    conditions = ["i.deleted_at IS NULL"]
    parameters: list[object] = []
    if actor_id:
        conditions.append("(i.visibility='shared' OR i.owner_id=?)")
        parameters.append(actor_id)
    else:
        conditions.append("i.visibility='shared'")
    if selected:
        conditions.append(f"i.domain IN ({','.join('?' for _ in selected)})")
        parameters.extend(selected)
    match = _fts_query(query)
    columns = (
        "i.domain,i.item_id,i.owner_id,i.visibility,i.title,i.subtitle,i.sort_time,"
        "i.object_version,i.metadata_json"
    )
    if match:
        rows = connection.execute(
            f"""SELECT {columns},bm25(catalog_fts) AS score
                FROM catalog_fts JOIN catalog_items i ON i.rowid=catalog_fts.rowid
                WHERE catalog_fts MATCH ? AND {' AND '.join(conditions)}
                ORDER BY score,i.sort_time DESC,i.domain,i.item_id LIMIT ?""",
            (match, *parameters, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            f"""SELECT {columns},0.0 AS score FROM catalog_items i
                WHERE {' AND '.join(conditions)}
                ORDER BY i.sort_time DESC,i.domain,i.item_id LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        results.append(item)
    return results
