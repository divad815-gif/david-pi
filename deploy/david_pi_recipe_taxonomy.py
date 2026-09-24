#!/usr/bin/env python3
"""Preview and safely apply the reviewed David-Pi recipe shelf corrections.

Preview is read-only. Apply is deliberately gated by the exact preview digest,
creates and verifies a full SQLite backup first, preserves upstream metadata in
an audit table, and changes only ``meal_type`` in one transaction.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone


PLAN_VERSION = 1
APPLY_CONFIRMATION = "APPLY_RECIPE_TAXONOMY"

# Source URL is the stable identity. Titles are preconditions, not lookup keys.
CATEGORY_OVERRIDES = (
    ("https://www.themealdb.com/meal/52802", "Fish pie", "breakfast", "main", "Savory fish pie was imported with a Breakfast tag."),
    ("https://www.themealdb.com/meal/52773", "Honey Teriyaki Salmon", "breakfast", "main", "Savory salmon entree was imported with a Breakfast tag."),
    ("https://www.bbcgoodfood.com/recipes/ultimate-spaghetti-carbonara-recipe", "Spaghetti alla Carbonara", "breakfast", "main", "Savory pasta entree was imported with a Breakfast tag."),
    ("https://www.thespruceeats.com/stamppot-with-curly-kale-and-rookworst-1128837", "Stamppot", "breakfast", "main", "Savory sausage and kale meal was imported with a Breakfast tag."),
    ("https://tasteoftheplace.com/ugali-kenyan-cornmeal/", "Ugali – Kenyan cornmeal", "breakfast", "main", "Staple cornmeal side/main was imported as Breakfast."),
    ("https://www.bbcgoodfood.com/recipes/2869/new-york-cheesecake", "New York cheesecake", "breakfast", "dessert", "Title and dessert tags agree that this is dessert."),
    ("https://tasty.co/recipe/3-ingredient-peanut-butter-cookies", "Peanut Butter Cookies", "breakfast", "dessert", "Title and dessert tag agree that this is dessert."),
    ("https://www.instagram.com/p/BO21bpYD3Fu", "Dal fry", "dessert", "main", "Savory lentil curry was imported with an erroneous Cake tag."),
    ("https://www.bbcgoodfood.com/recipes/531644/spiced-pork-and-potato-pie", "Tourtiere", "dessert", "main", "Pork main-meal pie was imported with an erroneous Cake tag."),
    ("https://www.thespruceeats.com/traditional-dutch-split-pea-soup-1129011", "Snert (Dutch Split Pea Soup)", "dessert", "main", "Savory soup was imported with an erroneous Cake tag."),
    ("https://www.bbcgoodfood.com/recipes/1803634/brie-wrapped-in-prosciutto-and-brioche", "Brie wrapped in prosciutto & brioche", "dessert", "main", "Savory side was placed on the Dessert shelf."),
    ("https://www.instagram.com/p/BHyuMZ1hZX0", "Cream Cheese Tart", "dessert", "main", "Savory starter was placed on the Dessert shelf."),
)

REVIEW_ONLY = (
    {
        "source_url": "https://cooking.nytimes.com/recipes/1018529-coq-au-vin",
        "title": "Bread and Butter Pudding",
        "reason": "The title/category suggest dessert, but the saved source URL points to Coq au Vin; inspect the recipe card before changing anything.",
    },
    {
        "group": ["Quick salt & pepper squid", "Salt & pepper squid"],
        "reason": "Near-duplicate titles and neighboring source URLs; compare content manually before merging.",
    },
    {
        "group": ["Easy Spanish chicken", "Spanish Chicken"],
        "reason": "Near-duplicate titles; compare content manually before merging.",
    },
    {
        "group": ["Fettuccine Alfredo", "Fettucine alfredo"],
        "reason": "Near-duplicate title with spelling variation; compare content manually before merging.",
    },
)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def connect_read_only(path):
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)


def source_locator_expression(connection):
    """Use the additive catalog locator when present, with legacy fallback."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(recipes)")
    }
    if "source_url" not in columns:
        raise RuntimeError("recipes.source_url is missing")
    if "catalog_source_url" in columns:
        return "COALESCE(catalog_source_url, source_url)"
    return "source_url"


def build_plan(connection):
    connection.row_factory = sqlite3.Row
    source_locator = source_locator_expression(connection)
    changes, already_applied, missing, conflicts = [], [], [], []
    for source_url, expected_title, old_section, new_section, reason in CATEGORY_OVERRIDES:
        row = connection.execute(
            f"""SELECT id, title, meal_type, tags_json, source_name,
                       {source_locator} AS source_url, updated_at
                FROM recipes WHERE {source_locator} = ? AND deleted_at IS NULL""",
            (source_url,),
        ).fetchone()
        identity = {"source_url": source_url, "expected_title": expected_title}
        if row is None:
            missing.append(identity)
            continue
        observed = {"id": row["id"], "title": row["title"], "section": row["meal_type"]}
        if row["title"] != expected_title:
            conflicts.append({**identity, **observed, "reason": "title_precondition_failed"})
            continue
        item = {
            "id": row["id"],
            "title": row["title"],
            "source_url": row["source_url"],
            "from": old_section,
            "to": new_section,
            "reason": reason,
        }
        if row["meal_type"] == old_section:
            changes.append(item)
        elif row["meal_type"] == new_section:
            already_applied.append(item)
        else:
            conflicts.append({**item, "observed_section": row["meal_type"], "reason": "section_precondition_failed"})
    digest_input = {
        "plan_version": PLAN_VERSION,
        "changes": changes,
        "already_applied": already_applied,
        "missing": missing,
        "conflicts": conflicts,
    }
    return {
        **digest_input,
        "plan_sha256": canonical_digest(digest_input),
        "counts": {
            "change": len(changes),
            "already_applied": len(already_applied),
            "missing": len(missing),
            "conflict": len(conflicts),
            "review_only": len(REVIEW_ONLY),
        },
        "review_only": list(REVIEW_ONLY),
    }


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_backup(database, backup_dir):
    backup_dir = Path(backup_dir).resolve()
    if not backup_dir.is_dir():
        raise RuntimeError(f"Backup directory does not exist: {backup_dir}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    descriptor, name = tempfile.mkstemp(
        prefix=f"{Path(database).stem}.pre-taxonomy-{stamp}-", suffix=".sqlite3", dir=backup_dir
    )
    os.close(descriptor)
    os.chmod(name, 0o600)
    try:
        with connect_read_only(database) as source, sqlite3.connect(name) as destination:
            source.backup(destination)
        with sqlite3.connect(f"file:{Path(name).resolve()}?mode=ro", uri=True) as check:
            result = check.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"Backup quick_check failed: {result}")
            with connect_read_only(database) as source_check:
                source_count = source_check.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            backup_count = check.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            if source_count != backup_count:
                raise RuntimeError("Backup recipe count does not match source database")
        with open(name, "rb") as backup_file:
            os.fsync(backup_file.fileno())
        return {"path": name, "sha256": file_sha256(name), "recipe_count": backup_count}
    except Exception:
        Path(name).unlink(missing_ok=True)
        raise


def migration_base_id(plan_digest):
    return f"recipe-taxonomy-v{PLAN_VERSION}-{plan_digest[:16]}"


def inspect_audit_attempts(connection, base_id):
    """Return complete attempt history, rejecting ambiguous audit state.

    Audit rows are append-only. An attempt is usable only when it has exactly
    one row per reviewed override and every row agrees on active/rolled-back
    state. This makes a partial manual edit a hard stop rather than something
    the migration tries to repair heuristically.
    """
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipe_taxonomy_migrations'"
    ).fetchone()
    if not table:
        return {"attempts": [], "active": None, "next_migration_id": base_id}
    rows = connection.execute(
        """SELECT migration_id, recipe_id, old_section, new_section, rolled_back_at
           FROM recipe_taxonomy_migrations
           WHERE migration_id = ? OR migration_id LIKE ?
           ORDER BY migration_id, recipe_id""",
        (base_id, f"{base_id}-attempt%"),
    ).fetchall()
    grouped = {}
    for row in rows:
        migration_id = row["migration_id"]
        if migration_id == base_id:
            attempt = 1
        elif migration_id.startswith(f"{base_id}-attempt"):
            suffix = migration_id.removeprefix(f"{base_id}-attempt")
            if not suffix.isdigit() or int(suffix) < 2 or str(int(suffix)) != suffix:
                raise RuntimeError("Audit history contains an invalid migration attempt id")
            attempt = int(suffix)
        else:
            raise RuntimeError("Audit history contains an invalid migration attempt id")
        grouped.setdefault(attempt, []).append(row)

    if not grouped:
        return {"attempts": [], "active": None, "next_migration_id": base_id}
    numbers = sorted(grouped)
    if numbers != list(range(1, numbers[-1] + 1)):
        raise RuntimeError("Audit history has a missing migration attempt")

    attempts = []
    for number in numbers:
        attempt_rows = grouped[number]
        if len(attempt_rows) != len(CATEGORY_OVERRIDES):
            raise RuntimeError("Audit history contains a partial migration attempt")
        active_count = sum(row["rolled_back_at"] is None for row in attempt_rows)
        if active_count == len(CATEGORY_OVERRIDES):
            state = "active"
        elif active_count == 0:
            state = "rolled_back"
        else:
            raise RuntimeError("Audit history contains a mixed migration attempt")
        attempts.append({
            "number": number,
            "migration_id": base_id if number == 1 else f"{base_id}-attempt{number}",
            "state": state,
            "transitions": {
                row["recipe_id"]: (row["old_section"], row["new_section"])
                for row in attempt_rows
            },
        })

    active = [attempt for attempt in attempts if attempt["state"] == "active"]
    if len(active) > 1 or (active and active[0]["number"] != attempts[-1]["number"]):
        raise RuntimeError("Audit history contains conflicting active migration attempts")
    next_number = numbers[-1] + 1
    return {
        "attempts": attempts,
        "active": active[0] if active else None,
        "next_migration_id": f"{base_id}-attempt{next_number}",
    }


def plan_apply_attempt(connection, preview, expected_digest):
    base_id = migration_base_id(expected_digest)
    audit = inspect_audit_attempts(connection, base_id)
    if not preview["conflicts"] and not preview["missing"]:
        expected_transitions = {
            item["id"]: (item["from"], item["to"])
            for item in preview["changes"] + preview["already_applied"]
        }
        for attempt in audit["attempts"]:
            if attempt["transitions"] != expected_transitions:
                raise RuntimeError("Audit history does not match the reviewed recipe transitions")
    if audit["active"]:
        fully_applied = (
            not preview["changes"]
            and not preview["conflicts"]
            and not preview["missing"]
            and len(preview["already_applied"]) == len(CATEGORY_OVERRIDES)
        )
        if not fully_applied:
            raise RuntimeError("Active audit attempt exists but recipe preconditions have changed")
        return {"status": "already_applied", "migration_id": audit["active"]["migration_id"]}
    if preview["plan_sha256"] != expected_digest:
        raise RuntimeError("Live plan differs from the approved preview digest; preview again")
    if preview["conflicts"] or preview["missing"]:
        raise RuntimeError("Apply refused because required rows are missing or changed")
    if not preview["changes"]:
        raise RuntimeError("Target sections are not backed by a fully active audit attempt")
    return {"status": "apply", "migration_id": audit["next_migration_id"]}


def apply_plan(database, expected_digest, backup_dir, confirmation):
    if confirmation != APPLY_CONFIRMATION:
        raise RuntimeError(f"Apply requires --confirm {APPLY_CONFIRMATION}")
    with connect_read_only(database) as connection:
        preview = build_plan(connection)
        decision = plan_apply_attempt(connection, preview, expected_digest)
    if decision["status"] == "already_applied":
        return {
            "status": "already_applied",
            "migration_id": decision["migration_id"],
            "changed": 0,
            "backup": None,
        }
    backup = verified_backup(database, backup_dir)
    migration_id = decision["migration_id"]
    connection = sqlite3.connect(Path(database).resolve())
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        locked_plan = build_plan(connection)
        locked_decision = plan_apply_attempt(connection, locked_plan, expected_digest)
        if locked_decision["status"] != "apply" or locked_decision["migration_id"] != migration_id:
            raise RuntimeError("Live plan changed while acquiring the database lock")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS recipe_taxonomy_migrations (
                migration_id TEXT NOT NULL,
                recipe_id TEXT NOT NULL,
                old_section TEXT NOT NULL,
                new_section TEXT NOT NULL,
                reason TEXT NOT NULL,
                original_tags_json TEXT NOT NULL,
                original_source_name TEXT,
                original_source_url TEXT,
                original_updated_at TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                rolled_back_at TEXT,
                PRIMARY KEY (migration_id, recipe_id)
            )"""
        )
        applied_at = utcnow()
        source_locator = source_locator_expression(connection)
        for item in locked_plan["changes"]:
            original = connection.execute(
                f"""SELECT meal_type, tags_json, source_name,
                           {source_locator} AS source_url, updated_at
                    FROM recipes WHERE id = ?""",
                (item["id"],),
            ).fetchone()
            connection.execute(
                """INSERT INTO recipe_taxonomy_migrations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    migration_id, item["id"], original["meal_type"], item["to"], item["reason"],
                    original["tags_json"], original["source_name"], original["source_url"],
                    original["updated_at"], applied_at,
                ),
            )
            result = connection.execute(
                "UPDATE recipes SET meal_type = ? WHERE id = ? AND meal_type = ? AND deleted_at IS NULL",
                (item["to"], item["id"], item["from"]),
            )
            if result.rowcount != 1:
                raise RuntimeError(f"Precondition changed for recipe {item['id']}")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Database quick_check failed after staged updates")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "status": "applied",
        "migration_id": migration_id,
        "changed": len(preview["changes"]),
        "backup": backup,
        "rollback": f"Use rollback --migration-id {migration_id}; full backup is at {backup['path']}",
    }


def rollback_plan(database, migration_id, confirmation):
    if confirmation != APPLY_CONFIRMATION:
        raise RuntimeError(f"Rollback requires --confirm {APPLY_CONFIRMATION}")
    connection = sqlite3.connect(Path(database).resolve())
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipe_taxonomy_migrations'"
        ).fetchone()
        if not table:
            raise RuntimeError("No audit rows exist for that migration")
        rows = connection.execute(
            """SELECT * FROM recipe_taxonomy_migrations
               WHERE migration_id = ? ORDER BY recipe_id""",
            (migration_id,),
        ).fetchall()
        if not rows:
            raise RuntimeError("No audit rows exist for that migration")
        if len(rows) != len(CATEGORY_OVERRIDES):
            raise RuntimeError("Rollback refused because audit rows are partial")
        active_count = sum(row["rolled_back_at"] is None for row in rows)
        if active_count == 0:
            connection.rollback()
            return {"status": "already_rolled_back", "migration_id": migration_id, "changed": 0}
        if active_count != len(CATEGORY_OVERRIDES):
            raise RuntimeError("Rollback refused because audit rows have mixed state")
        conflicts = []
        for row in rows:
            current = connection.execute("SELECT meal_type FROM recipes WHERE id = ?", (row["recipe_id"],)).fetchone()
            if current is None or current["meal_type"] != row["new_section"]:
                conflicts.append(row["recipe_id"])
        if conflicts:
            raise RuntimeError("Rollback refused because recipes changed after migration: " + ", ".join(conflicts))
        rolled_back_at = utcnow()
        for row in rows:
            connection.execute("UPDATE recipes SET meal_type = ? WHERE id = ?", (row["old_section"], row["recipe_id"]))
        connection.execute(
            "UPDATE recipe_taxonomy_migrations SET rolled_back_at = ? WHERE migration_id = ?",
            (rolled_back_at, migration_id),
        )
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Database quick_check failed during rollback")
        connection.commit()
        return {"status": "rolled_back", "migration_id": migration_id, "changed": len(rows)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to recipes.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preview")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--expected-plan-sha256", required=True)
    apply_parser.add_argument("--backup-dir", required=True)
    apply_parser.add_argument("--confirm", required=True)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--migration-id", required=True)
    rollback_parser.add_argument("--confirm", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preview":
            with connect_read_only(args.db) as connection:
                result = build_plan(connection)
        elif args.command == "apply":
            result = apply_plan(args.db, args.expected_plan_sha256, args.backup_dir, args.confirm)
        else:
            result = rollback_plan(args.db, args.migration_id, args.confirm)
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
