import base64
import importlib
import io
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


_OWN_TEST_DATA = None
if "app" not in sys.modules:
    _OWN_TEST_DATA = tempfile.TemporaryDirectory()
    _data = Path(_OWN_TEST_DATA.name)
    os.environ["PHOTO_DATA"] = str(_data)
    os.environ["DAVID_PI_DISABLE_METRICS"] = "1"
    os.environ["DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING"] = "1"
    os.environ["PIHOLE_SUMMARY"] = str(_data / "pihole-summary.json")
    os.environ["DAVID_PI_PLATFORM_DATA"] = str(_data / "platform")
    os.environ["DAVID_PI_FILES_DATA"] = str(_data / "files")
    os.environ["DAVID_PI_CHAT_DATA"] = str(_data / "chat")
    os.environ["DAVID_PI_CHAT_KEY_B64"] = base64.b64encode(
        b"david-pi-media-metadata-test-key"
    ).decode()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
portal = importlib.import_module("app")
chat_module = importlib.import_module("modules.chat")
portal.app.config["TESTING"] = True


class MediaMetadataProductTests(unittest.TestCase):
    def setUp(self):
        self._original_portal_storage = {
            name: getattr(portal, name)
            for name in (
                "DATA", "ORIGINALS", "PREVIEWS", "VIEWER_PREVIEWS", "THUMBS",
                "INCOMING", "QUARANTINE", "DB_PATH", "DATA_STORAGE",
                "ORIGINAL_STORAGE", "PREVIEW_STORAGE", "VIEWER_PREVIEW_STORAGE",
                "THUMB_STORAGE", "INCOMING_STORAGE", "QUARANTINE_STORAGE",
            )
        }
        self._original_chat_db_path = chat_module.DB_PATH
        self._database_directory = tempfile.TemporaryDirectory()
        test_root = Path(self._database_directory.name)
        portal.DATA = test_root
        for name, relative in (
            ("ORIGINALS", "originals"),
            ("PREVIEWS", "previews"),
            ("VIEWER_PREVIEWS", "viewer-previews"),
            ("THUMBS", "thumbs"),
            ("INCOMING", "incoming"),
            ("QUARANTINE", "quarantine"),
        ):
            path = test_root / relative
            portal.ensure_restricted_directory(path)
            setattr(portal, name, path)
        portal.DB_PATH = test_root / "photos.db"
        portal.DATA_STORAGE = portal.PinnedStorageRoot(portal.DATA)
        portal.ORIGINAL_STORAGE = portal.PinnedStorageRoot(portal.ORIGINALS)
        portal.PREVIEW_STORAGE = portal.PinnedStorageRoot(portal.PREVIEWS)
        portal.VIEWER_PREVIEW_STORAGE = portal.PinnedStorageRoot(portal.VIEWER_PREVIEWS)
        portal.THUMB_STORAGE = portal.PinnedStorageRoot(portal.THUMBS)
        portal.INCOMING_STORAGE = portal.PinnedStorageRoot(portal.INCOMING)
        portal.QUARANTINE_STORAGE = portal.PinnedStorageRoot(portal.QUARANTINE)
        chat_module.DB_PATH = test_root / "platform" / "chat.db"
        chat_module.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        chat_module.migrate(chat_module.DB_PATH, chat_module._migrate)
        portal.initialize()
        self.client = self.make_client("david@example.test", "David")

    def tearDown(self):
        for name, value in self._original_portal_storage.items():
            setattr(portal, name, value)
        chat_module.DB_PATH = self._original_chat_db_path
        self._database_directory.cleanup()

    def make_client(self, login, name, *, csrf=True):
        client = portal.app.test_client()
        client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = login
        client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = name
        if csrf:
            token = f"media-metadata-{name.lower()}-csrf-token-long-enough"
            client.set_cookie("david_pi_csrf", token, domain="localhost")
            client.environ_base["HTTP_X_CSRF_TOKEN"] = token
        return client

    def add_photo(
        self,
        photo_id,
        *,
        owner_id="david@example.test",
        owner_name="David",
        visibility="shared",
        original_name=None,
        content_type="image/jpeg",
        captured_at="2026-01-01T00:00:00+00:00",
        deleted_at=None,
    ):
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO photos
                   (id,original_name,stored_path,preview_name,thumb_name,content_type,
                    byte_size,sha256,taken_at,capture_timestamp,uploaded_at,uploaded_by,
                    deleted_at,owner_id,owner_name,visibility)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    photo_id,
                    original_name or f"{photo_id}.jpg",
                    f"{photo_id}.original",
                    f"{photo_id}.preview",
                    f"{photo_id}.thumb",
                    content_type,
                    10,
                    f"hash-{photo_id}",
                    captured_at,
                    captured_at,
                    captured_at,
                    owner_name or "Legacy",
                    deleted_at,
                    owner_id,
                    owner_name,
                    visibility,
                ),
            )

    def add_collection(self, collection_id, *, owner_id="david@example.test"):
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO collections
                   (id,name,created_at,created_by,owner_id,owner_name,visibility)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    collection_id,
                    "Summer album",
                    "2026-01-01T00:00:00+00:00",
                    "David",
                    owner_id,
                    "David",
                    "shared",
                ),
            )

    def add_membership(self, collection_id, photo_id):
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO collection_photos
                   (collection_id,photo_id,added_at,added_by_id,version)
                   VALUES (?,?,?,?,1)""",
                (collection_id, photo_id, "2026-01-01", "david@example.test"),
            )

    def caption(self, photo_id, value, *, version=0, media_version=1, client=None):
        return (client or self.client).put(
            f"/api/photos/{photo_id}/caption",
            json={
                "caption": value,
                "caption_version": version,
                "media_version": media_version,
            },
        )

    def favorite(self, photo_id, value, *, version=0, media_version=1, client=None, **extra):
        return (client or self.client).put(
            f"/api/photos/{photo_id}/favorite",
            json={
                "favorite": value,
                "state_version": version,
                "media_version": media_version,
                **extra,
            },
        )

    def search(self, query, *, client=None, **options):
        return (client or self.client).post(
            "/api/photos/search", json={"query": query, **options}
        )

    def test_additive_checksummed_migration_preserves_legacy_rows_and_cascades_only_on_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE photos(id TEXT PRIMARY KEY,payload BLOB)")
                connection.execute(
                    "INSERT INTO photos(id,payload) VALUES (?,?)",
                    ("legacy", sqlite3.Binary(b"\x00legacy\xff")),
                )
            applied = portal.apply_domain_migrations(
                database, "media_metadata", portal.MEDIA_METADATA_MIGRATIONS
            )
            self.assertEqual(applied, (1,))
            self.assertEqual(
                portal.apply_domain_migrations(
                    database, "media_metadata", portal.MEDIA_METADATA_MIGRATIONS
                ),
                (),
            )
            with portal.connect(database) as connection:
                self.assertEqual(
                    tuple(connection.execute("SELECT id,payload FROM photos").fetchone()),
                    ("legacy", b"\x00legacy\xff"),
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM media_captions").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM media_personal_state").fetchone()[0],
                    0,
                )
                index = connection.execute(
                    """SELECT sql FROM sqlite_master
                       WHERE type='index' AND name='media_personal_favorite_idx'"""
                ).fetchone()[0]
                self.assertIn("principal_id, favorite, photo_id", index)
                ledger = connection.execute(
                    """SELECT name,checksum FROM schema_migrations
                       WHERE domain='media_metadata' AND version=1"""
                ).fetchone()
                self.assertEqual(ledger["name"], "add-caption-and-personal-media-state")
                self.assertEqual(ledger["checksum"], portal.MEDIA_METADATA_MIGRATIONS[0].checksum)
                connection.execute(
                    """INSERT INTO media_captions
                       (photo_id,caption,version,created_at,updated_at)
                       VALUES ('legacy','',1,'now','now')"""
                )
                connection.execute(
                    """INSERT INTO media_personal_state
                       (photo_id,principal_id,favorite,version,created_at,updated_at)
                       VALUES ('legacy','david@example.test',1,1,'now','now')"""
                )
                connection.execute("DELETE FROM photos WHERE id='legacy'")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_captions").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_personal_state").fetchone()[0], 0)

    def test_favorites_are_per_verified_principal_and_legacy_shared_media_is_supported(self):
        self.add_photo("owned-shared")
        self.add_photo("legacy-shared", owner_id=None, owner_name=None)
        self.add_photo(
            "legacy-private", owner_id=None, owner_name=None, visibility="private"
        )
        diana = self.make_client("diana@example.test", "Diana")

        david_saved = self.favorite(
            "owned-shared", True, principal_id="diana@example.test"
        )
        self.assertEqual(david_saved.status_code, 200)
        self.assertTrue(david_saved.get_json()["favorite"])
        self.assertEqual(self.favorite("legacy-shared", True).status_code, 200)
        self.assertEqual(
            self.favorite("legacy-private", True).status_code,
            404,
        )

        diana_view = diana.get("/api/photos?scope=visible").get_json()["photos"]
        diana_owned = next(item for item in diana_view if item["id"] == "owned-shared")
        self.assertFalse(diana_owned["favorite"])
        diana_saved = self.favorite("owned-shared", True, client=diana)
        self.assertEqual(diana_saved.status_code, 200)

        with portal.db() as connection:
            rows = connection.execute(
                """SELECT principal_id,favorite FROM media_personal_state
                   WHERE photo_id='owned-shared' ORDER BY principal_id"""
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("david@example.test", 1), ("diana@example.test", 1)],
        )
        self.assertEqual(self.caption("legacy-shared", "Not claimable").status_code, 403)
        self.assertEqual(
            self.caption(
                "owned-shared", "Not mine", version=999,
                media_version=999, client=diana,
            ).status_code,
            403,
        )
        self.assertEqual(self.caption("legacy-private", "Hidden").status_code, 404)

    def test_caption_normalization_validation_noop_and_empty_row_semantics(self):
        self.add_photo("captioned")
        saved = self.caption("captioned", "  Cafe\u0301 memories  ")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["caption"], "Café memories")
        self.assertEqual(saved.get_json()["caption_version"], 1)

        repeated = self.caption("captioned", "Café memories", version=1)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.get_json()["caption_version"], 1)
        emptied = self.caption("captioned", "", version=1)
        self.assertEqual(emptied.status_code, 200)
        self.assertEqual(emptied.get_json()["caption_version"], 2)
        repeated_empty = self.caption("captioned", "", version=2)
        self.assertEqual(repeated_empty.get_json()["caption_version"], 2)

        with portal.db() as connection:
            row = connection.execute(
                "SELECT caption,version FROM media_captions WHERE photo_id='captioned'"
            ).fetchone()
            audit_count = connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE domain='media_caption' AND object_id='captioned'"""
            ).fetchone()[0]
        self.assertEqual(tuple(row), ("", 2))
        self.assertEqual(audit_count, 2)
        for invalid in ("line\nbreak", "unsafe\u202econtrol", "x" * 1001):
            with self.subTest(invalid=invalid[:20]):
                self.assertEqual(
                    self.caption("captioned", invalid, version=2).status_code, 400
                )

    def test_cas_conflicts_noops_and_audit_failures_never_partially_write(self):
        self.add_photo("cas-item")
        first = self.favorite("cas-item", True)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["state_version"], 1)
        no_op = self.favorite("cas-item", True, version=1)
        self.assertEqual(no_op.get_json()["state_version"], 1)
        self.assertEqual(self.favorite("cas-item", False, version=0).status_code, 409)
        self.assertEqual(self.caption("cas-item", "one").status_code, 200)
        self.assertEqual(self.caption("cas-item", "stale", version=0).status_code, 409)
        self.assertEqual(
            self.favorite("cas-item", False, version=1, media_version=2).status_code,
            409,
        )

        with portal.db() as connection:
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE domain='media_personal_state' AND action='favorite'
                     AND object_id=?""",
                (portal._personal_media_object_id("cas-item", "david@example.test"),),
            ).fetchone()[0], 1)

        self.add_photo("rollback-item")
        with patch.object(
            portal, "audit_mutation", side_effect=RuntimeError("audit unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.caption("rollback-item", "must rollback")
        with portal.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM media_captions WHERE photo_id='rollback-item'"
            ).fetchone())
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM domain_outbox
                   WHERE domain='media_caption' AND object_id='rollback-item'"""
            ).fetchone()[0], 0)

    def test_two_clients_using_one_state_version_get_one_success_and_one_conflict(self):
        self.add_photo("concurrent-favorite")
        barrier = threading.Barrier(2)
        statuses = []
        lock = threading.Lock()

        def update():
            client = self.make_client("david@example.test", "David")
            barrier.wait()
            response = self.favorite(
                "concurrent-favorite", True, version=0, client=client
            )
            with lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=update) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(statuses), [200, 409])

    def test_caption_and_favorite_survive_private_trash_restore_without_disclosure(self):
        self.add_photo("lifecycle-item")
        self.assertEqual(self.caption("lifecycle-item", "Private family note").status_code, 200)
        self.assertEqual(self.favorite("lifecycle-item", True).status_code, 200)
        made_private = self.client.patch(
            "/api/photos/visibility",
            json={
                "items": [{"id": "lifecycle-item", "version": 1}],
                "visibility": "private",
            },
        )
        self.assertEqual(made_private.status_code, 200)
        diana = self.make_client("diana@example.test", "Diana", csrf=False)
        hidden = diana.get("/api/photos?scope=visible")
        self.assertEqual(hidden.status_code, 200)
        self.assertEqual(hidden.get_json()["photos"], [])

        trashed = self.client.post(
            "/api/photos/trash",
            json={"items": [{"id": "lifecycle-item", "version": 2}]},
        )
        self.assertEqual(trashed.status_code, 200)
        visible_trash = self.client.get(
            "/api/photos/deleted?scope=visible"
        ).get_json()
        self.assertEqual(visible_trash["scope"], "visible")
        self.assertEqual(
            [item["id"] for item in visible_trash["photos"]],
            ["lifecycle-item"],
        )
        self.assertEqual(
            diana.get("/api/photos/deleted?scope=visible").get_json()["photos"],
            [],
        )
        deleted = self.client.get("/api/photos/deleted?view=mine").get_json()["photos"]
        self.assertEqual(deleted[0]["caption"], "Private family note")
        self.assertTrue(deleted[0]["favorite"])
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT caption FROM media_captions WHERE photo_id='lifecycle-item'"
            ).fetchone()[0], "Private family note")
            self.assertEqual(connection.execute(
                """SELECT favorite FROM media_personal_state
                   WHERE photo_id='lifecycle-item' AND principal_id='david@example.test'"""
            ).fetchone()[0], 1)
        restored = self.client.post(
            "/api/photos/restore",
            json={"items": [{"id": "lifecycle-item", "version": 3}]},
        )
        self.assertEqual(restored.status_code, 200)
        mine = self.client.get("/api/photos?scope=mine").get_json()["photos"]
        self.assertEqual(mine[0]["caption"], "Private family note")
        self.assertTrue(mine[0]["favorite"])

        shared = self.client.patch(
            "/api/photos/visibility",
            json={
                "items": [{"id": "lifecycle-item", "version": 4}],
                "visibility": "shared",
            },
        )
        self.assertEqual(shared.status_code, 200)
        diana_item = diana.get("/api/photos?scope=visible").get_json()["photos"][0]
        self.assertEqual(diana_item["caption"], "Private family note")
        self.assertFalse(diana_item["favorite"])

    def removed_search_is_wildcard_safe_parameterized_and_never_echoes_query(self):
        self.add_photo("literal", original_name="receipt_%_final.jpg")
        self.add_photo("ordinary", original_name="ordinary.jpg")
        self.assertEqual(self.caption("literal", "<img src=x onerror=alert(1)>").status_code, 200)
        literal = self.search("%_", scope="visible")
        self.assertEqual(literal.status_code, 200)
        self.assertEqual([item["id"] for item in literal.get_json()["photos"]], ["literal"])
        self.assertNotIn("query", literal.get_json())
        injected = self.search("' OR 1=1 --", scope="visible")
        self.assertEqual(injected.status_code, 200)
        self.assertEqual(injected.get_json()["photos"], [])
        xss = self.search("onerror", scope="visible").get_json()["photos"][0]
        self.assertEqual(xss["caption"], "<img src=x onerror=alert(1)>")
        self.assertEqual(self.search("bad\nquery", scope="visible").status_code, 400)
        self.assertEqual(
            self.search("x" * 201, scope="visible").status_code,
            400,
        )
        oversized = self.client.post(
            "/api/photos/search",
            data=b"{" + b"x" * (portal.MAX_MEDIA_SEARCH_BODY_BYTES + 1),
            content_type="application/json",
        )
        self.assertEqual(oversized.status_code, 413)
        for response in (literal, injected, oversized):
            self.assertIn("private", response.headers["Cache-Control"])
            self.assertIn("no-store", response.headers["Cache-Control"])

    def removed_metadata_search_is_deterministic_when_optional_backend_is_disabled(self):
        self.add_photo("metadata-only", original_name="quiet-garden.jpg")
        with patch.dict(os.environ, {"DAVID_PI_MEDIA_SEARCH_ENABLED": "0"}):
            first = self.search("garden", scope="visible").get_json()
            second = self.search("garden", scope="visible").get_json()
        self.assertEqual(first["photos"], second["photos"])
        self.assertEqual(first["search_state"], {
            "mode": "metadata",
            "state": "disabled",
            "available": False,
            "reason_code": "backend_disabled",
            "model_id": "",
            "index_version": "",
            "indexed_items": 0,
            "total_items": 1,
        })
        self.assertNotIn("query", first)

    def removed_invalid_or_unauthenticated_optional_backend_fails_to_metadata(self):
        self.add_photo("fallback-item", original_name="quiet-garden.jpg")
        with patch.dict(
            os.environ,
            {
                "DAVID_PI_MEDIA_SEARCH_ENABLED": "1",
                "DAVID_PI_MEDIA_SEARCH_TIMEOUT": "nan",
            },
        ):
            invalid = self.search("garden", scope="visible")
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(
            invalid.get_json()["search_state"]["reason_code"],
            "configuration_invalid",
        )
        self.assertEqual(
            [item["id"] for item in invalid.get_json()["photos"]],
            ["fallback-item"],
        )

        class Config:
            enabled = True

        class Manifest:
            def publish(self, items):
                return "e" * 64, len(items)

        class Client:
            def sync(self, generation):
                raise portal.MediaSearchError("response authentication failed")

        runtime = (
            Config(),
            Client(),
            Manifest(),
            portal.SearchCursorSigner(b"fallback-test-secret-at-least-32-bytes"),
        )
        with patch.object(portal, "_media_search_runtime", return_value=runtime):
            unauthenticated = self.search("garden", scope="visible")
        self.assertEqual(unauthenticated.status_code, 200)
        self.assertEqual(
            unauthenticated.get_json()["search_state"]["reason_code"],
            "backend_unavailable",
        )
        self.assertEqual(
            [item["id"] for item in unauthenticated.get_json()["photos"]],
            ["fallback-item"],
        )

    def removed_visual_results_are_reauthorized_against_live_actor_scope(self):
        self.add_photo(
            "david-private-visual",
            visibility="private",
            original_name="opaque-one.jpg",
        )
        self.add_photo(
            "household-shared-visual",
            visibility="shared",
            original_name="opaque-two.jpg",
        )
        diana = self.make_client("diana@example.test", "Diana")
        generation = "a" * 64

        class Config:
            enabled = True

        class Manifest:
            published = None

            def publish(self, items):
                self.published = items
                return generation, len(items)

        class Client:
            def sync(self, value):
                self.assert_generation = value

            def search(self, query, value, *, limit):
                return {
                    "schema_version": 1,
                    "state": "ready",
                    "model_id": "visual:test",
                    "index_version": "index-v1:test",
                    "manifest_generation": value,
                    "partial": False,
                    "hits": [
                        {"id": "david-private-visual", "score": 0.99},
                        {"id": "household-shared-visual", "score": 0.80},
                    ],
                }

        manifest = Manifest()
        signer = portal.SearchCursorSigner(b"portal-search-test-secret-at-least-32-bytes")
        with patch.object(
            portal,
            "_media_search_runtime",
            return_value=(Config(), Client(), manifest, signer),
        ):
            response = self.search(
                "semantic scene", client=diana, scope="visible"
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(
            [item["id"] for item in payload["photos"]],
            ["household-shared-visual"],
        )
        self.assertNotIn("david-private-visual", response.get_data(as_text=True))
        self.assertEqual(payload["search_state"]["mode"], "hybrid")
        self.assertTrue(payload["search_state"]["available"])
        self.assertEqual(
            set(payload["search_state"]),
            {
                "mode", "state", "available", "reason_code", "model_id",
                "index_version", "indexed_items", "total_items",
            },
        )
        self.assertNotIn("search_backend", payload)
        # The content-neutral manifest may cover all active derivatives, but
        # it never includes policy fields or user search text.
        self.assertEqual(
            set(manifest.published[0]),
            {"id", "derivative", "media_version", "kind"},
        )
        self.assertNotIn("semantic scene", str(manifest.published))

    def removed_hybrid_cursor_cannot_cross_actor_query_or_scope(self):
        self.add_photo("visual-a", original_name="opaque-a.jpg")
        self.add_photo("visual-b", original_name="opaque-b.jpg")
        generation = "b" * 64

        class Config:
            enabled = True

        class Manifest:
            def publish(self, items):
                return generation, len(items)

        class Client:
            def sync(self, value):
                return None

            def search(self, query, value, *, limit):
                return {
                    "schema_version": 1,
                    "state": "ready",
                    "model_id": "visual:test",
                    "index_version": "index-v1:fixed",
                    "manifest_generation": value,
                    "partial": False,
                    "hits": [
                        {"id": "visual-a", "score": 0.9},
                        {"id": "visual-b", "score": 0.8},
                    ],
                }

        signer = portal.SearchCursorSigner(b"portal-cursor-test-secret-at-least-32-bytes")
        runtime = (Config(), Client(), Manifest(), signer)
        with patch.object(portal, "_media_search_runtime", return_value=runtime):
            first = self.search(
                "semantic scene", scope="visible", limit=1
            ).get_json()
            self.assertTrue(first["has_more"])
            cursor = first["next_cursor"]
            continued = self.search(
                "semantic scene", scope="visible", limit=1, cursor=cursor
            )
            self.assertEqual(continued.status_code, 200)
            self.assertEqual(continued.get_json()["photos"][0]["id"], "visual-b")
            diana = self.make_client("diana@example.test", "Diana")
            self.assertEqual(
                self.search(
                    "semantic scene", client=diana, scope="visible",
                    limit=1, cursor=cursor,
                ).status_code,
                409,
            )
            self.assertEqual(
                self.search(
                    "different query", scope="visible", limit=1, cursor=cursor,
                ).status_code,
                409,
            )
            self.assertEqual(
                self.search(
                    "semantic scene", scope="mine", limit=1, cursor=cursor,
                ).status_code,
                409,
            )

    def removed_search_status_is_actor_scoped_and_never_reports_global_count(self):
        self.add_photo("shared-status")
        self.add_photo("david-private-one", visibility="private")
        self.add_photo("david-private-two", visibility="private")
        self.add_photo(
            "diana-private",
            owner_id="diana@example.test",
            owner_name="Diana",
            visibility="private",
        )
        diana = self.make_client("diana@example.test", "Diana")
        with patch.dict(os.environ, {"DAVID_PI_MEDIA_SEARCH_ENABLED": "0"}):
            david_status = self.client.get(
                "/api/photos/search-status?scope=visible"
            )
            diana_status = diana.get(
                "/api/photos/search-status?scope=visible"
            )
        self.assertEqual(david_status.status_code, 200)
        self.assertEqual(diana_status.status_code, 200)
        self.assertEqual(david_status.get_json()["total_items"], 3)
        self.assertEqual(diana_status.get_json()["total_items"], 2)
        for response in (david_status, diana_status):
            payload = response.get_json()
            self.assertNotIn("total_indexed", payload)
            self.assertNotIn("principal", payload)
            self.assertEqual(payload["state"], "disabled")
            self.assertEqual(payload["indexed_items"], 0)
            self.assertIn("no-store", response.headers["Cache-Control"])

    def removed_search_status_maps_backend_progress_to_stable_actor_scoped_shape(self):
        self.add_photo("shared-progress")
        self.add_photo("private-progress", visibility="private")

        class Config:
            enabled = True

        class Client:
            state = "degraded"
            reason_code = "index_partial"

            def status(self):
                return {
                    "schema_version": 1,
                    "state": self.state,
                    "available": True,
                    "model_id": "visual:test",
                    "index_version": "index-v1:test",
                    "capabilities": {"visual": True, "ocr": True},
                    "reason_code": self.reason_code,
                }

        client = Client()
        runtime = (Config(), client, object(), object())
        with patch.object(portal, "_media_search_runtime", return_value=runtime):
            indexing = self.client.get("/api/photos/search-status?scope=visible")
            client.state = "ready"
            client.reason_code = "ready"
            ready = self.client.get("/api/photos/search-status?scope=visible")
        self.assertEqual(indexing.status_code, 200)
        self.assertEqual(
            set(indexing.get_json()),
            {
                "mode", "state", "available", "reason_code", "model_id",
                "index_version", "indexed_items", "total_items",
            },
        )
        self.assertEqual(indexing.get_json()["state"], "indexing")
        self.assertEqual(indexing.get_json()["indexed_items"], 0)
        self.assertEqual(indexing.get_json()["total_items"], 2)
        self.assertEqual(ready.get_json()["state"], "ready")
        self.assertEqual(ready.get_json()["indexed_items"], 2)
        self.assertEqual(ready.get_json()["total_items"], 2)
        self.assertEqual(self.client.get("/api/photos/search/status").status_code, 404)

    def test_photo_json_and_hardened_detail_full_routes(self):
        self.add_photo("full-image", original_name="full-image.jpg")
        original = portal.ORIGINALS / "full-image.original"
        preview = portal.PREVIEWS / "full-image.preview"
        outside = Path(self._database_directory.name) / "outside.jpg"
        try:
            portal.Image.new("RGB", (120, 80), "navy").save(original, "JPEG")
            portal.Image.new("RGB", (2200, 1400), "navy").save(preview, "JPEG")
            with portal.db() as connection:
                connection.execute(
                    "UPDATE photos SET byte_size=?,content_type='image/jpeg' WHERE id=?",
                    (original.stat().st_size, "full-image"),
                )
            listing = self.client.get("/api/photos?scope=visible").get_json()["photos"][0]
            self.assertEqual(listing["detail"], "/media/detail/full-image")
            self.assertEqual(listing["full"], "/media/full/full-image")
            detail = self.client.get(listing["detail"])
            self.assertEqual(detail.status_code, 200)
            with portal.Image.open(io.BytesIO(detail.data)) as image:
                self.assertLessEqual(max(image.size), 2200)
            full = self.client.get(listing["full"])
            self.assertEqual(full.status_code, 200)
            self.assertEqual(full.mimetype, "image/jpeg")
            self.assertNotIn("attachment", full.headers.get("Content-Disposition", ""))
            self.assertIn("no-store", full.headers["Cache-Control"])
            self.assertEqual(full.headers["X-Content-Type-Options"], "nosniff")

            original.write_bytes(b"not really a jpeg")
            with portal.db() as connection:
                connection.execute(
                    "UPDATE photos SET byte_size=? WHERE id=?",
                    (original.stat().st_size, "full-image"),
                )
            self.assertEqual(self.client.get(listing["full"]).status_code, 404)

            outside.write_bytes(b"private outside bytes")
            original.unlink()
            original.symlink_to(outside)
            with portal.db() as connection:
                connection.execute(
                    "UPDATE photos SET byte_size=? WHERE id=?",
                    (outside.stat().st_size, "full-image"),
                )
            leaked = self.client.get(listing["full"])
            self.assertEqual(leaked.status_code, 404)
            self.assertNotIn(outside.read_bytes(), leaked.data)
        finally:
            if original.is_symlink() or original.exists():
                original.unlink()
            preview.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_full_route_rejects_video_and_unbounded_or_unsupported_images(self):
        self.add_photo("full-video", content_type="video/mp4")
        self.add_photo("full-gif", content_type="image/gif")
        self.add_photo("full-large", content_type="image/jpeg")
        self.add_photo("detail-large", content_type="image/jpeg")
        self.add_photo("detail-png", content_type="image/jpeg")
        with portal.db() as connection:
            connection.execute(
                "UPDATE photos SET byte_size=? WHERE id='full-large'",
                (portal.MAX_INLINE_FULL_IMAGE_BYTES + 1,),
            )
        for photo_id in ("full-video", "full-gif", "full-large"):
            self.assertEqual(
                self.client.get(f"/media/full/{photo_id}").status_code, 404
            )
        self.assertEqual(self.client.get("/media/detail/full-video").status_code, 404)
        detail_large = portal.PREVIEWS / "detail-large.preview"
        detail_png = portal.PREVIEWS / "detail-png.preview"
        try:
            portal.Image.new("RGB", (2201, 1), "navy").save(detail_large, "JPEG")
            portal.Image.new("RGB", (10, 10), "navy").save(detail_png, "PNG")
            self.assertEqual(
                self.client.get("/media/detail/detail-large").status_code, 404
            )
            self.assertEqual(
                self.client.get("/media/detail/detail-png").status_code, 404
            )
        finally:
            detail_large.unlink(missing_ok=True)
            detail_png.unlink(missing_ok=True)

    def test_complete_collections_page_is_a_verified_portal_route(self):
        response = self.client.get("/photos/collections")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose a collection", response.get_data(as_text=True))

    def removed_search_composes_scope_kind_favorite_period_collection_and_unicode(self):
        self.add_photo(
            "david-private",
            visibility="private",
            original_name="garden-private.jpg",
            captured_at="2024-12-31T23:59:59+00:00",
        )
        self.add_photo(
            "david-shared",
            original_name="garden-shared.jpg",
            captured_at="2025-01-01T00:00:00+00:00",
        )
        self.add_photo(
            "diana-video",
            owner_id="diana@example.test",
            owner_name="Diana",
            original_name="garden-party.mp4",
            content_type="video/mp4",
            captured_at="2024-06-01T00:00:00+00:00",
        )
        self.caption("david-private", "Cafe\u0301 garden")
        self.favorite("david-private", True)
        self.add_collection("summer")
        self.add_membership("summer", "david-private")

        visible = self.search("garden", scope="visible").get_json()["photos"]
        self.assertEqual(
            {item["id"] for item in visible},
            {"david-private", "david-shared", "diana-video"},
        )
        shared = self.search("garden", scope="shared").get_json()["photos"]
        self.assertEqual({item["id"] for item in shared}, {"david-shared", "diana-video"})
        mine = self.search("garden", scope="mine").get_json()["photos"]
        self.assertEqual({item["id"] for item in mine}, {"david-private", "david-shared"})
        video = self.search("garden", scope="visible", kind="video").get_json()["photos"]
        self.assertEqual([item["id"] for item in video], ["diana-video"])
        favorites = self.search(
            "garden", scope="visible", favorite=True
        ).get_json()["photos"]
        self.assertEqual([item["id"] for item in favorites], ["david-private"])
        period = self.search(
            "garden", scope="visible", period="2024"
        ).get_json()["photos"]
        self.assertEqual({item["id"] for item in period}, {"david-private", "diana-video"})
        collection = self.search(
            "garden", scope="visible", collection="summer"
        ).get_json()["photos"]
        self.assertEqual([item["id"] for item in collection], ["david-private"])
        unicode_result = self.search("Cafe\u0301", scope="visible").get_json()["photos"]
        self.assertEqual([item["id"] for item in unicode_result], ["david-private"])

    def removed_search_cursor_is_deterministic_and_bound_to_actor_query_and_filters(self):
        for index in range(5):
            self.add_photo(
                f"cursor-{index}", original_name=f"family-cursor-{index}.jpg"
            )
        first = self.search("family-cursor", scope="visible", limit=2).get_json()
        self.assertEqual(first["total"], 5)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["photos"]), 2)
        cursor = first["next_cursor"]
        second = self.search(
            "family-cursor", scope="visible", limit=2, cursor=cursor
        ).get_json()
        self.assertIsNone(second["total"])
        self.assertEqual(len(second["photos"]), 2)
        self.assertEqual(
            self.search(
                "different", scope="visible", limit=2, cursor=cursor
            ).status_code,
            400,
        )
        self.assertEqual(
            self.search(
                "family-cursor", scope="mine", limit=2, cursor=cursor
            ).status_code,
            400,
        )
        diana = self.make_client("diana@example.test", "Diana", csrf=False)
        self.assertEqual(
            self.search(
                "family-cursor", client=diana, scope="visible", limit=2, cursor=cursor
            ).status_code,
            400,
        )

    def removed_read_only_search_is_exactly_csrf_exempt_but_mutations_are_not(self):
        self.add_photo("csrf-item")
        unprotected = self.make_client("david@example.test", "David", csrf=False)
        search = self.search("csrf", client=unprotected, scope="visible")
        self.assertEqual(search.status_code, 200)
        favorite = self.favorite("csrf-item", True, client=unprotected)
        caption = self.caption("csrf-item", "blocked", client=unprotected)
        self.assertEqual(favorite.status_code, 403)
        self.assertEqual(caption.status_code, 403)
        unknown = self.make_client("unknown@example.test", "Unknown", csrf=False)
        self.assertEqual(
            self.search("csrf", client=unknown, scope="visible").status_code,
            403,
        )
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM media_personal_state WHERE photo_id='csrf-item'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM media_captions WHERE photo_id='csrf-item'"
            ).fetchone()[0], 0)

    def test_legacy_get_contract_and_new_filters_remain_compatible(self):
        self.add_photo("legacy-shared", owner_id=None, owner_name=None)
        self.add_photo("david-private", visibility="private")
        self.add_photo(
            "diana-shared", owner_id="diana@example.test", owner_name="Diana"
        )
        self.add_collection("david-private-collection")
        self.add_collection("david-shared-collection")
        self.add_collection("diana-shared-collection", owner_id="diana@example.test")
        with portal.db() as connection:
            connection.execute(
                "UPDATE collections SET visibility='private' WHERE id='david-private-collection'"
            )
        historic = self.client.get("/api/photos").get_json()
        self.assertEqual(historic["scope"], "shared")
        self.assertEqual(
            {item["id"] for item in historic["photos"]},
            {"legacy-shared", "diana-shared"},
        )
        mine = self.client.get("/api/photos?view=mine").get_json()
        self.assertEqual(mine["scope"], "mine")
        self.assertEqual([item["id"] for item in mine["photos"]], ["david-private"])
        visible = self.client.get("/api/photos?scope=visible").get_json()
        self.assertEqual(
            {item["id"] for item in visible["photos"]},
            {"legacy-shared", "david-private", "diana-shared"},
        )
        self.assertEqual(self.client.get("/api/photos?scope=everyone").status_code, 400)
        shared_collections = self.client.get("/api/collections").get_json()["collections"]
        self.assertEqual(
            {item["id"] for item in shared_collections},
            {"david-shared-collection", "diana-shared-collection"},
        )
        mine_collections = self.client.get("/api/collections?view=mine").get_json()["collections"]
        self.assertEqual(
            {item["id"] for item in mine_collections},
            {"david-private-collection", "david-shared-collection"},
        )

    def test_audit_and_outbox_are_content_neutral(self):
        self.add_photo("neutral-item")
        secret = "Do not write this caption into audit records"
        self.assertEqual(self.caption("neutral-item", secret).status_code, 200)
        self.assertEqual(self.favorite("neutral-item", True).status_code, 200)
        with portal.db() as connection:
            audit_rows = [tuple(row) for row in connection.execute(
                """SELECT actor_id,domain,object_id,action,before_digest,after_digest
                   FROM mutation_audit
                   WHERE object_id='neutral-item' OR object_id LIKE 'neutral-item:%'"""
            )]
            outbox_rows = [tuple(row) for row in connection.execute(
                """SELECT domain,object_id,event_type,payload_digest
                   FROM domain_outbox
                   WHERE object_id='neutral-item' OR object_id LIKE 'neutral-item:%'"""
            )]
        serialized = repr((audit_rows, outbox_rows))
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Do not write", serialized)
        self.assertEqual(len(audit_rows), 2)
        self.assertEqual(len(outbox_rows), 2)


if __name__ == "__main__":
    unittest.main()
