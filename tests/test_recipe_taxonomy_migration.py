import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "david_pi_recipe_taxonomy.py"
SPEC = importlib.util.spec_from_file_location("recipe_taxonomy_migration", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


class RecipeTaxonomyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "recipes.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """CREATE TABLE recipes (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, meal_type TEXT NOT NULL,
                    tags_json TEXT NOT NULL, source_name TEXT, source_url TEXT UNIQUE,
                    updated_at TEXT NOT NULL, deleted_at TEXT
                )"""
            )
            for index, (source_url, title, old_section, _, _) in enumerate(migration.CATEGORY_OVERRIDES):
                connection.execute(
                    "INSERT INTO recipes VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                    (f"recipe-{index}", title, old_section, '[\"upstream-tag\"]', "upstream", source_url, "original-time"),
                )

    def tearDown(self):
        self.temporary.cleanup()

    def preview(self):
        with migration.connect_read_only(self.database) as connection:
            return migration.build_plan(connection)

    def test_preview_is_read_only_and_deterministic(self):
        first = self.preview()
        second = self.preview()
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        self.assertEqual(first["counts"]["change"], len(migration.CATEGORY_OVERRIDES))
        self.assertEqual(first["counts"]["missing"], 0)
        with sqlite3.connect(self.database) as connection:
            sections = [row[0] for row in connection.execute("SELECT meal_type FROM recipes ORDER BY id")]
        self.assertIn("breakfast", sections)
        self.assertIn("dessert", sections)

    def test_catalog_source_locator_is_supported_without_rewriting_legacy_column(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE recipes ADD COLUMN catalog_source_url TEXT")
            connection.execute(
                "UPDATE recipes SET catalog_source_url=source_url, source_url=NULL"
            )
        preview = self.preview()
        self.assertEqual(preview["counts"]["change"], len(migration.CATEGORY_OVERRIDES))
        result = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root,
            migration.APPLY_CONFIRMATION,
        )
        self.assertEqual(result["status"], "applied")
        with sqlite3.connect(self.database) as connection:
            source_values = connection.execute(
                "SELECT source_url,catalog_source_url FROM recipes ORDER BY id"
            ).fetchall()
            audited_sources = {
                row[0] for row in connection.execute(
                    "SELECT original_source_url FROM recipe_taxonomy_migrations"
                )
            }
        self.assertTrue(all(legacy is None and catalog for legacy, catalog in source_values))
        self.assertEqual(audited_sources, {item[0] for item in migration.CATEGORY_OVERRIDES})

    def test_apply_is_backed_up_audited_transactional_and_reversible(self):
        preview = self.preview()
        result = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["changed"], len(migration.CATEGORY_OVERRIDES))
        self.assertEqual(
            result["migration_id"],
            f"recipe-taxonomy-v{migration.PLAN_VERSION}-{preview['plan_sha256'][:16]}",
        )
        backup = Path(result["backup"]["path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(result["backup"]["sha256"], migration.file_sha256(backup))
        with sqlite3.connect(self.database) as connection:
            audit = connection.execute(
                """SELECT COUNT(*), MIN(original_tags_json), MIN(original_source_name),
                          MIN(original_updated_at) FROM recipe_taxonomy_migrations"""
            ).fetchone()
            self.assertEqual(audit, (len(migration.CATEGORY_OVERRIDES), '["upstream-tag"]', "upstream", "original-time"))
        repeated = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        self.assertEqual(repeated["status"], "already_applied")
        self.assertEqual(repeated["migration_id"], result["migration_id"])
        self.assertIsNone(repeated["backup"])
        self.assertEqual(len(list(self.root.glob("*.sqlite3"))), 1)
        rolled_back = migration.rollback_plan(
            self.database, result["migration_id"], migration.APPLY_CONFIRMATION
        )
        self.assertEqual(rolled_back["changed"], len(migration.CATEGORY_OVERRIDES))
        repeated_rollback = migration.rollback_plan(
            self.database, result["migration_id"], migration.APPLY_CONFIRMATION
        )
        self.assertEqual(repeated_rollback["status"], "already_rolled_back")
        self.assertEqual(self.preview()["counts"]["change"], len(migration.CATEGORY_OVERRIDES))

    def test_rollback_then_reapply_uses_attempt2_and_preserves_attempt1(self):
        preview = self.preview()
        attempt1 = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        migration.rollback_plan(self.database, attempt1["migration_id"], migration.APPLY_CONFIRMATION)

        attempt2 = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        self.assertEqual(attempt2["status"], "applied")
        self.assertEqual(attempt2["migration_id"], f"{attempt1['migration_id']}-attempt2")
        self.assertEqual(len(list(self.root.glob("*.sqlite3"))), 2)

        repeated = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        self.assertEqual(repeated["status"], "already_applied")
        self.assertEqual(repeated["migration_id"], attempt2["migration_id"])
        self.assertIsNone(repeated["backup"])
        self.assertEqual(len(list(self.root.glob("*.sqlite3"))), 2)

        with sqlite3.connect(self.database) as connection:
            states = connection.execute(
                """SELECT migration_id, COUNT(*),
                          SUM(CASE WHEN rolled_back_at IS NOT NULL THEN 1 ELSE 0 END)
                   FROM recipe_taxonomy_migrations GROUP BY migration_id ORDER BY migration_id"""
            ).fetchall()
        self.assertEqual(states, [
            (attempt1["migration_id"], len(migration.CATEGORY_OVERRIDES), len(migration.CATEGORY_OVERRIDES)),
            (attempt2["migration_id"], len(migration.CATEGORY_OVERRIDES), 0),
        ])

        rolled_back = migration.rollback_plan(
            self.database, attempt2["migration_id"], migration.APPLY_CONFIRMATION
        )
        self.assertEqual(rolled_back["changed"], len(migration.CATEGORY_OVERRIDES))
        self.assertEqual(self.preview()["counts"]["change"], len(migration.CATEGORY_OVERRIDES))
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM recipe_taxonomy_migrations WHERE migration_id = ? AND rolled_back_at IS NOT NULL",
                    (attempt2["migration_id"],),
                ).fetchone()[0],
                len(migration.CATEGORY_OVERRIDES),
            )
        attempt3 = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        self.assertEqual(attempt3["migration_id"], f"{attempt1['migration_id']}-attempt3")

    def test_apply_refuses_mixed_or_partial_prior_audit_rows(self):
        preview = self.preview()
        applied = migration.apply_plan(
            self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """UPDATE recipe_taxonomy_migrations SET rolled_back_at = 'manual'
                   WHERE migration_id = ? AND recipe_id = 'recipe-0'""",
                (applied["migration_id"],),
            )
        with self.assertRaisesRegex(RuntimeError, "mixed"):
            migration.apply_plan(
                self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
            )

        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE recipe_taxonomy_migrations SET rolled_back_at = NULL WHERE migration_id = ?",
                (applied["migration_id"],),
            )
            connection.execute(
                "DELETE FROM recipe_taxonomy_migrations WHERE migration_id = ? AND recipe_id = 'recipe-0'",
                (applied["migration_id"],),
            )
        with self.assertRaisesRegex(RuntimeError, "partial"):
            migration.apply_plan(
                self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
            )
        self.assertEqual(len(list(self.root.glob("*.sqlite3"))), 1)

    def test_apply_refuses_stale_preview(self):
        preview = self.preview()
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE recipes SET title = 'Changed' WHERE id = 'recipe-0'")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            migration.apply_plan(
                self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
            )
        self.assertEqual(list(self.root.glob("*.sqlite3")), [])

    def test_backup_failure_prevents_audit_and_recipe_writes(self):
        preview = self.preview()
        with mock.patch.object(migration, "verified_backup", side_effect=RuntimeError("backup failed")):
            with self.assertRaisesRegex(RuntimeError, "backup failed"):
                migration.apply_plan(
                    self.database, preview["plan_sha256"], self.root, migration.APPLY_CONFIRMATION
                )
        with sqlite3.connect(self.database) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recipe_taxonomy_migrations'"
            ).fetchone())
            sections = dict(connection.execute("SELECT id, meal_type FROM recipes"))
        self.assertEqual(
            sections,
            {f"recipe-{index}": item[2] for index, item in enumerate(migration.CATEGORY_OVERRIDES)},
        )


if __name__ == "__main__":
    unittest.main()
