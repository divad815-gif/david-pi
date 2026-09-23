"""Projection primitives for David-Pi signed snapshot manifests.

The backup workflow owns manifest creation and file verification. This module
authenticates its schema-v3 metadata and projects only database paths plus
content-neutral aggregate tree summaries; it never reads or changes a backup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Mapping

from .platform import PLATFORM_DATA, utcnow


DB_PATH = PLATFORM_DATA / "protection.db"
MANIFEST_SCHEMA_VERSION = 3
SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
HEX_256 = re.compile(r"[0-9a-f]{64}\Z")
FILE_MODE = re.compile(r"[0-7]{4}\Z")
MAX_DATABASES = 1000
TREE_KINDS = ("content", "configuration")
TREE_SUMMARY_KEYS = {
    "file_count", "directory_count", "symlink_count", "logical_bytes",
    "unique_inode_count", "unique_inode_bytes", "tree_sha256",
}
DATABASE_RECORD_KEYS = {
    "path", "byte_size", "mode", "uid", "gid", "sha256", "quick_check",
    "foreign_key_errors",
}


class ManifestValidationError(ValueError):
    """A signed manifest is malformed, unsafe, or unauthentic."""


class ManifestConflict(RuntimeError):
    """A snapshot id was reused for different authenticated metadata."""


@dataclass(frozen=True)
class TreeSummary:
    file_count: int
    directory_count: int
    symlink_count: int
    logical_bytes: int
    unique_inode_count: int
    unique_inode_bytes: int
    tree_sha256: str


@dataclass(frozen=True)
class ValidatedManifest:
    snapshot_id: str
    created_at: str
    started_at: str
    completed_at: str
    source_fs_uuid: str
    backup_fs_uuid: str
    source_st_dev: int
    backup_st_dev: int
    portal_image: str
    database_count: int
    payload_sha256: str
    databases: tuple[tuple[str, int, str, int, int, str, str, int], ...]
    database_tree: TreeSummary
    content: TreeSummary
    configuration: TreeSummary


def canonical_manifest_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize exactly as the schema-v3 backup manifest producer does."""
    try:
        return json.dumps(
            dict(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ManifestValidationError("Snapshot manifest is not canonical JSON") from error


def _key(signing_key: bytes) -> bytes:
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise ManifestValidationError("Snapshot manifest signing key must contain at least 32 bytes")
    return signing_key


def _timestamp(value, label: str) -> str:
    raw = value if isinstance(value, str) else ""
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise ManifestValidationError(
            f"{label} must be a calendar-valid canonical UTC timestamp"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != raw:
        raise ManifestValidationError(f"{label} must round-trip as canonical UTC")
    return raw


def _digest(value, label: str) -> str:
    normalized = str(value or "").lower()
    if not HEX_256.fullmatch(normalized):
        raise ManifestValidationError(f"{label} must be a SHA-256 digest")
    return normalized


def _integer(value, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestValidationError(f"{label} must be a non-negative integer")
    return value


def _label(value, label: str, maximum: int = 500) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ManifestValidationError(f"{label} is invalid")
    return normalized


def _relative_path(value) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
        or len(raw) > 500
    ):
        raise ManifestValidationError("Database path must be a canonical relative POSIX path")
    return raw


def _file_mode(value) -> str:
    if not isinstance(value, str) or not FILE_MODE.fullmatch(value):
        raise ManifestValidationError("Database mode must be a four-digit octal string")
    return value


def _tree_summary(value, label: str) -> TreeSummary:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{label} tree summary is invalid")
    _exact_keys(value, TREE_SUMMARY_KEYS, f"{label} tree summary")
    summary = TreeSummary(
        file_count=_integer(value.get("file_count"), f"{label} file count"),
        directory_count=_integer(
            value.get("directory_count"), f"{label} directory count"
        ),
        symlink_count=_integer(value.get("symlink_count"), f"{label} symlink count"),
        logical_bytes=_integer(value.get("logical_bytes"), f"{label} logical bytes"),
        unique_inode_count=_integer(
            value.get("unique_inode_count"), f"{label} unique inode count"
        ),
        unique_inode_bytes=_integer(
            value.get("unique_inode_bytes"), f"{label} unique inode bytes"
        ),
        tree_sha256=_digest(value.get("tree_sha256"), f"{label} tree digest"),
    )
    if summary.unique_inode_count > summary.file_count:
        raise ManifestValidationError(f"{label} unique inode count exceeds file count")
    if summary.unique_inode_bytes > summary.logical_bytes:
        raise ManifestValidationError(f"{label} unique inode bytes exceed logical bytes")
    return summary


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ManifestValidationError(f"{label} schema is invalid")


def validate_signed_manifest(
    manifest: Mapping[str, object], signing_key: bytes
) -> ValidatedManifest:
    """Authenticate and strictly validate the backup workflow's schema-v3 manifest."""
    if not isinstance(manifest, Mapping):
        raise ManifestValidationError("Snapshot manifest must be an object")
    signed = dict(manifest)
    integrity = signed.pop("integrity", None)
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "hmac-sha256":
        raise ManifestValidationError("Snapshot manifest has no supported signature")
    _exact_keys(
        integrity, {"algorithm", "payload_sha256", "signature"},
        "Snapshot manifest integrity",
    )
    if type(signed.get("schema_version")) is not int or signed["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ManifestValidationError("Snapshot manifest schema version is unsupported")
    encoded = canonical_manifest_bytes(signed)
    payload_sha256 = _digest(integrity.get("payload_sha256"), "Manifest payload digest")
    signature = _digest(integrity.get("signature"), "Manifest signature")
    expected_payload = hashlib.sha256(encoded).hexdigest()
    expected_signature = hmac.new(_key(signing_key), encoded, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(payload_sha256, expected_payload):
        raise ManifestValidationError("Snapshot manifest payload digest does not match")
    if not hmac.compare_digest(signature, expected_signature):
        raise ManifestValidationError("Snapshot manifest signature does not match")

    _exact_keys(
        signed,
        {
            "schema_version", "created_at", "snapshot_id", "window", "source",
            "backup", "release", "databases", "database_tree", "content",
            "configuration",
        },
        "Snapshot manifest",
    )

    snapshot_id = str(signed.get("snapshot_id") or "")
    if not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ManifestValidationError("Snapshot id is invalid")
    created_at = _timestamp(signed.get("created_at"), "Manifest creation")
    window = signed.get("window")
    if not isinstance(window, Mapping) or window.get("writers_quiesced") is not True:
        raise ManifestValidationError("Snapshot window must record quiesced writers")
    _exact_keys(window, {"started_at", "completed_at", "writers_quiesced"}, "Snapshot window")
    started_at = _timestamp(window.get("started_at"), "Snapshot start")
    completed_at = _timestamp(window.get("completed_at"), "Snapshot completion")
    if completed_at < started_at:
        raise ManifestValidationError("Snapshot completion precedes its start")

    source = signed.get("source")
    backup = signed.get("backup")
    if not isinstance(source, Mapping) or not isinstance(backup, Mapping):
        raise ManifestValidationError("Snapshot source and backup metadata are required")
    _exact_keys(source, {"device", "filesystem_uuid", "st_dev"}, "Source filesystem")
    _exact_keys(backup, {"device", "filesystem_uuid", "st_dev"}, "Backup filesystem")
    _label(source.get("device"), "Source device")
    _label(backup.get("device"), "Backup device")
    source_uuid = _label(source.get("filesystem_uuid"), "Source filesystem id", 128)
    backup_uuid = _label(backup.get("filesystem_uuid"), "Backup filesystem id", 128)
    source_st_dev = _integer(source.get("st_dev"), "Source st_dev")
    backup_st_dev = _integer(backup.get("st_dev"), "Backup st_dev")
    if hmac.compare_digest(source_uuid, backup_uuid):
        raise ManifestValidationError("Backup must use a different filesystem")
    if source_st_dev == backup_st_dev:
        raise ManifestValidationError("Backup must use a different st_dev")
    release = signed.get("release")
    if not isinstance(release, Mapping):
        raise ManifestValidationError("Snapshot release metadata is required")
    _exact_keys(release, {"portal_image"}, "Snapshot release")
    portal_image = _label(release.get("portal_image"), "Portal image")

    raw_databases = signed.get("databases")
    if not isinstance(raw_databases, list) or not raw_databases or len(raw_databases) > MAX_DATABASES:
        raise ManifestValidationError("Snapshot database inventory is invalid")
    databases = []
    seen_paths = set()
    for item in raw_databases:
        if not isinstance(item, Mapping):
            raise ManifestValidationError("Snapshot database record is invalid")
        _exact_keys(item, DATABASE_RECORD_KEYS, "Snapshot database record")
        path = _relative_path(item.get("path"))
        if path in seen_paths:
            raise ManifestValidationError("Snapshot contains duplicate database paths")
        seen_paths.add(path)
        quick_check = item.get("quick_check")
        foreign_key_errors = item.get("foreign_key_errors")
        if quick_check != "ok" or type(foreign_key_errors) is not int or foreign_key_errors != 0:
            raise ManifestValidationError("Every snapshot database must pass integrity checks")
        databases.append(
            (
                path,
                _integer(item.get("byte_size"), "Database size"),
                _file_mode(item.get("mode")),
                _integer(item.get("uid"), "Database uid"),
                _integer(item.get("gid"), "Database gid"),
                _digest(item.get("sha256"), "Database digest"),
                quick_check,
                foreign_key_errors,
            )
        )

    database_tree = _tree_summary(signed.get("database_tree"), "Database")
    if database_tree.file_count != len(databases) or database_tree.symlink_count != 0:
        raise ManifestValidationError("Database tree inventory is not exact")

    return ValidatedManifest(
        snapshot_id=snapshot_id,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        source_fs_uuid=source_uuid,
        backup_fs_uuid=backup_uuid,
        source_st_dev=source_st_dev,
        backup_st_dev=backup_st_dev,
        portal_image=portal_image,
        database_count=len(databases),
        payload_sha256=payload_sha256,
        databases=tuple(sorted(databases)),
        database_tree=database_tree,
        content=_tree_summary(signed.get("content"), "Content"),
        configuration=_tree_summary(signed.get("configuration"), "Configuration"),
    )


def initialize_protection(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS protection_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            source_fs_uuid TEXT NOT NULL,
            backup_fs_uuid TEXT NOT NULL,
            source_st_dev INTEGER,
            backup_st_dev INTEGER,
            portal_image TEXT NOT NULL,
            database_count INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            projected_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS protection_snapshots_time_idx
            ON protection_snapshots(completed_at DESC,snapshot_id DESC)""",
        """CREATE TABLE IF NOT EXISTS protection_trees (
            snapshot_id TEXT NOT NULL REFERENCES protection_snapshots(snapshot_id) ON DELETE CASCADE,
            tree_kind TEXT NOT NULL CHECK(tree_kind IN ('content','configuration')),
            file_count INTEGER NOT NULL,
            directory_count INTEGER NOT NULL,
            symlink_count INTEGER NOT NULL,
            logical_bytes INTEGER NOT NULL,
            unique_inode_count INTEGER NOT NULL,
            unique_inode_bytes INTEGER NOT NULL,
            tree_sha256 TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,tree_kind)
        )""",
        """CREATE TABLE IF NOT EXISTS protected_databases (
            snapshot_id TEXT NOT NULL REFERENCES protection_snapshots(snapshot_id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            mode TEXT NOT NULL,
            uid INTEGER NOT NULL,
            gid INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            quick_check TEXT NOT NULL CHECK(quick_check='ok'),
            foreign_key_errors INTEGER NOT NULL CHECK(foreign_key_errors=0),
            PRIMARY KEY(snapshot_id,path)
        )""",
        """CREATE TABLE IF NOT EXISTS protection_database_trees (
            snapshot_id TEXT PRIMARY KEY REFERENCES protection_snapshots(snapshot_id) ON DELETE CASCADE,
            file_count INTEGER NOT NULL,
            directory_count INTEGER NOT NULL,
            symlink_count INTEGER NOT NULL CHECK(symlink_count=0),
            logical_bytes INTEGER NOT NULL,
            unique_inode_count INTEGER NOT NULL,
            unique_inode_bytes INTEGER NOT NULL,
            tree_sha256 TEXT NOT NULL
        )""",
    )
    for statement in statements:
        connection.execute(statement)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(protection_snapshots)")
    }
    if "source_st_dev" not in columns:
        connection.execute("ALTER TABLE protection_snapshots ADD COLUMN source_st_dev INTEGER")
    if "backup_st_dev" not in columns:
        connection.execute("ALTER TABLE protection_snapshots ADD COLUMN backup_st_dev INTEGER")


def project_signed_manifest(
    connection: sqlite3.Connection,
    manifest: Mapping[str, object],
    signing_key: bytes,
    *,
    projected_at: str | None = None,
) -> bool:
    """Atomically project one authenticated snapshot; exact retries are idempotent."""
    validated = validate_signed_manifest(manifest, signing_key)
    initialize_protection(connection)
    current = connection.execute(
        "SELECT payload_sha256 FROM protection_snapshots WHERE snapshot_id=?",
        (validated.snapshot_id,),
    ).fetchone()
    if current:
        if current["payload_sha256"] != validated.payload_sha256:
            raise ManifestConflict("Snapshot id was reused for different signed metadata")
        return False
    projected_at = projected_at or utcnow()
    connection.execute("SAVEPOINT project_manifest")
    try:
        connection.execute(
            """INSERT INTO protection_snapshots
               (snapshot_id,created_at,started_at,completed_at,source_fs_uuid,backup_fs_uuid,
                source_st_dev,backup_st_dev,portal_image,database_count,payload_sha256,projected_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                validated.snapshot_id,
                validated.created_at,
                validated.started_at,
                validated.completed_at,
                validated.source_fs_uuid,
                validated.backup_fs_uuid,
                validated.source_st_dev,
                validated.backup_st_dev,
                validated.portal_image,
                validated.database_count,
                validated.payload_sha256,
                projected_at,
            ),
        )
        for kind in TREE_KINDS:
            tree = getattr(validated, kind)
            connection.execute(
                """INSERT INTO protection_trees
                   (snapshot_id,tree_kind,file_count,directory_count,symlink_count,
                    logical_bytes,unique_inode_count,unique_inode_bytes,tree_sha256)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    validated.snapshot_id,
                    kind,
                    tree.file_count,
                    tree.directory_count,
                    tree.symlink_count,
                    tree.logical_bytes,
                    tree.unique_inode_count,
                    tree.unique_inode_bytes,
                    tree.tree_sha256,
                ),
            )
        connection.executemany(
            """INSERT INTO protected_databases
               (snapshot_id,path,byte_size,mode,uid,gid,sha256,quick_check,foreign_key_errors)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ((validated.snapshot_id, *record) for record in validated.databases),
        )
        tree = validated.database_tree
        connection.execute(
            """INSERT INTO protection_database_trees
               (snapshot_id,file_count,directory_count,symlink_count,logical_bytes,
                unique_inode_count,unique_inode_bytes,tree_sha256)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                validated.snapshot_id, tree.file_count, tree.directory_count,
                tree.symlink_count, tree.logical_bytes, tree.unique_inode_count,
                tree.unique_inode_bytes, tree.tree_sha256,
            ),
        )
        connection.execute("RELEASE SAVEPOINT project_manifest")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT project_manifest")
        connection.execute("RELEASE SAVEPOINT project_manifest")
        raise
    return True


def protection_summary(connection: sqlite3.Connection) -> dict:
    """Return content-neutral coverage totals for status/UI consumers."""
    initialize_protection(connection)
    latest = connection.execute(
        "SELECT * FROM protection_snapshots ORDER BY completed_at DESC,snapshot_id DESC LIMIT 1"
    ).fetchone()
    if not latest:
        return {"state": "unavailable", "latest_snapshot": None}
    tree_rows = list(connection.execute(
        "SELECT * FROM protection_trees WHERE snapshot_id=? ORDER BY tree_kind",
        (latest["snapshot_id"],),
    ))
    trees = {
        row["tree_kind"]: {
            "file_count": row["file_count"],
            "directory_count": row["directory_count"],
            "symlink_count": row["symlink_count"],
            "logical_bytes": row["logical_bytes"],
            "unique_inode_count": row["unique_inode_count"],
            "unique_inode_bytes": row["unique_inode_bytes"],
            "tree_sha256": row["tree_sha256"],
        }
        for row in tree_rows
    }
    database_tree = connection.execute(
        "SELECT * FROM protection_database_trees WHERE snapshot_id=?",
        (latest["snapshot_id"],),
    ).fetchone()
    if database_tree:
        trees["database"] = {
            key: database_tree[key]
            for key in (
                "file_count", "directory_count", "symlink_count", "logical_bytes",
                "unique_inode_count", "unique_inode_bytes", "tree_sha256",
            )
        }
    database_rows = connection.execute(
        "SELECT COUNT(*) FROM protected_databases WHERE snapshot_id=?",
        (latest["snapshot_id"],),
    ).fetchone()[0]
    evidence_complete = (
        latest["source_st_dev"] is not None
        and latest["backup_st_dev"] is not None
        and latest["source_st_dev"] != latest["backup_st_dev"]
        and latest["source_fs_uuid"] != latest["backup_fs_uuid"]
        and latest["database_count"] > 0
        and set(trees) == {"content", "configuration", "database"}
        and len(tree_rows) == 2
        and database_rows == latest["database_count"]
        and database_tree is not None
        and database_tree["file_count"] == latest["database_count"]
        and database_tree["symlink_count"] == 0
    )
    return {
        "state": "verified" if evidence_complete else "unverified_legacy",
        "verification_reason": None if evidence_complete else "schema_v3_tree_evidence_missing",
        "latest_snapshot": {
            "snapshot_id": latest["snapshot_id"],
            "started_at": latest["started_at"],
            "completed_at": latest["completed_at"],
            "database_count": latest["database_count"],
            "payload_sha256": latest["payload_sha256"],
            "portal_image": latest["portal_image"],
            "writers_quiesced": True,
            "trees": trees,
        },
    }
