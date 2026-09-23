import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from modules import recipe_quality


SCHEMA = """CREATE TABLE recipes (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    meal_type TEXT NOT NULL DEFAULT 'main', tags_json TEXT NOT NULL DEFAULT '[]',
    total_minutes INTEGER, servings TEXT, ingredients_json TEXT NOT NULL DEFAULT '[]',
    instructions_json TEXT NOT NULL DEFAULT '[]', source_name TEXT, source_url TEXT UNIQUE,
    image_url TEXT, content_hash TEXT NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    imported_at TEXT, last_viewed_at TEXT, last_made_at TEXT, deleted_at TEXT
)"""


def fingerprint(title, ingredients, instructions):
    return hashlib.sha256(
        json.dumps([title.lower(), ingredients, instructions], sort_keys=True).encode()
    ).hexdigest()


class RecipeQualityAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "recipes.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute(SCHEMA)

    def tearDown(self):
        self.temporary.cleanup()

    def insert(
        self, recipe_id, title, meal_type="main", tags=None,
        ingredients=None, instructions=None, description="A useful description.",
        total_minutes=30, servings="4", source_name="Test source",
        source_url=None, image_url="https://images.invalid/example.jpg",
        content_hash=None, deleted_at=None,
    ):
        tags = ["family"] if tags is None else tags
        ingredients = ["one", "two"] if ingredients is None else ingredients
        instructions = ["Cook it."] if instructions is None else instructions
        content_hash = content_hash or fingerprint(title, ingredients, instructions)
        source_url = source_url or f"https://recipes.invalid/{recipe_id}"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO recipes (
                       id, title, description, meal_type, tags_json, total_minutes,
                       servings, ingredients_json, instructions_json, source_name,
                       source_url, image_url, content_hash, created_by, created_at,
                       updated_at, deleted_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owner',
                             '2026-01-01', '2026-01-01', ?)""",
                (
                    recipe_id, title, description, meal_type, json.dumps(tags),
                    total_minutes, servings, json.dumps(ingredients),
                    json.dumps(instructions), source_name, source_url, image_url,
                    content_hash, deleted_at,
                ),
            )

    def audit(self, **kwargs):
        return recipe_quality.audit_database(self.database, **kwargs)

    def test_audit_is_read_only_and_deterministic(self):
        self.insert("one", "Chicken Soup")
        self.insert("two", "Tomato Soup")
        before = self.database.read_bytes()
        with recipe_quality.connect_read_only(self.database) as connection:
            rows = recipe_quality.load_recipes(connection)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM recipes")
        first = recipe_quality.analyze_recipes(list(reversed(rows)))
        second = self.audit()
        self.assertEqual(first, second)
        self.assertEqual(before, self.database.read_bytes())
        self.assertEqual(first["safety"]["changes_applied"], 0)
        self.assertEqual(first["scope"]["active"], 2)

    def test_audit_uses_catalog_source_locator_with_legacy_fallback(self):
        self.insert("catalog", "Catalog Recipe")
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE recipes ADD COLUMN catalog_source_url TEXT")
            connection.execute(
                """UPDATE recipes SET catalog_source_url=source_url, source_url=NULL
                   WHERE id='catalog'"""
            )
        before = self.database.read_bytes()
        with recipe_quality.connect_read_only(self.database) as connection:
            row = recipe_quality.load_recipes(connection)[0]
        self.assertEqual(row["source_url"], "https://recipes.invalid/catalog")
        report = self.audit()
        self.assertNotIn("missing_source_url", report["metadata"]["issue_counts"])
        self.assertEqual(before, self.database.read_bytes())

    def test_flags_conservative_category_reviews_and_invalid_category(self):
        self.insert("cake", "Chocolate Cake", meal_type="main")
        self.insert("stew", "Beef Stew", meal_type="dessert")
        self.insert("pie", "Garden Pie", meal_type="main")
        self.insert("bad", "Unknown Dish", meal_type="snack")
        self.insert("case", "Plain Dish", meal_type="Main")
        report = self.audit()
        reviews = {
            item["recipe_id"]: item
            for item in report["taxonomy"]["likely_misclassified"]["records"]
        }
        self.assertEqual(reviews["cake"]["review_category"], "dessert")
        self.assertEqual(reviews["stew"]["review_category"], "main")
        self.assertNotIn("pie", reviews)
        invalid = {
            item["recipe_id"]: item
            for item in report["taxonomy"]["invalid_or_uncategorized"]["records"]
        }
        self.assertEqual(invalid["bad"]["observed_category"], "snack")
        self.assertEqual(invalid["case"]["normalized_candidate"], "main")

    def test_reports_duplicates_and_near_duplicates_without_recipe_bodies(self):
        shared_ingredients = ["salt", "squid"]
        shared_instructions = ["Cook quickly."]
        shared_hash = fingerprint(
            "Salt and Pepper Squid", shared_ingredients, shared_instructions
        )
        self.insert(
            "a", "Salt and Pepper Squid", ingredients=shared_ingredients,
            instructions=shared_instructions, content_hash=shared_hash,
        )
        self.insert(
            "b", "Salt and Pepper Squid", ingredients=shared_ingredients,
            instructions=shared_instructions, content_hash=shared_hash,
        )
        self.insert("c", "Salt & Pepper Squid")
        self.insert("d", "Easy Spanish Chicken")
        self.insert("e", "Spanish Chicken")
        report = self.audit()
        duplicates = report["duplicates"]
        self.assertEqual(duplicates["stored_content_hash_groups"]["count"], 1)
        self.assertEqual(duplicates["computed_content_groups"]["count"], 1)
        self.assertEqual(duplicates["normalized_title_groups"]["count"], 1)
        near_ids = [
            item["recipe_ids"] for item in duplicates["near_title_pairs"]["records"]
        ]
        self.assertIn(["a", "c"], near_ids)
        self.assertIn(["d", "e"], near_ids)
        encoded = json.dumps(report)
        self.assertNotIn("Cook quickly.", encoded)
        self.assertNotIn("recipes.invalid", encoded)

    def test_metadata_quality_counts_invalid_missing_weak_and_stale_fields(self):
        self.insert(
            "weak", "Cake", description="short", tags=[], ingredients=["one"],
            instructions=[], total_minutes=None, servings="", source_name="",
            source_url="", image_url="", content_hash="0" * 64,
        )
        # UNIQUE permits NULL while preserving a genuinely missing source URL.
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE recipes SET source_url = NULL, tags_json = '{bad' WHERE id = 'weak'"
            )
        report = self.audit()
        counts = report["metadata"]["issue_counts"]
        expected = {
            "invalid_tags_json", "missing_total_minutes", "missing_servings",
            "weak_ingredients", "missing_instructions", "missing_source_name",
            "missing_source_url", "missing_image", "stale_content_hash",
            "weak_description",
        }
        self.assertTrue(expected.issubset(counts))

    def test_taxonomy_imbalance_deleted_scope_and_output_truncation(self):
        for index in range(8):
            self.insert(str(index), f"Main Dish {index}", meal_type="main")
        self.insert("dessert", "Berry Dessert", meal_type="dessert")
        self.insert("deleted", "Old Pancakes", meal_type="breakfast", deleted_at="now")
        report = self.audit(max_findings=1)
        self.assertEqual(report["scope"], {
            "total": 10,
            "active": 9,
            "deleted": 1,
            "analysis_input_sha256": report["scope"]["analysis_input_sha256"],
            "titles_included": True,
            "max_findings_per_section": 1,
        })
        flags = report["taxonomy"]["imbalance_flags"]
        self.assertIn({"category": "breakfast", "reason": "empty"}, flags)
        self.assertIn({"category": "main", "reason": "dominant"}, flags)

    def test_redacted_report_omits_titles_and_is_deterministic(self):
        self.insert("one", "Private Family Cake", meal_type="main")
        report = self.audit(include_titles=False)
        self.assertNotIn("Private Family Cake", json.dumps(report))
        self.assertFalse(report["scope"]["titles_included"])

    def test_missing_schema_fails_closed(self):
        broken = Path(self.temporary.name) / "broken.db"
        with sqlite3.connect(broken) as connection:
            connection.execute("CREATE TABLE recipes (id TEXT PRIMARY KEY)")
        with self.assertRaisesRegex(recipe_quality.RecipeAuditError, "missing required"):
            recipe_quality.audit_database(broken)

    def test_cli_emits_json_and_does_not_change_database(self):
        self.insert("one", "Pancakes", meal_type="main")
        before = self.database.read_bytes()
        with mock.patch("builtins.print") as output:
            result = recipe_quality.main([
                "--db", str(self.database), "--redact-titles", "--max-findings", "10"
            ])
        self.assertEqual(result, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["schema"], recipe_quality.REPORT_SCHEMA)
        self.assertEqual(before, self.database.read_bytes())


if __name__ == "__main__":
    unittest.main()
