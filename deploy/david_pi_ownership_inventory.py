#!/usr/bin/env python3
"""Create a deterministic, content-neutral ownership inventory from clone databases.

The command is intentionally read-only. It accepts explicit DOMAIN=PATH inputs,
rejects databases with live WAL sidecars, opens immutable SQLite copies, and emits
counts only. It never selects titles, bodies, filenames, paths, URLs, or object bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True)
class ResourceSpec:
    table: str
    owner_columns: tuple[str, ...] = ("owner_id",)
    legacy_actor_columns: tuple[str, ...] = ()
    object_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationshipSpec:
    table: str
    parent_table: str
    parent_key: str
    parent_foreign_key: str
    child_table: str | None = None
    child_key: str = "id"
    child_foreign_key: str | None = None


DOMAIN_RESOURCES = {
    "photos": (
        ResourceSpec("photos", object_columns=("stored_path", "preview_name", "thumb_name", "playback_name")),
        ResourceSpec("collections", legacy_actor_columns=("created_by",)),
    ),
    "notes": (ResourceSpec("notes", legacy_actor_columns=("owner",)),),
    "files": (
        ResourceSpec("stored_files", legacy_actor_columns=("uploaded_by",), object_columns=("stored_name",)),
        ResourceSpec("file_folders"),
    ),
    "recipes": (ResourceSpec("recipes", legacy_actor_columns=("created_by",)),),
    "movies": (ResourceSpec("movies", legacy_actor_columns=("added_by",)),),
    "places": (
        ResourceSpec("restaurants", owner_columns=("owner_id", "added_by_id"), legacy_actor_columns=("added_by_name",)),
        ResourceSpec("restaurant_photos", object_columns=("image_name",)),
        ResourceSpec("margaritas", owner_columns=("owner_id", "updated_by_id"), legacy_actor_columns=("updated_by_name",), object_columns=("image_name",)),
        ResourceSpec("restaurant_reviews"),
        ResourceSpec("margarita_entries", object_columns=("image_name",)),
    ),
    "chat": (
        ResourceSpec("conversations", owner_columns=("owner_id", "created_by")),
        ResourceSpec("conversation_members"),
        ResourceSpec("messages", owner_columns=("owner_id", "sender_id")),
        ResourceSpec("chat_attachments", owner_columns=("owner_id",), object_columns=("object_path", "preview_path")),
    ),
    "assistant": (
        ResourceSpec("conversations"),
        ResourceSpec("messages"),
        ResourceSpec("remote_tasks"),
        ResourceSpec("approvals"),
    ),
    "audiobooks": (
        ResourceSpec("audiobooks", object_columns=("stored_name", "cover_name")),
        ResourceSpec("audiobook_progress"),
    ),
}


DOMAIN_RELATIONSHIPS = {
    "photos": (
        RelationshipSpec("collection_photos", "collections", "id", "collection_id", "photos", "id", "photo_id"),
    ),
    "files": (
        RelationshipSpec("stored_files", "file_folders", "id", "folder_id"),
    ),
    "recipes": (
        RelationshipSpec("recipe_recommendations", "recipes", "id", "recipe_id"),
    ),
    "movies": (
        RelationshipSpec("availability", "movies", "id", "movie_id"),
    ),
    "places": (
        RelationshipSpec("restaurant_photos", "restaurants", "id", "restaurant_id"),
        RelationshipSpec("restaurant_reviews", "restaurants", "id", "restaurant_id"),
    ),
    "chat": (
        RelationshipSpec("conversation_members", "conversations", "id", "conversation_id"),
        RelationshipSpec("messages", "conversations", "id", "conversation_id"),
        RelationshipSpec("chat_attachments", "messages", "id", "message_id"),
    ),
    "assistant": (
        RelationshipSpec("messages", "conversations", "id", "conversation_id"),
        RelationshipSpec("approvals", "remote_tasks", "id", "task_id"),
    ),
    "audiobooks": (
        RelationshipSpec("audiobook_progress", "audiobooks", "id", "audiobook_id"),
    ),
}


def quoted(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError("unsafe SQLite identifier")
    return f'"{identifier}"'


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return set()
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted(table)})")}


def scalar(connection: sqlite3.Connection, query: str, parameters=()) -> int:
    value = connection.execute(query, tuple(parameters)).fetchone()[0]
    return int(value or 0)


def first_present(columns: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in columns), None)


def resource_inventory(
    connection: sqlite3.Connection,
    spec: ResourceSpec,
    principals: dict[str, str],
) -> dict:
    columns = table_columns(connection, spec.table)
    if not columns:
        return {"present": False}
    table = quoted(spec.table)
    total = scalar(connection, f"SELECT COUNT(*) FROM {table}")
    owner_column = first_present(columns, spec.owner_columns)
    owner_counts = {"legacy_unclaimed": total, "recognized": {}, "other_nonempty": 0}
    if owner_column:
        owner = quoted(owner_column)
        unclaimed = scalar(
            connection,
            f"SELECT COUNT(*) FROM {table} WHERE {owner} IS NULL OR TRIM({owner})=''",
        )
        recognized = {}
        recognized_total = 0
        for label, owner_id in sorted(principals.items()):
            count = scalar(
                connection,
                f"SELECT COUNT(*) FROM {table} WHERE LOWER(TRIM({owner}))=?",
                (owner_id.casefold(),),
            )
            recognized[label] = count
            recognized_total += count
        nonempty = total - unclaimed
        owner_counts = {
            "legacy_unclaimed": unclaimed,
            "recognized": recognized,
            "other_nonempty": max(0, nonempty - recognized_total),
        }

    if "deleted_at" in columns:
        deleted = scalar(
            connection,
            f"SELECT COUNT(*) FROM {table} WHERE deleted_at IS NOT NULL AND TRIM(deleted_at)!=''",
        )
    else:
        deleted = 0

    visibility = {"shared": 0, "private": 0, "unset": total, "other": 0}
    if "visibility" in columns:
        visibility = {
            name: scalar(connection, f"SELECT COUNT(*) FROM {table} WHERE {condition}")
            for name, condition in (
                ("shared", "LOWER(TRIM(COALESCE(visibility,'')))='shared'"),
                ("private", "LOWER(TRIM(COALESCE(visibility,'')))='private'"),
                ("unset", "visibility IS NULL OR TRIM(visibility)=''"),
                ("other", "visibility IS NOT NULL AND TRIM(visibility)!='' AND LOWER(TRIM(visibility)) NOT IN ('shared','private')"),
            )
        }

    legacy_columns = [column for column in spec.legacy_actor_columns if column in columns]
    legacy_evidence = 0
    if legacy_columns:
        evidence = " OR ".join(
            f"({quoted(column)} IS NOT NULL AND TRIM({quoted(column)})!='')"
            for column in legacy_columns
        )
        legacy_evidence = scalar(connection, f"SELECT COUNT(*) FROM {table} WHERE {evidence}")

    object_references = {}
    for column in spec.object_columns:
        if column not in columns:
            continue
        identifier = quoted(column)
        populated = scalar(
            connection,
            f"SELECT COUNT(*) FROM {table} WHERE {identifier} IS NOT NULL AND TRIM({identifier})!=''",
        )
        distinct = scalar(
            connection,
            f"SELECT COUNT(DISTINCT {identifier}) FROM {table} WHERE {identifier} IS NOT NULL AND TRIM({identifier})!=''",
        )
        object_references[column] = {
            "populated": populated,
            "distinct": distinct,
            "duplicate_references": max(0, populated - distinct),
        }

    return {
        "present": True,
        "rows": {"total": total, "active": total - deleted, "deleted": deleted},
        "ownership": {"column": owner_column, **owner_counts},
        "visibility": visibility,
        "legacy_actor_evidence_rows": legacy_evidence,
        "lifecycle_columns": {
            name: name in columns
            for name in ("ownership_state", "version", "deleted_at", "deleted_by_id", "purge_after")
        },
        "object_references": object_references,
    }


def relationship_inventory(connection: sqlite3.Connection, spec: RelationshipSpec) -> dict:
    relationship_columns = table_columns(connection, spec.table)
    parent_columns = table_columns(connection, spec.parent_table)
    child_columns = table_columns(connection, spec.child_table) if spec.child_table else set()
    required = {spec.parent_foreign_key}
    if spec.child_foreign_key:
        required.add(spec.child_foreign_key)
    if not relationship_columns or not parent_columns or not required.issubset(relationship_columns):
        return {"present": False}

    relation = quoted(spec.table)
    parent = quoted(spec.parent_table)
    parent_fk = quoted(spec.parent_foreign_key)
    parent_key = quoted(spec.parent_key)
    total = scalar(connection, f"SELECT COUNT(*) FROM {relation}")
    parent_orphans = scalar(
        connection,
        f"SELECT COUNT(*) FROM {relation} r LEFT JOIN {parent} p ON p.{parent_key}=r.{parent_fk} "
        f"WHERE r.{parent_fk} IS NOT NULL AND p.{parent_key} IS NULL",
    )
    result = {"present": True, "rows": total, "orphan_parents": parent_orphans}

    if spec.child_table and spec.child_foreign_key and child_columns:
        child = quoted(spec.child_table)
        child_fk = quoted(spec.child_foreign_key)
        child_key = quoted(spec.child_key)
        result["orphan_children"] = scalar(
            connection,
            f"SELECT COUNT(*) FROM {relation} r LEFT JOIN {child} c ON c.{child_key}=r.{child_fk} "
            f"WHERE r.{child_fk} IS NOT NULL AND c.{child_key} IS NULL",
        )
        if "owner_id" in parent_columns and "owner_id" in child_columns:
            result["cross_owner_links"] = scalar(
                connection,
                f"SELECT COUNT(*) FROM {relation} r "
                f"JOIN {parent} p ON p.{parent_key}=r.{parent_fk} "
                f"JOIN {child} c ON c.{child_key}=r.{child_fk} "
                "WHERE p.owner_id IS NOT NULL AND TRIM(p.owner_id)!='' "
                "AND c.owner_id IS NOT NULL AND TRIM(c.owner_id)!='' "
                "AND LOWER(TRIM(p.owner_id)) != LOWER(TRIM(c.owner_id))",
            )
        else:
            result["cross_owner_links"] = None
    return result


def clone_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(path.with_name(path.name + suffix) for suffix in ("-wal", "-shm", "-journal"))


def open_clone(path: Path) -> tuple[sqlite3.Connection, int, Path, tuple[int, ...]]:
    requested = path.absolute()
    if stat.S_ISLNK(requested.lstat().st_mode):
        raise ValueError("database clone cannot be a symbolic link")
    resolved = requested.resolve(strict=True)
    if any(sidecar.exists() for sidecar in clone_sidecars(resolved)):
        raise ValueError("database clone has a live SQLite sidecar")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise ValueError("database clone must be a regular file")
    stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    descriptor_path = Path("/proc/self/fd") / str(descriptor)
    if not descriptor_path.exists():
        os.close(descriptor)
        raise ValueError("pinned clone descriptors require Linux procfs")
    uri = f"file:{quote(str(descriptor_path), safe='/')}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except Exception:
        os.close(descriptor)
        raise
    connection.execute("PRAGMA query_only=ON")
    return connection, descriptor, resolved, stamp


def verify_clone_stable(descriptor: int, path: Path, stamp: tuple[int, ...]) -> None:
    after = os.fstat(descriptor)
    after_stamp = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    try:
        path_after = path.stat()
    except OSError as error:
        raise ValueError("database clone changed during inventory") from error
    if after_stamp != stamp or (path_after.st_dev, path_after.st_ino) != stamp[:2]:
        raise ValueError("database clone changed during inventory")
    if any(sidecar.exists() for sidecar in clone_sidecars(path)):
        raise ValueError("database clone gained a live SQLite sidecar")


def inventory_database(domain: str, path: Path, principals: dict[str, str]) -> dict:
    if domain not in DOMAIN_RESOURCES:
        raise ValueError(f"unsupported inventory domain: {domain}")
    connection, descriptor, resolved, stamp = open_clone(path)
    try:
        resources = {
            spec.table: resource_inventory(connection, spec, principals)
            for spec in DOMAIN_RESOURCES[domain]
        }
        relationships = {
            f"{spec.table}.{spec.parent_foreign_key}": relationship_inventory(connection, spec)
            for spec in DOMAIN_RELATIONSHIPS.get(domain, ())
        }
        verify_clone_stable(descriptor, resolved, stamp)
    finally:
        connection.close()
        os.close(descriptor)
    return {"resources": resources, "relationships": relationships}


def build_inventory(databases: dict[str, Path], principals: dict[str, str]) -> dict:
    domains = {
        domain: inventory_database(domain, path, principals)
        for domain, path in sorted(databases.items())
    }
    payload = {"schema_version": 1, "domains": domains}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**payload, "plan_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def parse_mapping(values: list[str], label: str) -> dict[str, str]:
    result = {}
    for value in values:
        key, separator, raw = value.partition("=")
        key = key.strip().casefold()
        raw = raw.strip()
        if not separator or not key or not raw or not key.replace("_", "").isalnum():
            raise ValueError(f"invalid {label}: expected LABEL=VALUE")
        if key in result:
            raise ValueError(f"duplicate {label}: {key}")
        result[key] = raw
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="append", default=[], metavar="DOMAIN=PATH", required=True)
    parser.add_argument("--principal", action="append", default=[], metavar="LABEL=OWNER_ID")
    parser.add_argument("--output", type=Path, help="Create this new JSON file instead of writing stdout")
    arguments = parser.parse_args(argv)
    try:
        databases = {key: Path(value) for key, value in parse_mapping(arguments.database, "database").items()}
        principals = parse_mapping(arguments.principal, "principal")
        normalized_principals = [value.casefold() for value in principals.values()]
        if len(normalized_principals) != len(set(normalized_principals)):
            raise ValueError("principal owner IDs must be unique")
        report = build_inventory(databases, principals)
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if arguments.output:
            descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
        else:
            sys.stdout.write(encoded)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
