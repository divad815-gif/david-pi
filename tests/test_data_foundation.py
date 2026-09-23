import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault("DAVID_PI_PLATFORM_DATA", str(Path(IMPORT_DATA.name) / "platform"))

from modules.catalog import (  # noqa: E402
    CatalogConflict,
    CatalogItem,
    initialize_catalog,
    rebuild_catalog,
    replay_catalog_events,
    search_catalog,
    upsert_catalog_item,
)
from modules.platform import (  # noqa: E402
    FoundationConflict,
    MigrationChecksumConflict,
    MigrationStep,
    apply_domain_migrations,
    connect,
    emit_outbox_event,
    initialize_data_foundation,
    read_outbox,
    record_mutation_audit,
)
from modules.protection import (  # noqa: E402
    ManifestConflict,
    ManifestValidationError,
    canonical_manifest_bytes,
    initialize_protection,
    project_signed_manifest,
    protection_summary,
    validate_signed_manifest,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
KEY = b"test-only-manifest-signing-key-32-bytes"
OCCURRED = "2026-09-03T12:00:00+00:00"


def row_connection(path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


class PlatformFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "platform.db"

    def tearDown(self):
        self.temporary.cleanup()

    def test_successful_commit_is_not_reported_as_failure_when_close_raises(self):
        class CloseFaultConnection:
            def __init__(self):
                self.row_factory = None
                self.committed = False
                self.rolled_back = False

            def execute(self, *_args, **_kwargs):
                return self

            def commit(self):
                self.committed = True

            def rollback(self):
                self.rolled_back = True

            def close(self):
                raise sqlite3.OperationalError("synthetic close failure")

        connection = CloseFaultConnection()
        with patch("modules.platform.sqlite3.connect", return_value=connection):
            with connect(self.path) as opened:
                opened.execute("INSERT INTO example VALUES (1)")
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)

    def test_migration_is_additive_idempotent_and_preserves_legacy_rows(self):
        with connect(self.path) as connection:
            connection.execute("CREATE TABLE legacy(id INTEGER PRIMARY KEY,value TEXT NOT NULL)")
            connection.execute("INSERT INTO legacy VALUES(1,'untouched')")
        step = MigrationStep.from_sql(
            1, "add projection", ["CREATE TABLE projection(id TEXT PRIMARY KEY)"]
        )
        self.assertEqual(
            apply_domain_migrations(
                self.path, "catalog", [step], release_id="test-1", backup_set="backup-1"
            ),
            (1,),
        )
        self.assertEqual(apply_domain_migrations(self.path, "catalog", [step]), ())
        with connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT * FROM legacy").fetchall()[0]["value"], "untouched")
            ledger = connection.execute("SELECT * FROM schema_migrations").fetchone()
            self.assertEqual((ledger["domain"], ledger["version"], ledger["checksum"]), ("catalog", 1, step.checksum))

    def test_checksum_conflict_is_fail_closed(self):
        first = MigrationStep.from_sql(1, "schema", ["CREATE TABLE first(id INTEGER)"])
        conflict = MigrationStep.from_sql(1, "schema", ["CREATE TABLE second(id INTEGER)"])
        apply_domain_migrations(self.path, "media", [first])
        with self.assertRaises(MigrationChecksumConflict):
            apply_domain_migrations(self.path, "media", [conflict])
        with connect(self.path) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='second'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 1)

    def test_migration_batch_rolls_back_schema_and_ledger_together(self):
        first = MigrationStep.from_sql(1, "first", ["CREATE TABLE one(id INTEGER)"])

        def fail(connection):
            connection.execute("CREATE TABLE two(id INTEGER)")
            raise RuntimeError("deliberate")

        second = MigrationStep(2, "second", DIGEST_B, fail)
        with self.assertRaisesRegex(RuntimeError, "deliberate"):
            apply_domain_migrations(self.path, "media", [first, second])
        connection = row_connection(self.path)
        try:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("one", names)
            self.assertNotIn("two", names)
            self.assertNotIn("schema_migrations", names)
        finally:
            connection.close()

    def test_concurrent_migration_applies_once(self):
        step = MigrationStep.from_sql(1, "once", ["CREATE TABLE once_only(id INTEGER)"])
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def run():
            try:
                barrier.wait()
                results.append(apply_domain_migrations(self.path, "concurrent", [step]))
            except Exception as error:  # pragma: no cover - captured for assertion
                failures.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertCountEqual(results, [(1,), ()])

    def test_audit_is_metadata_only_append_only_and_idempotent(self):
        with connect(self.path) as connection:
            self.assertTrue(record_mutation_audit(
                connection, event_id="event-1", actor_id="owner-a", domain="recipes",
                object_id="recipe-1", action="update", request_id="request-1",
                before_digest=DIGEST_A, after_digest=DIGEST_B, occurred_at=OCCURRED,
            ))
            self.assertFalse(record_mutation_audit(
                connection, event_id="event-1", actor_id="owner-a", domain="recipes",
                object_id="recipe-1", action="update", request_id="request-1",
                before_digest=DIGEST_A, after_digest=DIGEST_B,
            ))
            self.assertTrue(record_mutation_audit(
                connection, event_id="event-2", actor_id="owner-a", domain="recipes",
                object_id="recipe-1", action="rollback", request_id="request-2",
                before_digest=DIGEST_B, after_digest=DIGEST_A, occurred_at=OCCURRED,
                rollback_of="event-1",
            ))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(mutation_audit)")}
            self.assertNotIn("payload", columns)
            self.assertNotIn("body", columns)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE mutation_audit SET action='rewrite' WHERE event_id='event-1'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM mutation_audit WHERE event_id='event-1'")
        with connect(self.path) as connection:
            with self.assertRaises(FoundationConflict):
                record_mutation_audit(
                    connection, event_id="event-1", actor_id="owner-b", domain="recipes",
                    object_id="recipe-1", action="update", request_id="request-1",
                    before_digest=DIGEST_A, after_digest=DIGEST_B, occurred_at=OCCURRED,
                )

    def test_outbox_key_replay_and_conflict_are_deterministic(self):
        with connect(self.path) as connection:
            first = emit_outbox_event(
                connection, domain="media", object_id="photo-1", event_type="upsert",
                object_version=1, payload_digest=DIGEST_A, occurred_at=OCCURRED,
            )
            retry = emit_outbox_event(
                connection, domain="media", object_id="photo-1", event_type="upsert",
                object_version=1, payload_digest=DIGEST_A,
            )
            emit_outbox_event(
                connection, domain="recipes", object_id="recipe-1", event_type="upsert",
                object_version=1, occurred_at=OCCURRED,
            )
            self.assertEqual(first, retry)
            self.assertEqual([event.domain for event in read_outbox(connection, domains=["media"])], ["media"])
            self.assertEqual([event.id for event in read_outbox(connection)], [1, 2])
            with self.assertRaises(FoundationConflict):
                emit_outbox_event(
                    connection, domain="media", object_id="photo-1", event_type="upsert",
                    object_version=1, payload_digest=DIGEST_B, occurred_at=OCCURRED,
                )


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.db"
        self.connection = row_connection(self.path)
        initialize_catalog(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def item(domain, item_id, title, *, owner=None, visibility="shared", version=1, deleted=None):
        return CatalogItem(
            domain=domain, item_id=item_id, owner_id=owner, visibility=visibility,
            title=title, subtitle="Common shelf", search_text="common discoverable",
            sort_time="2026-09-03T12:00:00+00:00", deleted_at=deleted,
            object_version=version, metadata={"kind": domain}, updated_at=OCCURRED,
        )

    def test_search_filters_acl_before_returning_results(self):
        items = [
            self.item("media", "shared", "Shared common"),
            self.item("media", "a-private", "Alice common", owner="alice", visibility="private"),
            self.item("media", "b-private", "Bob common", owner="bob", visibility="private"),
            self.item("recipes", "deleted", "Deleted common", deleted=OCCURRED),
        ]
        rebuild_catalog(self.connection, items, indexed_at=OCCURRED)
        public = search_catalog(self.connection, "common")
        alice = search_catalog(self.connection, "common", actor_id="alice")
        bob_media = search_catalog(self.connection, "common", actor_id="bob", domains=["media"])
        self.assertEqual({row["item_id"] for row in public}, {"shared"})
        self.assertEqual({row["item_id"] for row in alice}, {"shared", "a-private"})
        self.assertEqual({row["item_id"] for row in bob_media}, {"shared", "b-private"})
        self.assertNotIn("b-private", str(alice))

    def test_multiline_source_text_is_normalized_for_search(self):
        item = self.item("recipes", "soup", "Vegetable soup")
        item = CatalogItem(**{**item.__dict__, "search_text": "carrot\ncelery\tstock"})
        self.assertTrue(upsert_catalog_item(self.connection, item))
        self.assertEqual(search_catalog(self.connection, "celery")[0]["item_id"], "soup")

    def test_versions_are_monotonic_and_same_version_conflicts(self):
        original = self.item("media", "one", "Version one", version=1)
        self.assertTrue(upsert_catalog_item(self.connection, original))
        self.assertFalse(upsert_catalog_item(self.connection, original))
        with self.assertRaises(CatalogConflict):
            upsert_catalog_item(self.connection, self.item("media", "one", "Changed", version=1))
        self.assertTrue(upsert_catalog_item(self.connection, self.item("media", "one", "Version two", version=2)))
        self.assertFalse(upsert_catalog_item(self.connection, original))
        self.assertEqual(search_catalog(self.connection, "version")[0]["object_version"], 2)

    def test_rebuild_is_deterministic_and_rolls_back_on_invalid_input(self):
        items = [
            self.item("recipes", "two", "Recipe result"),
            self.item("media", "one", "Media result"),
        ]
        rebuild_catalog(
            self.connection, items, checkpoints={"recipes": 4, "media": 3}, indexed_at=OCCURRED
        )
        before_items = [tuple(row) for row in self.connection.execute("SELECT rowid,* FROM catalog_items ORDER BY rowid")]
        before_fts = [tuple(row) for row in self.connection.execute("SELECT rowid,* FROM catalog_fts ORDER BY rowid")]
        rebuild_catalog(
            self.connection, reversed(items), checkpoints={"media": 3, "recipes": 4}, indexed_at=OCCURRED
        )
        self.assertEqual(before_items, [tuple(row) for row in self.connection.execute("SELECT rowid,* FROM catalog_items ORDER BY rowid")])
        self.assertEqual(before_fts, [tuple(row) for row in self.connection.execute("SELECT rowid,* FROM catalog_fts ORDER BY rowid")])
        with self.assertRaises(ValueError):
            rebuild_catalog(self.connection, [items[0], items[0]], indexed_at=OCCURRED)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0], 2)

    def test_outbox_replay_is_idempotent_and_matches_full_rebuild(self):
        outbox_path = Path(self.temporary.name) / "outbox.db"
        with connect(outbox_path) as outbox:
            initialize_data_foundation(outbox)
            emit_outbox_event(outbox, domain="media", object_id="one", event_type="upsert", object_version=1, occurred_at=OCCURRED)
            emit_outbox_event(outbox, domain="recipes", object_id="two", event_type="upsert", object_version=1, occurred_at=OCCURRED)
            emit_outbox_event(outbox, domain="media", object_id="one", event_type="upsert", object_version=2, occurred_at="2026-09-03T12:01:00+00:00")
            events = read_outbox(outbox)
        current = {
            ("media", "one"): self.item("media", "one", "Media current", version=2),
            ("recipes", "two"): self.item("recipes", "two", "Recipe current", version=1),
        }
        resolver = lambda domain, item_id: current.get((domain, item_id))
        self.assertEqual(replay_catalog_events(self.connection, events, resolver, indexed_at=OCCURRED), 3)
        self.assertEqual(replay_catalog_events(self.connection, events, resolver, indexed_at=OCCURRED), 0)
        replay_rows = [tuple(row) for row in self.connection.execute(
            "SELECT domain,item_id,title,object_version FROM catalog_items ORDER BY domain,item_id"
        )]
        second = row_connection(Path(self.temporary.name) / "rebuilt.db")
        try:
            rebuild_catalog(
                second, current.values(), checkpoints={"media": 3, "recipes": 2}, indexed_at=OCCURRED
            )
            rebuilt_rows = [tuple(row) for row in second.execute(
                "SELECT domain,item_id,title,object_version FROM catalog_items ORDER BY domain,item_id"
            )]
        finally:
            second.close()
        self.assertEqual(replay_rows, rebuilt_rows)

    def test_replay_failure_rolls_back_items_and_checkpoints(self):
        from modules.platform import OutboxEvent

        events = [
            OutboxEvent(1, DIGEST_A, "media", "one", "upsert", 1, None, OCCURRED),
            OutboxEvent(2, DIGEST_B, "media", "two", "upsert", 1, None, OCCURRED),
        ]

        def resolver(domain, item_id):
            if item_id == "two":
                raise RuntimeError("resolver failed")
            return self.item(domain, item_id, "Temporary")

        with self.assertRaisesRegex(RuntimeError, "resolver failed"):
            replay_catalog_events(self.connection, events, resolver, indexed_at=OCCURRED)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM catalog_checkpoints").fetchone()[0], 0)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "protection.db"
        self.connection = row_connection(self.path)
        initialize_protection(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    @staticmethod
    def payload(snapshot="snapshot-1", completed="2026-09-03T12:00:00Z"):
        return {
            "schema_version": 3,
            "created_at": "2026-09-03T11:59:59Z",
            "snapshot_id": snapshot,
            "window": {
                "started_at": "2026-09-03T11:59:00Z",
                "completed_at": completed,
                "writers_quiesced": True,
            },
            "source": {
                "device": "/dev/source", "filesystem_uuid": "source-uuid",
                "st_dev": 101,
            },
            "backup": {
                "device": "/dev/backup", "filesystem_uuid": "backup-uuid",
                "st_dev": 202,
            },
            "release": {"portal_image": "david-family-photos:test"},
            "databases": [
                {
                    "path": "databases/platform/recipes.db",
                    "byte_size": 300,
                    "mode": "0640",
                    "uid": 10001,
                    "gid": 10001,
                    "sha256": DIGEST_A,
                    "quick_check": "ok",
                    "foreign_key_errors": 0,
                }
            ],
            "database_tree": {
                "file_count": 1,
                "directory_count": 1,
                "symlink_count": 0,
                "logical_bytes": 300,
                "unique_inode_count": 1,
                "unique_inode_bytes": 300,
                "tree_sha256": DIGEST_B,
            },
            "content": {
                "file_count": 10,
                "directory_count": 4,
                "symlink_count": 1,
                "logical_bytes": 1000,
                "unique_inode_count": 8,
                "unique_inode_bytes": 800,
                "tree_sha256": DIGEST_A,
            },
            "configuration": {
                "file_count": 3,
                "directory_count": 2,
                "symlink_count": 0,
                "logical_bytes": 300,
                "unique_inode_count": 3,
                "unique_inode_bytes": 300,
                "tree_sha256": DIGEST_B,
            },
        }

    @staticmethod
    def signed(payload):
        encoded = canonical_manifest_bytes(payload)
        return {
            **payload,
            "integrity": {
                "algorithm": "hmac-sha256",
                "payload_sha256": hashlib.sha256(encoded).hexdigest(),
                "signature": hmac.new(KEY, encoded, hashlib.sha256).hexdigest(),
            },
        }

    def test_schema_v3_manifest_projects_idempotently_and_summarizes(self):
        manifest = self.signed(self.payload())
        validated = validate_signed_manifest(manifest, KEY)
        self.assertEqual(validated.database_count, 1)
        self.assertEqual(validated.content.file_count, 10)
        self.assertTrue(project_signed_manifest(self.connection, manifest, KEY, projected_at=OCCURRED))
        self.assertFalse(project_signed_manifest(self.connection, manifest, KEY, projected_at=OCCURRED))
        summary = protection_summary(self.connection)
        self.assertEqual(summary["state"], "verified")
        self.assertEqual(summary["latest_snapshot"]["snapshot_id"], "snapshot-1")
        self.assertEqual(summary["latest_snapshot"]["database_count"], 1)
        self.assertEqual(summary["latest_snapshot"]["trees"]["content"]["tree_sha256"], DIGEST_A)
        self.assertEqual(summary["latest_snapshot"]["trees"]["database"]["file_count"], 1)

    def test_schema_matches_backup_producer_and_does_not_project_content_paths(self):
        manifest = self.signed(self.payload())
        project_signed_manifest(self.connection, manifest, KEY)
        database = self.connection.execute("SELECT * FROM protected_databases").fetchone()
        self.assertEqual(
            tuple(
                database[key]
                for key in (
                    "path", "byte_size", "mode", "uid", "gid", "sha256",
                    "quick_check", "foreign_key_errors",
                )
            ),
            (
                "databases/platform/recipes.db", 300, "0640", 10001, 10001,
                DIGEST_A, "ok", 0,
            ),
        )
        columns = {
            row[1]
            for table in ("protection_snapshots", "protection_trees")
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        for forbidden in ("relative_path", "object_path", "filename", "file_path"):
            self.assertNotIn(forbidden, columns)
        self.assertNotIn("originals", json.dumps(protection_summary(self.connection)))

    def test_current_v3_metadata_omissions_and_malformed_values_fail_closed(self):
        invalid_payloads = []
        for field in ("mode", "uid", "gid"):
            missing = self.payload()
            del missing["databases"][0][field]
            invalid_payloads.append(missing)
        for tree_name in ("content", "configuration"):
            for field in ("directory_count", "symlink_count"):
                missing = self.payload()
                del missing[tree_name][field]
                invalid_payloads.append(missing)
        for side in ("source", "backup"):
            missing = self.payload(); del missing[side]["st_dev"]; invalid_payloads.append(missing)
        missing_tree = self.payload(); del missing_tree["database_tree"]; invalid_payloads.append(missing_tree)
        extra_tree = self.payload(); extra_tree["database_tree"]["unexpected"] = 1; invalid_payloads.append(extra_tree)
        same_device = self.payload(); same_device["backup"]["st_dev"] = same_device["source"]["st_dev"]; invalid_payloads.append(same_device)
        tree_count = self.payload(); tree_count["database_tree"]["file_count"] = 2; invalid_payloads.append(tree_count)
        tree_link = self.payload(); tree_link["database_tree"]["symlink_count"] = 1; invalid_payloads.append(tree_link)
        bad_mode = self.payload(); bad_mode["databases"][0]["mode"] = "9999"; invalid_payloads.append(bad_mode)
        numeric_mode = self.payload(); numeric_mode["databases"][0]["mode"] = 640; invalid_payloads.append(numeric_mode)
        negative_uid = self.payload(); negative_uid["databases"][0]["uid"] = -1; invalid_payloads.append(negative_uid)
        boolean_gid = self.payload(); boolean_gid["databases"][0]["gid"] = True; invalid_payloads.append(boolean_gid)
        negative_directories = self.payload(); negative_directories["content"]["directory_count"] = -1; invalid_payloads.append(negative_directories)
        boolean_symlinks = self.payload(); boolean_symlinks["configuration"]["symlink_count"] = False; invalid_payloads.append(boolean_symlinks)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ManifestValidationError):
                    validate_signed_manifest(self.signed(payload), KEY)

    def test_tampering_wrong_key_and_weak_key_fail_closed(self):
        manifest = self.signed(self.payload())
        manifest["snapshot_id"] = "forged"
        with self.assertRaisesRegex(ManifestValidationError, "payload digest"):
            validate_signed_manifest(manifest, KEY)
        with self.assertRaisesRegex(ManifestValidationError, "signature"):
            validate_signed_manifest(self.signed(self.payload()), b"x" * 32)
        with self.assertRaisesRegex(ManifestValidationError, "32 bytes"):
            validate_signed_manifest(self.signed(self.payload()), b"weak")

    def test_manifest_rejects_unsafe_or_unverified_backup_metadata(self):
        invalid_payloads = []
        same = self.payload(); same["backup"]["filesystem_uuid"] = same["source"]["filesystem_uuid"]; invalid_payloads.append(same)
        traversal = self.payload(); traversal["databases"][0]["path"] = "../recipes.db"; invalid_payloads.append(traversal)
        duplicate = self.payload(); duplicate["databases"].append(dict(duplicate["databases"][0])); invalid_payloads.append(duplicate)
        foreign_keys = self.payload(); foreign_keys["databases"][0]["foreign_key_errors"] = 1; invalid_payloads.append(foreign_keys)
        quick_check = self.payload(); quick_check["databases"][0]["quick_check"] = "corrupt"; invalid_payloads.append(quick_check)
        active = self.payload(); active["window"]["writers_quiesced"] = False; invalid_payloads.append(active)
        string_size = self.payload(); string_size["databases"][0]["byte_size"] = "300"; invalid_payloads.append(string_size)
        impossible_tree = self.payload(); impossible_tree["content"]["unique_inode_count"] = 11; invalid_payloads.append(impossible_tree)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ManifestValidationError):
                    validate_signed_manifest(self.signed(payload), KEY)

    def test_manifest_rejects_impossible_or_noncanonical_utc_timestamps(self):
        for field, value in (
            ("created_at", "2026-19-39T29:59:59Z"),
            ("created_at", "2026-09-03T11:59:59+00:00"),
            ("started_at", "2026-02-30T11:59:00Z"),
        ):
            payload = self.payload()
            if field == "started_at":
                payload["window"][field] = value
            else:
                payload[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ManifestValidationError, "UTC|timestamp"):
                    validate_signed_manifest(self.signed(payload), KEY)

    def test_legacy_projection_without_v3_tree_evidence_is_not_verified(self):
        self.connection.execute(
            """INSERT INTO protection_snapshots
               (snapshot_id,created_at,started_at,completed_at,source_fs_uuid,
                backup_fs_uuid,source_st_dev,backup_st_dev,portal_image,
                database_count,payload_sha256,projected_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "legacy-v2",
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:01:00Z",
                "source",
                "backup",
                None,
                None,
                "fixture:image",
                1,
                DIGEST_A,
                OCCURRED,
            ),
        )
        summary = protection_summary(self.connection)
        self.assertEqual(summary["state"], "unverified_legacy")
        self.assertEqual(
            summary["verification_reason"], "schema_v3_tree_evidence_missing"
        )

    def test_missing_database_tree_evidence_downgrades_an_otherwise_complete_row(self):
        manifest = self.signed(self.payload())
        project_signed_manifest(self.connection, manifest, KEY, projected_at=OCCURRED)
        self.connection.execute(
            "DELETE FROM protection_database_trees WHERE snapshot_id='snapshot-1'"
        )
        summary = protection_summary(self.connection)
        self.assertEqual(summary["state"], "unverified_legacy")
        self.assertEqual(
            summary["verification_reason"], "schema_v3_tree_evidence_missing"
        )

    def test_projection_conflict_and_transaction_rollback_preserve_prior_snapshot(self):
        original = self.signed(self.payload())
        project_signed_manifest(self.connection, original, KEY, projected_at=OCCURRED)
        changed = self.payload(); changed["content"]["file_count"] = 11
        conflict = self.signed(changed)
        with self.assertRaises(ManifestConflict):
            project_signed_manifest(self.connection, conflict, KEY)
        self.connection.execute(
            """CREATE TRIGGER reject_next_database BEFORE INSERT ON protected_databases
               WHEN NEW.snapshot_id='snapshot-2' BEGIN SELECT RAISE(ABORT,'deliberate'); END"""
        )
        second_payload = self.payload("snapshot-2", "2026-09-04T12:00:00Z")
        second_payload["window"]["started_at"] = "2026-09-04T11:59:00Z"
        second_payload["created_at"] = "2026-09-04T11:58:59Z"
        with self.assertRaises(sqlite3.IntegrityError):
            project_signed_manifest(self.connection, self.signed(second_payload), KEY)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM protection_snapshots").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM protection_trees").fetchone()[0], 2)

    def test_older_snapshot_does_not_replace_latest_summary(self):
        newer_payload = self.payload("newer", "2026-09-04T12:00:00Z")
        newer_payload["window"]["started_at"] = "2026-09-04T11:59:00Z"
        newer_payload["created_at"] = "2026-09-04T11:58:59Z"
        older_payload = self.payload("older", "2026-09-02T18:00:00Z")
        older_payload["window"]["started_at"] = "2026-09-02T17:59:00Z"
        older_payload["created_at"] = "2026-09-02T17:58:59Z"
        project_signed_manifest(self.connection, self.signed(newer_payload), KEY)
        project_signed_manifest(self.connection, self.signed(older_payload), KEY)
        self.assertEqual(protection_summary(self.connection)["latest_snapshot"]["snapshot_id"], "newer")


if __name__ == "__main__":
    unittest.main()
