import importlib
import hashlib
import ctypes
import errno
import os
import sqlite3
import sys
import tempfile
import threading
import time
import json
import unittest
import zipfile
import base64
import shutil
import signal
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from werkzeug.security import generate_password_hash


TEST_DATA = tempfile.TemporaryDirectory()
os.environ["PHOTO_DATA"] = TEST_DATA.name
os.environ["DAVID_PI_DISABLE_METRICS"] = "1"
os.environ["DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING"] = "1"
os.environ["PIHOLE_SUMMARY"] = str(Path(TEST_DATA.name) / "pihole-summary.json")
os.environ["DAVID_PI_PLATFORM_DATA"] = str(Path(TEST_DATA.name) / "platform")
os.environ["DAVID_PI_FILES_DATA"] = str(Path(TEST_DATA.name) / "files")
os.environ["DAVID_PI_CHAT_DATA"] = str(Path(TEST_DATA.name) / "chat")
os.environ["DAVID_PI_CHAT_KEY_B64"] = base64.b64encode(b"david-pi-chat-test-key-32-bytes!").decode()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
portal = importlib.import_module("app")
identity_module = importlib.import_module("modules.identity")
notes_module = importlib.import_module("modules.notes")
movies_module = importlib.import_module("modules.movies")
recipes_module = importlib.import_module("modules.recipes")
files_module = importlib.import_module("modules.files")
platform_module = importlib.import_module("modules.platform")
secure_storage_module = importlib.import_module("modules.secure_storage")
places_module = importlib.import_module("modules.places")
audiobooks_module = importlib.import_module("modules.audiobooks")
audiobook_streaming_module = importlib.import_module("modules.audiobook_streaming")
games_module = importlib.import_module("modules.games")
device_backup_module = importlib.import_module("modules.device_backup")
chat_module = importlib.import_module("modules.chat")
assistant_module = importlib.import_module("modules.assistant")
portal.app.config["TESTING"] = True
DEVICE_SENTINEL = Path(TEST_DATA.name) / ".david-pi-storage"
DEVICE_SENTINEL.write_text("david-pi-family-storage-v1", encoding="utf-8")
device_backup_module.PRIMARY_SENTINEL = DEVICE_SENTINEL


def tearDownModule():
    TEST_DATA.cleanup()


SNAPSHOT = {
    "timestamp": 1,
    "cpu": 12.0,
    "memory": 24.0,
    "memory_used_gb": 0.9,
    "memory_total_gb": 3.7,
    "temperature": 55.0,
    "disk_used": 20.0,
    "disk_used_gb": 10.0,
    "disk_total_gb": 50.0,
    "disk_free_gb": 40.0,
    "load1": 0.1,
    "load5": 0.1,
    "load15": 0.1,
    "uptime": 90061,
}


class PortalTestCase(unittest.TestCase):
    def setUp(self):
        # These tiny audiobook fixtures model a volume with room for playback
        # preparation, independently of the CI runner's real disk capacity.
        # Keep the real capacity policy and let low-storage tests override this
        # measurement; unrelated application paths still use their real disk.
        real_disk_usage = shutil.disk_usage
        audiobook_roots = {
            os.fspath(audiobooks_module.ROOT),
            os.fspath(audiobook_streaming_module.ROOT),
        }

        def fixture_disk_usage(path):
            if os.fspath(path) in audiobook_roots:
                return SimpleNamespace(
                    total=500 * 1024**3, used=100 * 1024**3, free=400 * 1024**3
                )
            return real_disk_usage(path)

        capacity = patch.object(shutil, "disk_usage", side_effect=fixture_disk_usage)
        capacity.start()
        self.addCleanup(capacity.stop)
        names = patch.object(assistant_module, "household_names", return_value={"david@example.test":"David", "diana@example.test":"Diana"})
        names.start()
        self.addCleanup(names.stop)
        self.client = portal.app.test_client()
        self.client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "david@example.test"
        self.client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "David"
        self.csrf = "test-csrf-token-with-more-than-32-characters"
        self.client.set_cookie("david_pi_csrf", self.csrf, domain="localhost")
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf
        with portal.db() as connection:
            connection.execute("DELETE FROM device_backup_events")
            connection.execute("DELETE FROM device_sync_runs")
            connection.execute("DELETE FROM device_media_records")
            connection.execute("DELETE FROM device_uploads")
            connection.execute("DELETE FROM backup_devices")
            connection.execute("DELETE FROM device_pairing_tokens")
            connection.execute("DELETE FROM slideshow_jobs")
            connection.execute("DELETE FROM media_publish_intents")
            connection.execute("DELETE FROM collection_photos")
            connection.execute("DELETE FROM collections")
            connection.execute("DELETE FROM photos")
        with notes_module.connect(notes_module.DB_PATH) as connection:
            connection.execute("DELETE FROM notes")
        with movies_module.connect(movies_module.DB_PATH) as connection:
            connection.execute("DELETE FROM availability")
            connection.execute("DELETE FROM movies")
            connection.execute("DELETE FROM movie_subscriptions")
            connection.execute("DELETE FROM movie_subscription_profiles")
            connection.execute("UPDATE subscriptions SET enabled = 0")
            connection.execute("DELETE FROM movie_settings")
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            connection.execute("DELETE FROM recipe_recommendations")
            connection.execute("DELETE FROM recipes")
        with recipes_module.connect(recipes_module.IMAGE_CACHE_PATH) as connection:
            connection.execute("DELETE FROM recipe_image_derivatives")
        with files_module.connect(files_module.DB_PATH) as connection:
            connection.execute("DELETE FROM file_upload_intents")
            connection.execute("DELETE FROM file_upload_batches")
            connection.execute("DELETE FROM file_pdf_jobs")
            connection.execute("DELETE FROM file_job_state")
            connection.execute("DELETE FROM stored_files")
            connection.execute("DELETE FROM file_folders")
        with places_module.connect(places_module.DB_PATH) as connection:
            connection.execute("DELETE FROM restaurants")
            connection.execute("DELETE FROM margarita_records")
            connection.execute("DELETE FROM margarita_image_history")
            connection.execute(
                "DELETE FROM cuisine_categories WHERE normalized_name NOT IN (%s)"
                % ",".join("?" for _ in places_module.CUISINES),
                tuple(name.casefold() for name in places_module.CUISINES),
            )
            connection.execute(
                """UPDATE margaritas SET name='', rating=NULL, review='',
                   image_name=NULL, updated_by_id=NULL, updated_by_name=NULL,
                   updated_at=NULL, version=1"""
            )
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            connection.execute("DELETE FROM audiobook_progress")
            connection.execute("DELETE FROM audiobooks")
        with audiobook_streaming_module.queue_connection() as connection:
            connection.execute("DELETE FROM audiobook_playback_jobs")
            connection.execute("DELETE FROM audiobook_catalog_snapshot")
            connection.execute("DELETE FROM audiobook_import_reservations")
            connection.execute("DELETE FROM audiobook_queue_metadata")
        audiobook_streaming_module.reconcile_catalog([])
        with games_module.connect(games_module.DB_PATH) as connection:
            connection.execute("DELETE FROM game_scores")
            connection.execute("DELETE FROM chess_ratings")
        with chat_module.connect(chat_module.DB_PATH) as connection:
            connection.execute("DELETE FROM notification_jobs")
            connection.execute("DELETE FROM push_subscriptions")
            connection.execute("DELETE FROM conversation_reads")
            connection.execute("DELETE FROM chat_attachments")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM conversation_members")
            connection.execute("DELETE FROM conversations")
            connection.execute("DELETE FROM portal_users")
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM conversations")
            connection.execute("DELETE FROM approvals")
            connection.execute("DELETE FROM remote_tasks")
        for path in files_module.OBJECTS.glob("*"):
            path.unlink()
        for path in files_module.INCOMING.glob("*.part"):
            path.unlink()
        for path in files_module.PDF_CACHE.glob("*.jpg"):
            path.unlink()
        files_module.pdf_page_counts.clear()
        for path in places_module.IMAGE_ROOT.glob("*"):
            path.unlink()
        for path in places_module.RESTAURANT_IMAGE_ROOT.glob("*"):
            path.unlink()
        for directory in (audiobooks_module.ORIGINALS, audiobooks_module.COVERS, audiobooks_module.INCOMING, audiobooks_module.TRASH):
            for path in directory.glob("*"):
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        for path in audiobook_streaming_module.STREAMING.glob("*"):
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    def paired_client(self, profile="home", name=None):
        client = portal.app.test_client()
        person = "diana" if profile == "diana" else "david"
        client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = f"{person}@example.test"
        client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = name or person.title()
        token = "paired-test-csrf-token-with-more-than-32-characters"
        client.set_cookie("david_pi_csrf", token, domain="localhost")
        return client, token

    def allowlisted_client(self, login, name):
        client = portal.app.test_client()
        client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = login
        client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = name
        token = "allowlisted-test-csrf-token-more-than-32-characters"
        client.set_cookie("david_pi_csrf", token, domain="localhost")
        return client, token

    def add_photo(self, photo_id, deleted_at=None, owner_id=None, owner_name=None, visibility="shared"):
        with portal.db() as connection:
            connection.execute(
                "INSERT INTO photos (id, original_name, stored_path, preview_name, thumb_name, content_type, "
                "byte_size, sha256, content_sha256, taken_at, capture_timestamp, uploaded_at, uploaded_by, deleted_at, owner_id, owner_name, visibility) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo_id, f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.jpg", "image/jpeg", 10,
                 f"hash-{photo_id}", hashlib.sha256(photo_id.encode()).hexdigest(),
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00+00:00", "Test", deleted_at,
                 owner_id, owner_name, visibility),
            )

    def add_collection(self, collection_id, name, owner_id=None, owner_name=None, visibility="shared"):
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO collections
                   (id, name, created_at, created_by, owner_id, owner_name, visibility)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (collection_id, name, "2026-01-01T00:00:00+00:00", "Test",
                 owner_id, owner_name, visibility),
            )

    def pair_backup_device(
        self, client=None, owner="david@example.test", name="David", csrf=None,
        endpoint="/api/v1/device-backup/pair",
    ):
        client = client or self.client
        client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = owner
        client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = name
        token_response = client.post(
            "/api/device-backup/pairing-token",
            headers={"X-CSRF-Token": csrf or self.csrf},
        )
        self.assertEqual(token_response.status_code, 200)
        pairing_token = token_response.get_json()["pairing_token"]
        paired = client.post(
            endpoint,
            json={"pairing_token": pairing_token, "device_name": f"{name}'s phone"},
        )
        self.assertEqual(paired.status_code, 201)
        return paired.get_json()

    def add_device_media_record(
        self, device_id, client_item_id, photo_id, owner_id="david@example.test"
    ):
        owner_name = "Diana" if owner_id == "diana@example.test" else "David"
        self.add_photo(
            photo_id, owner_id=owner_id, owner_name=owner_name, visibility="private"
        )
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO device_media_records
                   (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                    content_sha256,byte_size,capture_timestamp,ingestion_source,
                    primary_verification_state,secondary_verification_state,
                    local_source_visible,ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"record-{photo_id}", device_id, client_item_id, photo_id,
                    owner_id, f"{photo_id}.jpg", f"hash-{photo_id}", 10,
                    "2026-01-01T00:00:00+00:00", "android_backup",
                    "primary_verified", "secondary_pending", 1,
                    "2026-01-01T00:00:00+00:00",
                ),
            )

    def reconciliation_payload(self, scan_id, visible_ids, complete=True):
        canonical = device_backup_module.canonical_reconciliation_ids(visible_ids)
        return {
            "scan_id": scan_id,
            "complete": complete,
            "item_count": len(canonical),
            "ids_sha256": device_backup_module.reconciliation_ids_sha256(canonical),
            "visible_client_item_ids": visible_ids,
        }

    def test_reconciliation_digest_matches_the_android_ascii_json_contract(self):
        self.assertEqual(
            device_backup_module.reconciliation_ids_sha256(["image:1", "video:2"]),
            "4b9c23e5cf882e47c04dacccd817750a8a7b33d4e1cbb173f7fe13e1e9e48ccd",
        )

    def memberships(self):
        with portal.db() as connection:
            return {tuple(row) for row in connection.execute(
                "SELECT collection_id, photo_id FROM collection_photos ORDER BY collection_id, photo_id"
            ).fetchall()}

    def photo_items(self, *photo_ids):
        with portal.db() as connection:
            rows = connection.execute(
                f"SELECT id,version FROM photos WHERE id IN ({','.join('?' for _ in photo_ids)})",
                photo_ids,
            ).fetchall()
        versions = {row["id"]: int(row["version"]) for row in rows}
        return [{"id": photo_id, "version": versions[photo_id]} for photo_id in photo_ids]

    def collection_version(self, collection_id):
        with portal.db() as connection:
            return int(connection.execute(
                "SELECT version FROM collections WHERE id=?", (collection_id,)
            ).fetchone()["version"])

    def add_membership(self, collection_id, photo_id, added_by_id=None):
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO collection_photos
                   (collection_id,photo_id,added_at,added_by_id,version)
                   VALUES (?,?,?,?,1)""",
                (collection_id, photo_id, "2026-01-01", added_by_id),
            )

    def staged_video_derivatives(self, photo_id, *, playback=b"playback"):
        artifacts = []
        for kind, final_name, content in (
            ("preview", f"{photo_id}.jpg", b"poster"),
            ("thumb", f"{photo_id}.jpg", b"thumb"),
            ("playback", f"{photo_id}.mp4", playback),
        ):
            stage_name = portal.safe_component(
                f".test-media-{photo_id}-{kind}-{portal.uuid.uuid4().hex}.tmp"
            )
            descriptor, _metadata = portal.INCOMING_STORAGE.create_regular(
                stage_name
            )
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            artifacts.append(
                portal._staged_artifact_record(
                    kind, final_name, f"incoming/{stage_name}"
                )
            )
        return (
            f"{photo_id}.jpg",
            f"{photo_id}.jpg",
            f"{photo_id}.mp4",
            artifacts,
        )

    def test_pages_render(self):
        for path in ("/", "/photos", "/assistant", "/chat", "/games", "/status", "/files", "/device-backup", "/places", "/audiobooks"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.assertIn(b"David-Pi", self.client.get("/assistant").data)
        icon = self.client.get("/david-pi-icon-192.png")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.mimetype, "image/png")
        self.assertEqual(portal.Image.open(BytesIO(icon.data)).size, (192, 192))
        files_page = self.client.get("/files").data
        self.assertIn(b"files.js?v=16", files_page)
        self.assertIn(b"file-drop-plus", files_page)
        self.assertNotIn(b"<iframe", files_page)
        self.assertIn(b"pinchStartZoom", self.client.get("/static/files.js").data)
        audiobooks_page = self.client.get("/audiobooks").data
        self.assertIn(b"audiobook-progress.js?v=4", audiobooks_page)
        self.assertIn(b"audiobook-continuity.js?v=2", audiobooks_page)
        self.assertIn(b"audiobook-offline-web.js?v=10", audiobooks_page)
        self.assertIn(b"audiobook-ui-safety.js?v=3", audiobooks_page)
        self.assertIn(b"audiobooks.js?v=31", audiobooks_page)
        self.assertNotIn(b'<script src="/static/vendor/hls/hls.min.js', audiobooks_page)
        self.assertIn(b"Interrupted saves can resume", audiobooks_page)
        self.assertNotIn(b"Background playback", audiobooks_page)
        created = self.client.post(
            "/api/collections", json={"name": "Trips", "visibility": "shared"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(created.status_code, 201)
        photos_page = self.client.get("/photos").data
        self.assertIn(b'organizerCollectionCatalog', photos_page)
        self.assertIn(b'>Trips</span>', photos_page)
        self.assertIn(b'startOrganizerWatchdog', self.client.get("/static/gallery.js").data)
        gallery_script = self.client.get("/static/gallery.js").get_data(as_text=True)
        self.assertIn("serverOrganizerCatalog", gallery_script)
        self.assertNotIn("liveChecks.replaceChildren()", gallery_script)
        self.assertNotIn("existingRows.forEach((label, id) => { if (id) label.remove(); })", gallery_script)
        files_script = self.client.get("/static/files.js").get_data(as_text=True)
        self.assertIn("pdf-page-surface", files_script)
        self.assertIn("fitRenderWidth=1200,zoomRenderWidth=2000", files_script)

    def test_audiobook_upload_progress_range_and_privacy(self):
        with patch.object(audiobooks_module,"probe",return_value=({"title":"A Safe Book","artist":"An Author"},3600.0,[{"title":"Chapter 1","start":0,"end":120}])), patch.object(audiobooks_module,"cover",return_value=None):
            uploaded=self.client.post("/api/audiobooks/upload",data={"visibility":"private","books":(BytesIO(b"ID3"+b"a"*2048),"safe.mp3")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(uploaded.status_code,201,uploaded.get_data(as_text=True))
        mine_payload=self.client.get("/api/audiobooks?owner=mine").get_json();mine=mine_payload["books"]
        self.assertEqual(mine[0]["title"],"A Safe Book"); self.assertEqual(mine[0]["chapters"][0]["title"],"Chapter 1")
        self.assertEqual(mine[0]["sha256"],hashlib.sha256(b"ID3"+b"a"*2048).hexdigest())
        book_id=mine[0]["id"]
        self.assertEqual(self.client.put(f"/api/audiobooks/{book_id}/progress",json={"position_seconds":125.5,"progress_scope":mine_payload["progress_scope"],"session_id":"a"*32,"sequence":1,"base_revision":0},headers={"X-CSRF-Token":self.csrf}).status_code,200)
        updated=self.client.get("/api/audiobooks?owner=mine").get_json()["books"][0]
        self.assertEqual(updated["position_seconds"],125.5)
        self.assertIsNotNone(updated["progress_updated_at"])
        stream_url=f"/api/audiobooks/{book_id}/stream"
        metadata=self.client.head(stream_url)
        self.assertEqual(metadata.status_code,200)
        self.assertEqual(metadata.headers["Accept-Ranges"],"bytes")
        self.assertEqual(int(metadata.headers["Content-Length"]),2051)
        self.assertTrue(metadata.headers.get("ETag"))
        ranged=self.client.get(stream_url,headers={"Range":"bytes=0-2"}); self.assertEqual(ranged.status_code,206); self.assertEqual(ranged.data,b"ID3")
        self.assertEqual(ranged.headers["Content-Range"],"bytes 0-2/2051")
        self.assertEqual(self.client.get(stream_url,headers={"Range":"bytes=-3"}).data,b"aaa")
        self.assertEqual(self.client.get(stream_url,headers={"Range":"bytes=5000-"}).status_code,416)
        resumed=self.client.get(stream_url,headers={"Range":"bytes=3-5","If-Range":metadata.headers["ETag"]})
        self.assertEqual((resumed.status_code,resumed.data),(206,b"aaa"))
        restarted=self.client.get(stream_url,headers={"Range":"bytes=3-5","If-Range":'"stale"'})
        self.assertEqual(restarted.status_code,200)
        self.assertEqual(len(restarted.data),2051)
        download=self.client.head(f"/api/audiobooks/{book_id}/download")
        self.assertEqual(download.headers["Accept-Ranges"],"bytes")
        self.assertIn("attachment",download.headers["Content-Disposition"])
        diana,_=self.paired_client("diana","Diana"); self.assertEqual(diana.get("/api/audiobooks").get_json()["books"],[])
        self.assertEqual(self.client.delete(f"/api/audiobooks/{book_id}",headers={"X-CSRF-Token":self.csrf}).status_code,200)
        with audiobook_streaming_module.queue_connection() as connection:
            inactive=connection.execute("SELECT state,error_code FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()
        self.assertEqual(tuple(inactive),("failed","inactive"))
        self.assertEqual(self.client.post(f"/api/audiobooks/{book_id}/restore",headers={"X-CSRF-Token":self.csrf}).status_code,200)
        self.assertEqual(audiobook_streaming_module.playback_status(book_id)["state"],"pending")

    def test_audiobook_rejects_protected_aax(self):
        response=self.client.post("/api/audiobooks/upload",data={"visibility":"shared","books":(BytesIO(b"protected"),"book.aax")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(response.status_code,422); self.assertIn("DRM-free",response.get_data(as_text=True))

    def test_audiobook_upload_rejects_unresolved_duration_without_library_or_queue_writes(self):
        for duration in (None, 0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(duration=duration), patch.object(
                audiobooks_module, "probe", return_value=({"title": "Invalid timing"}, duration, [])
            ), patch.object(audiobooks_module, "cover", return_value=None):
                response = self.client.post(
                    "/api/audiobooks/upload",
                    data={
                        "visibility": "shared",
                        "books": (BytesIO(b"ID3" + b"z" * 2048), "invalid.mp3"),
                    },
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": self.csrf},
                )
            self.assertEqual(response.status_code, 422)
            self.assertIn("duration is invalid", response.get_data(as_text=True))
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0], 0)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot").fetchone()[0], 0)
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()), [])
        self.assertEqual(list(audiobooks_module.ORIGINALS.iterdir()), [])

    def test_audiobook_upload_forecast_overflow_is_atomic(self):
        with patch.object(
            audiobooks_module,"probe",return_value=({"title":"Impossible timing"},1e308,[])
        ), patch.object(audiobooks_module,"cover",return_value=None):
            response=self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"shared","books":(BytesIO(b"ID3"+b"o"*2048),"overflow.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        self.assertEqual(response.status_code,422)
        self.assertIn("timing metadata",response.get_data(as_text=True))
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0],0)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()),[])
        self.assertEqual(list(audiobooks_module.ORIGINALS.iterdir()),[])

    def test_audiobook_upload_low_storage_refuses_before_publishing(self):
        for free_bytes in (audiobooks_module.RESERVE - 1, audiobook_streaming_module.MIN_FREE_BYTES):
            with self.subTest(free_bytes=free_bytes), patch.object(
                audiobooks_module.shutil, "disk_usage",
                return_value=SimpleNamespace(free=free_bytes),
            ), patch.object(
                audiobooks_module, "probe", return_value=({"title": "No room"}, 60.0, [])
            ), patch.object(audiobooks_module, "cover", return_value=None):
                response = self.client.post(
                    "/api/audiobooks/upload",
                    data={"visibility": "private", "books": (BytesIO(b"ID3" + b"l" * 2048), "low.mp3")},
                    content_type="multipart/form-data", headers={"X-CSRF-Token": self.csrf},
                )
            self.assertEqual(response.status_code, 422)
            self.assertIn("more free space", response.get_data(as_text=True))
            with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0], 0)
            with audiobook_streaming_module.queue_connection() as connection:
                for table in ("audiobook_playback_jobs", "audiobook_catalog_snapshot", "audiobook_import_reservations"):
                    self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(list(audiobooks_module.INCOMING.iterdir()), [])
            self.assertEqual(list(audiobooks_module.ORIGINALS.iterdir()), [])

    def test_audiobook_queue_commit_failure_rolls_back_uncommitted_import(self):
        with patch.object(
            audiobooks_module,"probe",return_value=({"title":"Atomic import"},60.0,[])
        ), patch.object(audiobooks_module,"cover",return_value=None), patch.object(
            audiobooks_module,"commit_reserved_import",side_effect=sqlite3.OperationalError("injected queue failure")
        ):
            response=self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"q"*2048),"commit.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        self.assertEqual(response.status_code,422,response.get_data(as_text=True))
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0],0)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()),[])
        self.assertEqual(list(audiobooks_module.ORIGINALS.iterdir()),[])

    def test_audiobook_activation_failure_preserves_concurrent_edits_progress_and_journal(self):
        edited_at="2026-08-30T12:34:56+00:00"
        def edit_then_fail(_token,book_id):
            with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE audiobooks SET title=?,visibility=?,updated_at=? WHERE id=?",
                    ("Reader edit","shared",edited_at,book_id),
                )
                connection.execute(
                    """INSERT INTO audiobook_progress(
                       book_id,owner_id,position_seconds,completed,updated_at
                       ) VALUES(?,?,?,?,?)""",
                    (book_id,"david@example.test",37.5,0,edited_at),
                )
            raise sqlite3.OperationalError("injected activation failure")
        with patch.object(
            audiobooks_module,"probe",return_value=({"title":"Original title"},60.0,[])
        ), patch.object(audiobooks_module,"cover",return_value=None), patch.object(
            audiobooks_module,"activate_reserved_import",side_effect=edit_then_fail
        ):
            response=self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"a"*2048),"activate.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            row=connection.execute("SELECT * FROM audiobooks").fetchone()
            progress=connection.execute("SELECT * FROM audiobook_progress").fetchone()
        self.assertEqual((row["title"],row["visibility"],row["updated_at"]),("Reader edit","shared",edited_at))
        self.assertEqual(progress["position_seconds"],37.5)
        original=audiobooks_module.ORIGINALS/row["stored_name"]
        self.assertTrue(original.is_file())
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT state FROM audiobook_import_reservations").fetchone()[0],"queued")
            self.assertEqual(tuple(connection.execute("SELECT state,error_code FROM audiobook_playback_jobs").fetchone()),("failed","import_pending"))
        recovered=audiobooks_module.reconcile_incomplete_imports(0)
        self.assertEqual(recovered["completed"],1)
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            row=connection.execute("SELECT * FROM audiobooks").fetchone()
            progress=connection.execute("SELECT * FROM audiobook_progress").fetchone()
        self.assertEqual((row["title"],row["visibility"],row["updated_at"]),("Reader edit","shared",edited_at))
        self.assertEqual(progress["position_seconds"],37.5)
        self.assertTrue(original.is_file())

    def test_audiobook_publication_never_overwrites_colliding_filename(self):
        identifiers=iter(
            SimpleNamespace(hex=value)
            for value in ("a"*32,"b"*32,"c"*32,"d"*32,"e"*32)
        )
        sentinel=audiobooks_module.ORIGINALS/("c"*32+".mp3")
        sentinel.write_bytes(b"preexisting audiobook")
        with patch.object(audiobooks_module.uuid,"uuid4",side_effect=lambda:next(identifiers)), patch.object(
            audiobooks_module,"probe",return_value=({"title":"Collision safe"},60.0,[])
        ), patch.object(audiobooks_module,"cover",return_value=None):
            response=self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"n"*2048),"same-name.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        self.assertEqual(sentinel.read_bytes(),b"preexisting audiobook")
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            row=connection.execute("SELECT stored_name FROM audiobooks").fetchone()
        self.assertEqual(row["stored_name"],"e"*32+".mp3")

    def test_audiobook_cover_extraction_writes_exclusive_descriptor_and_preserves_collision(self):
        source=audiobooks_module.INCOMING/"cover-source.part"
        source.write_bytes(b"audio")
        destination=audiobooks_module.INCOMING/"cover-output.part"
        def write_image(_command,**options):
            self.assertEqual(len(options["pass_fds"]),1)
            os.write(options["pass_fds"][0],b"jpeg-bytes")
            return SimpleNamespace(returncode=0)
        with patch.object(audiobooks_module.subprocess,"run",side_effect=write_image):
            self.assertEqual(audiobooks_module.cover(source,destination),destination.name)
        self.assertEqual(destination.read_bytes(),b"jpeg-bytes")
        destination.write_bytes(b"preexisting-cover")
        with patch.object(audiobooks_module.subprocess,"run") as encoder:
            self.assertIsNone(audiobooks_module.cover(source,destination))
        encoder.assert_not_called()
        self.assertEqual(destination.read_bytes(),b"preexisting-cover")

    def test_audiobook_cleanup_failure_remains_journaled_and_reconciles_without_touching_original(self):
        with patch.object(
            audiobooks_module,"probe",return_value=({"title":"Recover cleanup"},60.0,[])
        ), patch.object(audiobooks_module,"cover",return_value=None), patch.object(
            audiobooks_module,"_cleanup_reservation_files",return_value=False
        ):
            response=self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"r"*2048),"recover.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        self.assertEqual(response.status_code,201,response.get_data(as_text=True))
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            row=dict(connection.execute("SELECT * FROM audiobooks").fetchone())
        original=audiobooks_module.ORIGINALS/row["stored_name"]
        before=original.read_bytes()
        with audiobook_streaming_module.queue_connection() as connection:
            reservation=connection.execute("SELECT state FROM audiobook_import_reservations").fetchone()
        self.assertEqual(reservation["state"],"activated")
        recovered=audiobooks_module.reconcile_incomplete_imports(0)
        self.assertEqual(recovered["completed"],1)
        self.assertEqual(original.read_bytes(),before)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT state FROM audiobook_playback_jobs").fetchone()[0],"pending")
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()),[])

    def test_audiobook_process_crash_after_library_commit_is_completed_from_journal(self):
        with patch.object(
            audiobooks_module,"probe",return_value=({"title":"Crash boundary"},60.0,[])
        ), patch.object(audiobooks_module,"cover",return_value=None), patch.object(
            audiobooks_module,"activate_reserved_import",side_effect=SystemExit("injected crash")
        ), self.assertRaises(SystemExit):
            self.client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"x"*2048),"crash.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf},
            )
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            saved=dict(connection.execute("SELECT * FROM audiobooks").fetchone())
        original=audiobooks_module.ORIGINALS/saved["stored_name"]
        expected=original.read_bytes()
        with audiobook_streaming_module.queue_connection() as connection:
            job=connection.execute("SELECT state,error_code FROM audiobook_playback_jobs").fetchone()
            catalog=connection.execute("SELECT active FROM audiobook_catalog_snapshot").fetchone()
            reservation=connection.execute("SELECT state FROM audiobook_import_reservations").fetchone()
        self.assertEqual(tuple(job),("failed","import_pending"))
        self.assertEqual(catalog["active"],0)
        self.assertEqual(reservation["state"],"queued")
        recovered=audiobooks_module.reconcile_incomplete_imports(0)
        self.assertEqual(recovered["completed"],1)
        self.assertEqual(original.read_bytes(),expected)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT state FROM audiobook_playback_jobs").fetchone()[0],"pending")
            self.assertEqual(connection.execute("SELECT active FROM audiobook_catalog_snapshot").fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()),[])

    def test_audiobook_process_crash_before_library_commit_aborts_only_reserved_artifacts(self):
        token="7"*32; book_id="8"*32
        staging=audiobooks_module.INCOMING/f"{token}.upload.part"
        staging.write_bytes(b"exclusive staged audiobook")
        details=staging.stat(follow_symlinks=False)
        digest=hashlib.sha256(staging.read_bytes()).hexdigest()
        final=audiobooks_module.ORIGINALS/f"{book_id}.mp3"
        audiobook_streaming_module.reserve_import(
            "9"*32,book_id,final.name,details.st_size,digest,60,
            staging.name,details.st_dev,details.st_ino,
        )
        os.link(staging,final,follow_symlinks=False)
        audiobook_streaming_module.mark_import_published("9"*32,book_id)
        audiobook_streaming_module.commit_reserved_import("9"*32,book_id)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(tuple(connection.execute(
                "SELECT state,error_code FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)
            ).fetchone()),("failed","import_pending"))
        recovered=audiobooks_module.reconcile_incomplete_imports(0)
        self.assertEqual(recovered["aborted"],1)
        self.assertFalse(staging.exists())
        self.assertFalse(final.exists())
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)

    def test_audiobook_recovery_waits_for_inflight_library_commit_before_abort_decision(self):
        token="a"*32; book_id="b"*32
        staging=audiobooks_module.INCOMING/f"{token}.upload.part"
        staging.write_bytes(b"ID3"+b"r"*2048)
        details=staging.stat(follow_symlinks=False)
        digest=hashlib.sha256(staging.read_bytes()).hexdigest()
        final=audiobooks_module.ORIGINALS/f"{book_id}.mp3"
        audiobook_streaming_module.reserve_import(
            token,book_id,final.name,details.st_size,digest,60,
            staging.name,details.st_dev,details.st_ino,
        )
        os.link(staging,final,follow_symlinks=False)
        audiobook_streaming_module.mark_import_published(token,book_id)
        inserted=threading.Event(); release=threading.Event(); errors=[]
        def commit_library_import():
            try:
                timestamp=audiobooks_module.utcnow()
                with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        """INSERT INTO audiobooks(
                           id,title,author,narrator,series,original_name,stored_name,cover_name,
                           content_type,byte_size,sha256,duration_seconds,chapters_json,owner_id,
                           owner_name,visibility,created_at,updated_at,deleted_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                        (book_id,"Raced commit","","","","race.mp3",final.name,None,
                         "audio/mpeg",details.st_size,digest,60,"[]","david@example.test",
                         "David","private",timestamp,timestamp),
                    )
                    audiobook_streaming_module.commit_reserved_import(token,book_id)
                    inserted.set()
                    if not release.wait(5): raise RuntimeError("test release timed out")
            except Exception as error:
                errors.append(error); inserted.set()
        writer=threading.Thread(target=commit_library_import)
        writer.start(); self.assertTrue(inserted.wait(5)); self.assertEqual(errors,[])
        recovered=[]
        recovery=threading.Thread(target=lambda: recovered.append(
            audiobooks_module.reconcile_incomplete_imports(0)
        ))
        recovery.start(); time.sleep(0.1)
        self.assertTrue(recovery.is_alive(),"recovery did not wait for the authoritative library writer")
        release.set(); writer.join(5); recovery.join(5)
        self.assertFalse(writer.is_alive()); self.assertFalse(recovery.is_alive()); self.assertEqual(errors,[])
        self.assertEqual(recovered[0]["completed"],1)
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT title FROM audiobooks WHERE id=?",(book_id,)).fetchone()[0],"Raced commit")
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT state FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()[0],"pending")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertTrue(final.is_file()); self.assertFalse(staging.exists())

    def test_audiobook_recovery_reactivates_legacy_aborting_journal_before_finishing(self):
        token="e"*32; book_id="f"*32
        staging=audiobooks_module.INCOMING/f"{token}.upload.part"
        staging.write_bytes(b"ID3"+b"l"*1024)
        details=staging.stat(follow_symlinks=False)
        digest=hashlib.sha256(staging.read_bytes()).hexdigest()
        final=audiobooks_module.ORIGINALS/f"{book_id}.mp3"
        audiobook_streaming_module.reserve_import(
            token,book_id,final.name,details.st_size,digest,60,
            staging.name,details.st_dev,details.st_ino,
        )
        os.link(staging,final,follow_symlinks=False)
        audiobook_streaming_module.mark_import_published(token,book_id)
        audiobook_streaming_module.commit_reserved_import(token,book_id)
        audiobook_streaming_module.begin_abort_import(token,book_id)
        timestamp=audiobooks_module.utcnow()
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO audiobooks(
                   id,title,author,narrator,series,original_name,stored_name,cover_name,
                   content_type,byte_size,sha256,duration_seconds,chapters_json,owner_id,
                   owner_name,visibility,created_at,updated_at,deleted_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (book_id,"Legacy race","","","","legacy.mp3",final.name,None,
                 "audio/mpeg",details.st_size,digest,60,"[]","david@example.test",
                 "David","private",timestamp,timestamp),
            )
        recovered=audiobooks_module.reconcile_incomplete_imports(0)
        self.assertEqual(recovered["completed"],1)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT state FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()[0],"pending")
            self.assertEqual(connection.execute("SELECT active FROM audiobook_catalog_snapshot WHERE book_id=?",(book_id,)).fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertTrue(final.is_file()); self.assertFalse(staging.exists())

    def test_audiobook_restart_defers_fresh_reservation_then_periodic_pass_converges(self):
        token="c"*32; book_id="d"*32
        staging=audiobooks_module.INCOMING/f"{token}.upload.part"
        staging.write_bytes(b"pending import")
        details=staging.stat(follow_symlinks=False)
        digest=hashlib.sha256(staging.read_bytes()).hexdigest()
        audiobook_streaming_module.reserve_import(
            token,book_id,f"{book_id}.mp3",details.st_size,digest,60,
            staging.name,details.st_dev,details.st_ino,
        )
        # `force=True` is the same entry point init_audiobooks uses after an
        # immediate supervisor restart.  The fresh in-flight state survives.
        first=audiobooks_module.scheduled_import_recovery(force=True,current_epoch=1_000)
        self.assertEqual(first["deferred"],1)
        self.assertTrue(staging.is_file())
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],1)
        self.assertIsNone(audiobooks_module.scheduled_import_recovery(force=True,current_epoch=1_001))
        with audiobook_streaming_module.queue_connection() as connection:
            connection.execute(
                "UPDATE audiobook_import_reservations SET updated_at='2000-01-01T00:00:00+00:00'"
            )
        later=audiobooks_module.scheduled_import_recovery(
            force=True,current_epoch=1_000+audiobooks_module.IMPORT_RECOVERY_INTERVAL_SECONDS+1
        )
        self.assertEqual(later["aborted"],1)
        self.assertFalse(staging.exists())
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)

    def test_audiobook_periodic_recovery_has_one_owner_for_the_entire_pass(self):
        entered=threading.Event(); release=threading.Event()
        def blocking_recovery():
            entered.set()
            if not release.wait(5): raise RuntimeError("test release timed out")
            return {"completed":0,"aborted":0,"deferred":0,"staging_removed":0}
        results=[]; errors=[]
        def run_first():
            try:
                results.append(audiobooks_module.scheduled_import_recovery(force=True,current_epoch=2_000))
            except Exception as error:
                errors.append(error)
        with patch.object(audiobooks_module,"claim_import_recovery",return_value=True), patch.object(
            audiobooks_module,"reconcile_incomplete_imports",side_effect=blocking_recovery
        ):
            first=threading.Thread(target=run_first); first.start()
            self.assertTrue(entered.wait(5))
            self.assertIsNone(audiobooks_module.scheduled_import_recovery(force=True,current_epoch=3_000))
            release.set(); first.join(5)
        self.assertFalse(first.is_alive()); self.assertEqual(errors,[])
        self.assertEqual(results,[{"completed":0,"aborted":0,"deferred":0,"staging_removed":0}])

    def test_audiobook_health_probe_drives_periodic_recovery_schedule(self):
        audiobook_result={"completed":0,"aborted":0,"deferred":0,"staging_removed":0}
        audiobooks_module._next_import_recovery_poll=0
        with patch.object(audiobooks_module,"claim_import_recovery",return_value=True) as claim, patch.object(
            audiobooks_module,"reconcile_incomplete_imports",return_value=audiobook_result
        ) as recover:
            response=self.client.get("/health")
        self.assertEqual(response.status_code,200)
        claim.assert_called_once_with(audiobooks_module.IMPORT_RECOVERY_INTERVAL_SECONDS,current_epoch=None)
        recover.assert_called_once_with()

    def test_audiobook_cleanup_quarantines_before_identity_check_and_preserves_replacements(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary)/"incoming"; parent.mkdir()
            target=parent/"book.upload.part"; target.write_bytes(b"owned")
            details=target.stat(follow_symlinks=False)
            displaced=parent/"displaced-owned"
            real_rename=audiobooks_module._rename_noreplace_at
            swapped=False
            def swap_at_rename(source_directory,source_name,target_directory,target_name):
                nonlocal swapped
                if not swapped:
                    swapped=True
                    target.rename(displaced)
                    target.write_bytes(b"replacement")
                return real_rename(source_directory,source_name,target_directory,target_name)
            with patch.object(audiobooks_module,"_rename_noreplace_at",side_effect=swap_at_rename):
                self.assertFalse(audiobooks_module._unlink_matching(target,details.st_dev,details.st_ino))
            self.assertEqual(target.read_bytes(),b"replacement")
            self.assertEqual(displaced.read_bytes(),b"owned")

    def test_audiobook_cleanup_public_replacement_at_private_unlink_is_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary)/"incoming"; parent.mkdir()
            target=parent/"book.upload.part"; target.write_bytes(b"owned")
            details=target.stat(follow_symlinks=False)
            real_unlink=os.unlink; installed=False
            def replace_public_before_unlink(path,*args,**kwargs):
                nonlocal installed
                if path=="held" and kwargs.get("dir_fd") is not None and not installed:
                    installed=True; target.write_bytes(b"replacement")
                return real_unlink(path,*args,**kwargs)
            with patch.object(audiobooks_module.os,"unlink",side_effect=replace_public_before_unlink):
                self.assertTrue(audiobooks_module._unlink_matching(target,details.st_dev,details.st_ino))
            self.assertEqual(target.read_bytes(),b"replacement")

    def test_audiobook_cleanup_private_quarantine_converges_after_unlink_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary)/"incoming"; parent.mkdir()
            target=parent/"book.upload.part"; target.write_bytes(b"owned")
            details=target.stat(follow_symlinks=False)
            with patch.object(audiobooks_module.os,"unlink",side_effect=OSError("injected unlink failure")):
                self.assertFalse(audiobooks_module._unlink_matching(target,details.st_dev,details.st_ino))
            self.assertFalse(target.exists())
            quarantines=list(parent.glob(".audiobook-cleanup-*"))
            self.assertEqual(len(quarantines),1)
            self.assertEqual((quarantines[0]/"held").read_bytes(),b"owned")
            self.assertTrue(audiobooks_module._unlink_matching(target,details.st_dev,details.st_ino))
            self.assertEqual(list(parent.iterdir()),[])

    def test_audiobook_cleanup_uses_kernel_renameat2_when_libc_has_no_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "source"
            destination = parent / "destination"
            source.write_bytes(b"owned")
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patch.object(audiobooks_module, "_RENAMEAT2_FUNCTION", None):
                    audiobooks_module._rename_noreplace_at(
                        directory, source.name, directory, destination.name
                    )
            finally:
                os.close(directory)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"owned")

    def test_audiobook_cleanup_fails_closed_without_renameat2_interface(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "source"
            destination = parent / "destination"
            source.write_bytes(b"owned")
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patch.object(audiobooks_module, "_RENAMEAT2_FUNCTION", None), patch.object(
                    audiobooks_module, "_SYSCALL_FUNCTION", None
                ):
                    with self.assertRaises(OSError) as raised:
                        audiobooks_module._rename_noreplace_at(
                            directory, source.name, directory, destination.name
                        )
            finally:
                os.close(directory)
            self.assertEqual(raised.exception.errno, errno.ENOSYS)
            self.assertEqual(source.read_bytes(), b"owned")
            self.assertFalse(destination.exists())

    def test_audiobook_cleanup_syscall_collision_preserves_both_files(self):
        if audiobooks_module._SYSCALL_FUNCTION is None or audiobooks_module._RENAMEAT2_SYSCALL is None:
            self.skipTest("supported Linux renameat2 syscall is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "source"
            destination = parent / "destination"
            source.write_bytes(b"source-owned")
            destination.write_bytes(b"destination-owned")
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patch.object(audiobooks_module, "_RENAMEAT2_FUNCTION", None):
                    with self.assertRaises(OSError) as raised:
                        audiobooks_module._rename_noreplace_at(
                            directory, source.name, directory, destination.name
                        )
            finally:
                os.close(directory)
            self.assertEqual(raised.exception.errno, errno.EEXIST)
            self.assertEqual(source.read_bytes(), b"source-owned")
            self.assertEqual(destination.read_bytes(), b"destination-owned")

    def test_audiobook_unlink_preserves_staging_without_renameat2_interface(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "book.upload.part"
            target.write_bytes(b"saved-staging")
            details = target.stat(follow_symlinks=False)
            with patch.object(audiobooks_module, "_RENAMEAT2_FUNCTION", None), patch.object(
                audiobooks_module, "_SYSCALL_FUNCTION", None
            ):
                self.assertFalse(
                    audiobooks_module._unlink_matching(
                        target, details.st_dev, details.st_ino
                    )
                )
            self.assertEqual(target.read_bytes(), b"saved-staging")

    def test_audiobook_cleanup_unsupported_platform_never_calls_raw_syscall(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "source"
            source.write_bytes(b"owned")
            directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with patch.object(audiobooks_module, "_RENAMEAT2_FUNCTION", None), patch.object(
                    audiobooks_module, "_RENAMEAT2_SYSCALL", None
                ), patch.object(audiobooks_module, "_SYSCALL_FUNCTION") as syscall:
                    with self.assertRaises(OSError) as raised:
                        audiobooks_module._rename_noreplace_at(
                            directory, source.name, directory, "destination"
                        )
            finally:
                os.close(directory)
            self.assertEqual(raised.exception.errno, errno.ENOSYS)
            syscall.assert_not_called()
            self.assertEqual(source.read_bytes(), b"owned")

    def test_audiobook_cleanup_release_image_matches_expected_abi(self):
        expected = os.environ.get("DAVID_PI_EXPECT_RENAMEAT2_ARCH", "").strip()
        if not expected:
            self.skipTest("release-image ABI assertion is enabled only by the image gate")
        self.assertTrue(sys.platform.startswith("linux"))
        self.assertEqual(os.uname().machine.lower(), expected)
        self.assertEqual(audiobooks_module._RENAMEAT2_SYSCALL, {"aarch64":276,"x86_64":316}[expected])
        self.assertIsNone(audiobooks_module._RENAMEAT2_FUNCTION)
        self.assertIsNotNone(audiobooks_module._SYSCALL_FUNCTION)
        self.assertIs(audiobooks_module._SYSCALL_FUNCTION.restype, ctypes.c_long)

    def test_audiobook_cleanup_parent_rebinding_cannot_redirect_dirfd_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); parent=root/"incoming"; parent.mkdir()
            target=parent/"book.upload.part"; target.write_bytes(b"owned")
            details=target.stat(follow_symlinks=False)
            old_parent=root/"old-incoming"
            real_rename=audiobooks_module._rename_noreplace_at
            rebound=False
            def rebind_parent(source_directory,source_name,target_directory,target_name):
                nonlocal rebound
                if not rebound:
                    rebound=True
                    parent.rename(old_parent); parent.mkdir()
                    (parent/target.name).write_bytes(b"replacement-parent")
                return real_rename(source_directory,source_name,target_directory,target_name)
            with patch.object(audiobooks_module,"_rename_noreplace_at",side_effect=rebind_parent):
                self.assertTrue(audiobooks_module._unlink_matching(target,details.st_dev,details.st_ino))
            self.assertEqual((parent/target.name).read_bytes(),b"replacement-parent")
            self.assertFalse((old_parent/target.name).exists())

    def test_concurrent_duplicate_audiobook_import_commits_one_complete_identity(self):
        gate=threading.Barrier(2)
        def synchronized_probe(_path):
            gate.wait(timeout=5)
            return {"title":"One copy"},60.0,[]
        first,first_csrf=self.paired_client("david","David")
        second,second_csrf=self.paired_client("david","David")
        responses=[]
        def send(client,csrf):
            responses.append(client.post(
                "/api/audiobooks/upload",
                data={"visibility":"private","books":(BytesIO(b"ID3"+b"q"*2048),"duplicate.mp3")},
                content_type="multipart/form-data",headers={"X-CSRF-Token":csrf},
            ))
        with patch.object(audiobooks_module,"probe",side_effect=synchronized_probe), patch.object(audiobooks_module,"cover",return_value=None):
            threads=[threading.Thread(target=send,args=pair) for pair in ((first,first_csrf),(second,second_csrf))]
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(response.status_code for response in responses),[201,409])
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0],1)
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_playback_jobs WHERE state='pending'").fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_catalog_snapshot WHERE active=1").fetchone()[0],1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audiobook_import_reservations").fetchone()[0],0)
        self.assertEqual(len(list(audiobooks_module.ORIGINALS.iterdir())),1)
        self.assertEqual(list(audiobooks_module.INCOMING.iterdir()),[])

    def test_audiobook_probe_rejects_unresolved_duration(self):
        for duration in (None, "0", "-1", "NaN", "Infinity", "-Infinity"):
            payload = json.dumps({"format": {"duration": duration, "tags": {}}, "chapters": []})
            with self.subTest(duration=duration), patch.object(
                audiobooks_module.subprocess,
                "run",
                return_value=SimpleNamespace(stdout=payload),
            ), self.assertRaises(audiobooks_module.AudiobookDurationError):
                audiobooks_module.probe(Path(TEST_DATA.name) / "probe.mp3")

    def test_audiobook_public_dto_and_deleted_books_are_owner_only(self):
        with patch.object(audiobooks_module,"probe",return_value=({"title":"Shared Safe"},60.0,[])), patch.object(audiobooks_module,"cover",return_value=None):
            uploaded=self.client.post("/api/audiobooks/upload",data={"visibility":"shared","books":(BytesIO(b"ID3"+b"s"*2048),"shared.mp3")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(uploaded.status_code,201)
        david_payload=self.client.get("/api/audiobooks").get_json();item=david_payload["books"][0];book_id=item["id"]
        forbidden={"owner_id","owner_name","owner_display","stored_name","original_name","cover_name","chapters_json","created_at","updated_at","deleted_at"}
        self.assertRegex(item["sha256"],r"^[0-9a-f]{64}$")
        self.assertFalse(forbidden.intersection(item))
        detail=self.client.get(f"/api/audiobooks/{book_id}").get_json()["book"]
        self.assertFalse(forbidden.intersection(detail));self.assertIn("chapters",detail)
        diana,diana_csrf=self.paired_client("diana","Diana")
        diana_payload=diana.get("/api/audiobooks").get_json();self.assertEqual(len(diana_payload["books"]),1)
        self.assertRegex(david_payload["progress_scope"],r"^[0-9a-f]{32}$");self.assertRegex(diana_payload["progress_scope"],r"^[0-9a-f]{32}$")
        self.assertNotEqual(david_payload["progress_scope"],diana_payload["progress_scope"])
        self.assertEqual(audiobooks_module.progress_scope({"owner_id":"david@example.test","csrf_token":"one"}),audiobooks_module.progress_scope({"owner_id":"DAVID@example.test","csrf_token":"two"}))
        mismatched=diana.put(f"/api/audiobooks/{book_id}/progress",json={"position_seconds":30,"progress_scope":david_payload["progress_scope"]},headers={"X-CSRF-Token":diana_csrf})
        self.assertEqual(mismatched.status_code,409)
        self.assertEqual(self.client.delete(f"/api/audiobooks/{book_id}",headers={"X-CSRF-Token":self.csrf}).status_code,200)
        self.assertEqual(diana.get("/api/audiobooks?view=deleted").get_json()["books"],[])
        self.assertEqual(diana.get(f"/api/audiobooks/{book_id}?view=deleted").status_code,404)
        self.assertEqual(diana.post(f"/api/audiobooks/{book_id}/restore",headers={"X-CSRF-Token":diana_csrf}).status_code,404)

    def test_audiobook_segmented_playback_is_private_and_range_capable(self):
        with patch.object(audiobooks_module,"probe",return_value=({"title":"Segmented"},120.0,[])), patch.object(audiobooks_module,"cover",return_value=None):
            response=self.client.post("/api/audiobooks/upload",data={"visibility":"private","books":(BytesIO(b"ID3"+b"x"*4096),"segmented.mp3")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(response.status_code,201)
        pending=self.client.get("/api/audiobooks?owner=mine").get_json()["books"][0]
        self.assertEqual(pending["playback_mode"],"range")
        self.assertEqual(pending["playback_state"],"pending")
        self.assertIsNone(pending["hls_url"])
        target=audiobook_streaming_module.STREAMING/pending["id"]/f"v{audiobook_streaming_module.FORMAT_VERSION}"; target.mkdir(parents=True)
        (target/"index.m3u8").write_text("#EXTM3U\n#EXT-X-MAP:URI=\"index.m4s\",BYTERANGE=\"100@0\"\n#EXT-X-BYTERANGE:4@100\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n",encoding="utf-8")
        (target/"index.m4s").write_bytes(b"0"*100+b"DATA")
        with audiobook_streaming_module.queue_connection() as connection:
            connection.execute("UPDATE audiobook_playback_jobs SET state='ready',derivative_bytes=104 WHERE book_id=?",(pending["id"],))
        ready=self.client.get("/api/audiobooks?owner=mine").get_json()["books"][0]
        self.assertEqual(ready["playback_mode"],"segmented")
        self.assertTrue(ready["hls_url"].endswith("/index.m3u8"))
        playlist=self.client.get(ready["hls_url"])
        self.assertEqual(playlist.status_code,200)
        self.assertEqual(playlist.headers["Cache-Control"],"private, no-store")
        media=self.client.get(ready["hls_url"].replace("index.m3u8","index.m4s"),headers={"Range":"bytes=100-103"})
        self.assertEqual(media.status_code,206);self.assertEqual(media.data,b"DATA")
        self.assertEqual(media.headers["Accept-Ranges"],"bytes")
        self.assertIn("private",media.headers["Cache-Control"])
        diana,_=self.paired_client("diana","Diana")
        self.assertEqual(diana.get(ready["hls_url"]).status_code,404)

    def test_legacy_zero_duration_book_stays_readable_and_range_playable(self):
        book_id = "9" * 32
        stored_name = f"{book_id}.mp3"
        source = audiobooks_module.ORIGINALS / stored_name
        source.write_bytes(b"ID3legacy-range-audio")
        now = "2026-01-01T00:00:00+00:00"
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO audiobooks(
                id,title,author,series,original_name,stored_name,content_type,
                byte_size,sha256,duration_seconds,chapters_json,owner_id,
                owner_name,visibility,created_at,updated_at,deleted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    book_id,"Legacy timing","","","legacy.mp3",stored_name,
                    "audio/mpeg",source.stat().st_size,"legacy",0,"[]",
                    "david@example.test","David","shared",now,now,
                ),
            )
        forecast = audiobook_streaming_module.reconcile_catalog([{
            "id": book_id,
            "stored_name": stored_name,
            "byte_size": source.stat().st_size,
            "sha256": "legacy",
            "duration_seconds": 0,
        }])
        self.assertEqual((forecast["active_books"], forecast["range_only_books"]), (0, 1))
        item = self.client.get("/api/audiobooks").get_json()["books"][0]
        self.assertEqual(item["duration_seconds"], 0)
        self.assertEqual((item["playback_mode"], item["playback_state"]), ("range", "failed"))
        self.assertIsNone(item["hls_url"])
        stream = self.client.get(item["stream_url"], headers={"Range": "bytes=0-2"})
        self.assertEqual((stream.status_code, stream.data), (206, b"ID3"))
        self.assertEqual(self.client.get(f"/api/audiobooks/{book_id}/hls/index.m3u8").status_code, 404)

    def test_audiobook_derivative_validation_and_atomic_original_preservation(self):
        book_id="a"*32;source=audiobook_streaming_module.ORIGINALS/f"{book_id}.m4b";original=b"immutable-original"*1024;source.write_bytes(original)
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size,duration_seconds=100)
        job=audiobook_streaming_module.claim_next_job()
        commands=[]
        def fake_run(command,**_kwargs):
            commands.append(command)
            if command[0]=="ffprobe":return SimpleNamespace(stdout='{"streams":[{"codec_name":"aac","channels":2,"bit_rate":"96000"}],"format":{"duration":"100"}}')
            playlist=Path(command[-1]);media=Path(command[command.index("-hls_segment_filename")+1])
            media.write_bytes(b"M"*2048)
            playlist.write_text('#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n#EXT-X-BYTERANGE:1948@100\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n',encoding="utf-8")
            return SimpleNamespace(stdout="")
        with patch.object(audiobook_streaming_module.subprocess,"run",side_effect=fake_run):
            audiobook_streaming_module.prepare_job(job)
        self.assertEqual(source.read_bytes(),original)
        self.assertIsNotNone(audiobook_streaming_module.derivative_paths(book_id))
        state=audiobook_streaming_module.playback_status(book_id)
        self.assertEqual(state["state"],"ready")
        transcode=next(command for command in commands if command[0]=="ffmpeg")
        self.assertIn("-b:a",transcode);self.assertIn("96k",transcode)
        self.assertIn("-ac",transcode);self.assertIn("2",transcode)
        self.assertNotIn("copy",transcode)

    def test_audiobook_compact_listing_is_paged_and_uses_bulk_playback_status(self):
        now="2026-01-01T00:00:00+00:00"
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            for index in range(30):
                book_id=f"{index:032x}"
                connection.execute(
                    """INSERT INTO audiobooks(id,title,author,series,original_name,stored_name,content_type,byte_size,sha256,duration_seconds,chapters_json,owner_id,owner_name,visibility,created_at,updated_at,deleted_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
                    (book_id,f"Book {index:02d}","Author","",f"book-{index}.mp3",f"{book_id}.mp3","audio/mpeg",1024,f"hash-{index}",3600,"[]","david@example.test","David","shared",now,now),
                )
        with patch.object(audiobooks_module,"playback_status",side_effect=AssertionError("per-book queue query")), patch.object(audiobooks_module,"playback_statuses",wraps=audiobook_streaming_module.playback_statuses) as bulk:
            first=self.client.get("/api/audiobooks?compact=1&limit=12").get_json()
        self.assertEqual((first["total"],len(first["books"]),first["next_offset"],first["has_more"]),(30,12,12,True))
        self.assertNotIn("chapters",first["books"][0]);self.assertEqual(bulk.call_count,1)
        second=self.client.get("/api/audiobooks?compact=1&limit=12&offset=24").get_json()
        self.assertEqual((len(second["books"]),second["next_offset"],second["has_more"]),(6,None,False))
        detail=self.client.get(f"/api/audiobooks/{first['books'][0]['id']}").get_json()["book"]
        self.assertEqual(detail["chapters"],[])

    def test_audiobook_worker_does_not_discover_uncatalogued_originals(self):
        orphan_id="d"*32
        (audiobook_streaming_module.ORIGINALS/f"{orphan_id}.mp3").write_bytes(b"not-catalogued")
        with patch.object(audiobook_streaming_module,"system_pause_reason",return_value=None):
            self.assertEqual(audiobook_streaming_module.work_once(),{"state":"idle"})
        with audiobook_streaming_module.queue_connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM audiobook_playback_jobs WHERE book_id=?",(orphan_id,)).fetchone())

    def test_audiobook_worker_pauses_for_storage_temperature_or_load(self):
        with patch.object(audiobook_streaming_module.shutil,"disk_usage",return_value=SimpleNamespace(free=1)):
            self.assertEqual(audiobook_streaming_module.system_pause_reason(),"low_storage")
        with patch.object(audiobook_streaming_module.shutil,"disk_usage",return_value=SimpleNamespace(free=200*1024**3)), patch.object(audiobook_streaming_module.os,"getloadavg",return_value=(99,1,1)):
            self.assertEqual(audiobook_streaming_module.system_pause_reason(),"load_high")

    def test_audiobook_worker_restart_requeues_interrupted_job_and_clears_staging(self):
        book_id="b"*32
        source=audiobook_streaming_module.ORIGINALS/f"{book_id}.m4b"
        source.write_bytes(b"original")
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size,duration_seconds=100)
        self.assertEqual(audiobook_streaming_module.claim_next_job()["book_id"],book_id)
        stale=audiobook_streaming_module.STAGING/f"{book_id}-stale"
        stale.mkdir(parents=True)
        (stale/"partial.m4s").write_bytes(b"partial")
        self.assertEqual(audiobook_streaming_module.recover_interrupted_worker(),1)
        self.assertFalse(stale.exists())
        with audiobook_streaming_module.queue_connection() as connection:
            row=connection.execute("SELECT state,error_code FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()
        self.assertEqual((row["state"],row["error_code"]),("pending","worker_restarted"))

    def test_audiobook_preflight_failure_never_leaves_job_preparing(self):
        book_id="c"*32
        source=audiobook_streaming_module.ORIGINALS/f"{book_id}.m4b"
        source.write_bytes(b"malformed-original")
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size,duration_seconds=100)
        job=audiobook_streaming_module.claim_next_job()
        with patch.object(
            audiobook_streaming_module.subprocess,
            "run",
            side_effect=__import__("subprocess").CalledProcessError(1,["ffprobe"]),
        ):
            with self.assertRaises(__import__("subprocess").CalledProcessError):
                audiobook_streaming_module.prepare_job(job)
        with audiobook_streaming_module.queue_connection() as connection:
            row=connection.execute(
                "SELECT state,error_code FROM audiobook_playback_jobs WHERE book_id=?",
                (book_id,),
            ).fetchone()
        self.assertEqual((row["state"],row["error_code"]),("pending","prepare_failed"))

    def test_audiobook_stale_worker_generation_cannot_overwrite_new_queue_state(self):
        book_id="e"*32
        audiobook_streaming_module.enqueue(book_id,f"{book_id}.mp3",100,"old",100)
        stale=audiobook_streaming_module.claim_next_job();self.assertIsNotNone(stale)
        audiobook_streaming_module.suspend(book_id)
        audiobook_streaming_module.enqueue(book_id,f"{book_id}.mp3",101,"new",100)
        audiobook_streaming_module._fail_job(stale,ValueError("prepare_failed"))
        with audiobook_streaming_module.queue_connection() as connection:
            row=connection.execute("SELECT state,source_size,source_sha256,generation,lease_token FROM audiobook_playback_jobs WHERE book_id=?",(book_id,)).fetchone()
        self.assertEqual((row["state"],row["source_size"],row["source_sha256"]),("pending",101,"new"))
        self.assertGreater(row["generation"],stale["generation"]);self.assertIsNone(row["lease_token"])

    def test_audiobook_manifest_ranges_reject_overlap_and_out_of_bounds(self):
        target=audiobook_streaming_module.STAGING/"range-validation";target.mkdir(parents=True)
        (target/"index.m4s").write_bytes(b"x"*2048)
        (target/"index.m3u8").write_text('#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n#EXT-X-BYTERANGE:100@50\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n')
        with self.assertRaisesRegex(ValueError,"playlist_range_out_of_bounds"):
            audiobook_streaming_module._validate_derivative(target,20)

    def test_audiobook_recovery_restores_previous_derivative_before_ready_cas(self):
        book_id="f"*32
        audiobook_streaming_module.enqueue(book_id,f"{book_id}.mp3",100,"source",100)
        parent=audiobook_streaming_module.STREAMING/book_id;parent.mkdir(parents=True)
        final=parent/f"v{audiobook_streaming_module.FORMAT_VERSION}";previous=parent/(".previous-"+"1"*32)
        final.mkdir();(final/"candidate").write_text("new")
        previous.mkdir();(previous/"known-good").write_text("old")
        audiobook_streaming_module.recover_interrupted_worker()
        self.assertTrue((final/"known-good").is_file());self.assertFalse((final/"candidate").exists());self.assertFalse(previous.exists())

    def test_audiobook_catalog_reconciliation_suspends_orphans_and_forecasts_storage(self):
        orphan="7"*32;active="8"*32
        audiobook_streaming_module.enqueue(orphan,f"{orphan}.mp3",500,"orphan",60)
        result=audiobook_streaming_module.reconcile_catalog([{"id":active,"stored_name":f"{active}.mp3","byte_size":1000,"sha256":"active","duration_seconds":60}])
        with audiobook_streaming_module.queue_connection() as connection:
            rows={row["book_id"]:dict(row) for row in connection.execute("SELECT * FROM audiobook_playback_jobs")}
        self.assertEqual((rows[orphan]["state"],rows[orphan]["error_code"]),("failed","inactive"))
        self.assertEqual(rows[active]["state"],"pending")
        expected=audiobook_streaming_module._derivative_byte_ceiling(
            audiobook_streaming_module._forecast_duration_ceiling(60),2
        )
        self.assertEqual(result["active_books"],1);self.assertEqual(result["estimated_derivative_bytes"],expected)
        self.assertEqual(result["additional_bytes"],expected);self.assertIn("retained_legacy_bytes",result);self.assertIn("fits",result)

    def test_audiobook_derivative_probe_enforces_codec_channels_bitrate_and_duration(self):
        target=audiobook_streaming_module.STAGING/"probe-validation";target.mkdir(parents=True)
        (target/"index.m4s").write_bytes(b"x"*2048)
        (target/"index.m3u8").write_text('#EXTM3U\n#EXT-X-MAP:URI="index.m4s",BYTERANGE="100@0"\n#EXT-X-BYTERANGE:1948@100\n#EXTINF:20,\nindex.m4s\n#EXT-X-ENDLIST\n')
        valid={"codec":"aac","channels":1,"duration":100.0,"bit_rate":64000}
        with patch.object(audiobook_streaming_module,"_probe",return_value=valid):
            self.assertGreater(audiobook_streaming_module._validate_derivative(target,100,1),2048)
        for invalid,code in [({**valid,"codec":"mp3"},"codec"),({**valid,"channels":2},"channels"),({**valid,"bit_rate":200000},"bitrate"),({**valid,"duration":80},"duration")]:
            with self.subTest(code=code),patch.object(audiobook_streaming_module,"_probe",return_value=invalid),self.assertRaisesRegex(ValueError,code):
                audiobook_streaming_module._validate_derivative(target,100,1)

    def test_audiobook_service_worker_never_caches_private_catalog_or_media(self):
        source=(Path(__file__).resolve().parents[1]/"static"/"sw.js").read_text()
        self.assertIn("url.pathname.startsWith('/api/')",source)
        self.assertIn("url.pathname.startsWith('/media/')",source)
        self.assertNotIn("/api/audiobooks",source.split("const SHELL = [",1)[1].split("];",1)[0])
        self.assertIn("min-height: 44px",(Path(__file__).resolve().parents[1]/"static"/"audiobooks-offline.css").read_text())

    def test_audiobook_player_prefers_hls_with_bounded_buffer_and_range_fallback(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/"static"/"audiobooks.js").read_text(encoding="utf-8")
        self.assertIn("audio.canPlayType('application/vnd.apple.mpegurl')",script)
        self.assertIn("loadAudiobookHlsLibrary",script)
        self.assertIn("Hls.isSupported()",script)
        self.assertIn("maxBufferLength:60",script)
        self.assertIn("maxBufferSize:33554432",script)
        self.assertIn("backBufferLength:30",script)
        self.assertIn("useRangeAudiobookStream",script)
        self.assertIn("destroyAudiobookStream",script)

    def test_gallery_cursor_is_stable_and_response_is_lean(self):
        for index in range(35):
            self.add_photo(f"cursor-{index:02d}")
        first = self.client.get("/api/photos?limit=20").get_json()
        self.assertEqual(first["total"], 35)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(first["photos"]), 20)
        self.assertNotIn("stored_path", first["photos"][0])
        self.assertNotIn("content_sha256", first["photos"][0])
        second = self.client.get(
            f"/api/photos?limit=20&cursor={first['next_cursor']}"
        ).get_json()
        self.assertIsNone(second["total"])
        self.assertFalse(second["has_more"])
        self.assertEqual(len(second["photos"]), 15)
        ids = [item["id"] for item in first["photos"] + second["photos"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(
            self.client.get("/api/photos?cursor=not-a-real-cursor").status_code,
            400,
        )

    def test_gallery_read_endpoints_resolve_identity_once_and_do_not_mutate(self):
        self.add_photo(
            "identity-once", owner_id="david@example.test", owner_name="David",
        )
        self.add_collection(
            "identity-collection", "Identity", owner_id="david@example.test",
            owner_name="David",
        )
        actor = {
            "profile": "home", "name": "David", "owner_id": "david@example.test",
            "verified": True, "csrf_token": self.csrf,
        }
        with portal.db() as connection:
            before = (
                connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM collection_photos").fetchone()[0],
            )
        for path in (
            "/api/photos?view=mine", "/api/photos/timeline?view=mine",
            "/api/photos/deleted?view=mine", "/api/collections?view=mine",
        ):
            with self.subTest(path=path), patch.object(
                portal, "current_device", return_value=actor,
            ) as identity_resolver:
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(identity_resolver.call_count, 1)
        with portal.db() as connection:
            after = (
                connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM collection_photos").fetchone()[0],
            )
        self.assertEqual(after, before)

    def test_gallery_orders_by_capture_metadata_not_upload_time(self):
        self.add_photo("capture-newer")
        self.add_photo("capture-older")
        with portal.db() as connection:
            connection.execute(
                """UPDATE photos SET capture_timestamp=?, uploaded_at=? WHERE id=?""",
                ("2024-02-03T04:05:06+00:00", "2020-01-01T00:00:00+00:00", "capture-newer"),
            )
            connection.execute(
                """UPDATE photos SET capture_timestamp=?, uploaded_at=? WHERE id=?""",
                ("2021-02-03T04:05:06+00:00", "2026-01-01T00:00:00+00:00", "capture-older"),
            )
        result = self.client.get("/api/photos?limit=10").get_json()["photos"]
        self.assertEqual([item["id"] for item in result[:2]], ["capture-newer", "capture-older"])
        self.assertEqual(result[0]["captured_at"], "2024-02-03T04:05:06+00:00")

    def test_gallery_timeline_and_period_filter_are_bounded(self):
        for photo_id, captured in (
            ("timeline-a", "2024-01-05T10:00:00+00:00"),
            ("timeline-b", "2024-01-28T10:00:00+00:00"),
            ("timeline-c", "2023-12-31T10:00:00+00:00"),
        ):
            self.add_photo(photo_id)
            with portal.db() as connection:
                connection.execute(
                    "UPDATE photos SET capture_timestamp=?,taken_at=? WHERE id=?",
                    (captured, captured, photo_id),
                )
        timeline = self.client.get("/api/photos/timeline").get_json()
        self.assertEqual(timeline["months"][0], {"month": "2024-01", "item_count": 2})
        self.assertEqual(timeline["total"], 3)
        january = self.client.get("/api/photos?period=2024-01").get_json()
        self.assertEqual({item["id"] for item in january["photos"]}, {"timeline-a", "timeline-b"})
        self.assertEqual(january["period"], "2024-01")
        year = self.client.get("/api/photos?period=2023").get_json()
        self.assertEqual([item["id"] for item in year["photos"]], ["timeline-c"])
        self.assertEqual(self.client.get("/api/photos?period=2024-13").status_code, 400)

    def test_existing_videos_are_not_silently_claimed_during_startup(self):
        self.add_photo("auto-video")
        with portal.db() as connection:
            connection.execute(
                "UPDATE photos SET content_type='video/mp4' WHERE id='auto-video'"
            )
        portal.initialize_once()
        with portal.db() as connection:
            collection = connection.execute(
                "SELECT id FROM collections WHERE lower(name)='videos'"
            ).fetchone()
            self.assertIsNone(collection)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM collection_photos WHERE photo_id='auto-video'"
            ).fetchone())

    def test_owner_specific_videos_collections_do_not_leak_between_users(self):
        self.add_photo("david-private-video", owner_id="david@example.test", owner_name="David", visibility="private")
        self.add_photo("diana-private-video", owner_id="diana@example.test", owner_name="Diana", visibility="private")
        self.add_collection("david-videos", "Videos", owner_id="david@example.test", owner_name="David", visibility="private")
        self.add_collection("diana-videos", "Videos", owner_id="diana@example.test", owner_name="Diana", visibility="private")
        self.add_membership("david-videos", "david-private-video", "david@example.test")
        self.add_membership("diana-videos", "diana-private-video", "diana@example.test")
        listing = self.client.get("/api/collections?view=mine").get_json()["collections"]
        videos = next(item for item in listing if item["name"].casefold() == "videos")
        self.assertEqual(videos["photo_count"], 1)
        self.assertEqual(videos["id"], "david-videos")
        photos = self.client.get(
            f"/api/photos?view=mine&collection={videos['id']}"
        ).get_json()["photos"]
        self.assertEqual([item["id"] for item in photos], ["david-private-video"])

    def test_android_shell_uses_shared_web_dialogs_and_has_audiobooks(self):
        root = Path(__file__).resolve().parents[1]
        shell = (root / "clients" / "android" / "app" / "src" / "main" / "java" / "com" / "davidpi" / "backup" / "PortalShell.kt").read_text(encoding="utf-8")
        dialog_host = (root / "static" / "mobile-dialog-host.js").read_text(encoding="utf-8")
        audiobooks = (root / "static" / "audiobooks.js").read_text(encoding="utf-8")
        self.assertIn('"Audiobooks" to "/audiobooks"', shell)
        self.assertNotIn("david-pi-android-dialog-fix", shell)
        self.assertIn("Android", dialog_host)
        self.assertIn("navigator.mediaSession", audiobooks)

    def test_android_audiobooks_use_native_lock_screen_bridge(self):
        root = Path(__file__).resolve().parents[1]
        android_root = root / "clients" / "android" / "app" / "src" / "main"
        bridge = (android_root / "java" / "com" / "davidpi" / "backup" / "NativeMediaBridge.kt").read_text(encoding="utf-8")
        keepalive = (android_root / "java" / "com" / "davidpi" / "backup" / "AudiobookKeepAliveService.kt").read_text(encoding="utf-8")
        shell = (android_root / "java" / "com" / "davidpi" / "backup" / "PortalShell.kt").read_text(encoding="utf-8")
        audiobooks = (root / "static" / "audiobooks.js").read_text(encoding="utf-8")
        self.assertIn("MediaSessionCompat", bridge)
        self.assertIn("MediaStyle", bridge)
        self.assertIn("WebViewCompat.addWebMessageListener", shell)
        self.assertIn("setOf(DavidPiOrigin.ORIGIN)", shell)
        self.assertIn("!isMainFrame", shell)
        self.assertIn("DavidPiOrigin.canonicalPairingOrigin(sourceOrigin.toString())", shell)
        self.assertNotIn("addJavascriptInterface", shell)
        self.assertNotIn("@JavascriptInterface", bridge)
        self.assertIn("WebSettings.LOAD_DEFAULT", shell)
        self.assertIn("mediaPlaybackRequiresUserGesture = false", shell)
        self.assertNotIn("clearCache(true)", shell)
        self.assertNotIn("serviceWorker.getRegistrations", shell)
        self.assertNotIn("location.reload()", shell)
        self.assertIn("webView?.stopLoading()", shell)
        self.assertNotIn("webView?.destroy()", shell)
        self.assertIn("startForeground", keepalive)
        self.assertIn("AudiobookKeepAliveService", bridge)
        self.assertIn("setArtworkDataUrl", bridge)
        self.assertIn("METADATA_KEY_ALBUM_ART", bridge)
        self.assertIn("setLargeIcon(artwork)", bridge)
        self.assertIn("NativeMediaRegistry.current?.buildNotification()", keepalive)
        self.assertNotIn('setContentTitle(title)', keepalive)
        self.assertNotIn("} else {\n            context.stopService(keepAlive)", bridge)
        self.assertIn("davidPiNativeAudioCommand", audiobooks)
        self.assertIn("configureNativeArtwork", audiobooks)
        self.assertIn("audiobookPlayerGuard.listen(generation,audio,'canplay',resume,{once:true})", audiobooks)
        self.assertIn("seekAudiobookChapter", audiobooks)

    def test_gallery_has_continuous_loading_drill_in_and_viewport_organizer(self):
        root = Path(__file__).resolve().parents[1]
        gallery = (root / "static" / "gallery.js").read_text(encoding="utf-8")
        self.assertIn("openTimelinePeriod", gallery)
        self.assertIn("window.addEventListener('scroll'", gallery)
        self.assertIn("organizeSheet.__davidPiMobileHost", gallery)
        self.assertIn("organizeSheet.__davidPiMobileHost", gallery)
        self.assertIn("organizeSheet.showModal()", gallery)
        self.assertIn("captureGalleryPosition", gallery)

    def test_mobile_media_repairs_and_iphone_now_playing_contracts(self):
        root = Path(__file__).resolve().parents[1]
        gallery = (root / "static" / "gallery.js").read_text(encoding="utf-8")
        dialog_host = (root / "static" / "mobile-dialog-host.js").read_text(encoding="utf-8")
        audiobooks = (root / "static" / "audiobooks.js").read_text(encoding="utf-8")
        audiobook_page = (root / "templates" / "audiobooks.html").read_text(encoding="utf-8")
        self.assertIn("collection-card:not(.dynamic)", gallery)
        self.assertIn("api('/api/collections?view=mine')", gallery)
        self.assertIn("gallery-month-row", gallery)
        self.assertIn("openTimelinePeriod", gallery)
        self.assertIn("davidPiGalleryDensityV2", gallery)
        self.assertIn("gallery.dataset.densityLevel", gallery)
        self.assertIn("renderOrganizerCollections", gallery)
        self.assertIn("organizerCollectionFallback", gallery)
        self.assertIn(".movie-search-sheet, .recipe-view", dialog_host)
        self.assertIn("'overflow-y', scrollableDocument ? 'auto' : 'hidden'", dialog_host)
        self.assertIn("document.addEventListener('visibilitychange'", audiobooks)
        self.assertIn("'MediaMetadata' in window", audiobooks)
        self.assertIn('apple-mobile-web-app-capable', audiobook_page)

    def test_mobile_viewer_is_swipe_first_and_video_navigation_does_not_wait(self):
        root = Path(__file__).resolve().parents[1]
        gallery = (root / "static" / "gallery.js").read_text(encoding="utf-8")
        styles = (root / "static" / "app.css").read_text(encoding="utf-8")
        page = (root / "templates" / "photos.html").read_text(encoding="utf-8")
        touch_handler = gallery.split(
            "viewerStage.addEventListener('pointermove'", 1
        )[1].split("function finishViewerTouchPointer", 1)[0]
        self.assertIn("viewerTouchPointers.size === 2", touch_handler)
        self.assertIn("setViewerZoom(zoom * distance", touch_handler)
        self.assertIn("{passive: false, capture: true}", gallery)
        self.assertIn("stopViewerVideo()", gallery)
        self.assertIn("viewerVideo.removeAttribute('src')", gallery)
        self.assertIn("viewerVideo.load()", gallery)
        self.assertIn("@media (hover: none) and (pointer: coarse)", styles)
        self.assertIn(".viewer-nav { display: none; }", styles)
        self.assertIn('class="viewer-nav viewer-prev"', page)
        self.assertIn('preload="metadata"', page)

    def test_phone_viewer_uses_bounded_cached_webp_derivative(self):
        self.add_photo("viewer-optimized")
        preview = portal.PREVIEWS / "viewer-optimized.jpg"
        derivative = portal.VIEWER_PREVIEWS / "viewer-optimized.webp"
        portal.Image.new("RGB", (2600, 1800), "#d85e48").save(preview, "JPEG", quality=92)
        derivative.unlink(missing_ok=True)
        try:
            response = self.client.get("/media/view/viewer-optimized")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "image/webp")
            self.assertEqual(
                response.headers["Cache-Control"],
                "private, no-cache, max-age=0, must-revalidate",
            )
            self.assertNotIn("immutable", response.headers["Cache-Control"])
            self.assertTrue(derivative.is_file())
            with portal.Image.open(derivative) as image:
                self.assertLessEqual(max(image.size), 1600)
            page = self.client.get("/photos").get_data(as_text=True)
            self.assertIn("app.css?v=40", page)
            self.assertIn("gallery-window.js?v=5", page)
            self.assertIn("gallery.js?v=62", page)
            self.assertIn("mobile-dialog-host.js?v=10", page)
        finally:
            preview.unlink(missing_ok=True)
            derivative.unlink(missing_ok=True)

    def test_revocable_media_cache_revalidates_and_private_media_is_never_stored(self):
        photo_id = "revocable-cache-video"
        self.add_photo(
            photo_id,
            owner_id="david@example.test",
            owner_name="David",
            visibility="shared",
        )
        original = portal.ORIGINALS / f"{photo_id}.jpg"
        preview = portal.PREVIEWS / f"{photo_id}.jpg"
        thumb = portal.THUMBS / f"{photo_id}.jpg"
        playback = portal.PREVIEWS / f"{photo_id}.mp4"
        viewer = portal.VIEWER_PREVIEWS / f"{photo_id}.webp"
        original.write_bytes(b"0123456789")
        portal.Image.new("RGB", (120, 80), "#4969a8").save(preview, "JPEG")
        portal.Image.new("RGB", (60, 40), "#4969a8").save(thumb, "JPEG")
        playback.write_bytes(b"0123456789")
        with portal.db() as connection:
            connection.execute(
                """UPDATE photos SET content_type='video/mp4',playback_name=?
                   WHERE id=?""",
                (playback.name, photo_id),
            )
        diana, _token = self.paired_client("diana")
        paths = [
            f"/media/thumb/{photo_id}",
            f"/media/preview/{photo_id}",
            f"/media/view/{photo_id}",
            f"/media/original/{photo_id}",
            f"/media/play/{photo_id}",
        ]
        revalidate = "private, no-cache, max-age=0, must-revalidate"
        no_store = "private, no-store, max-age=0"
        shared_etags = {}
        try:
            # Diana is legitimately authorized while David's media is shared.
            # Every ordinary browser cache reuse is forced through the server.
            for path in paths:
                response = diana.get(path)
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.headers["Cache-Control"], revalidate, path)
                self.assertEqual(response.headers["Pragma"], "no-cache", path)
                self.assertEqual(response.headers["Expires"], "0", path)
                self.assertNotIn("immutable", response.headers["Cache-Control"], path)
                self.assertNotIn("X-David-Pi-Media-Cache-Scope", response.headers, path)
                shared_etags[path] = response.headers["ETag"]

                unchanged = diana.get(
                    path, headers={"If-None-Match": shared_etags[path]}
                )
                self.assertEqual(unchanged.status_code, 304, path)
                self.assertEqual(unchanged.headers["Cache-Control"], revalidate, path)

            ranged = diana.get(
                f"/media/play/{photo_id}",
                headers={
                    "Range": "bytes=2-5",
                    "If-Range": shared_etags[f"/media/play/{photo_id}"],
                },
            )
            self.assertEqual(ranged.status_code, 206)
            self.assertEqual(ranged.data, b"2345")
            self.assertEqual(ranged.headers["Cache-Control"], revalidate)

            made_private = self.client.patch(
                "/api/photos/visibility",
                json={
                    "items": [{"id": photo_id, "version": 1}],
                    "visibility": "private",
                },
            )
            self.assertEqual(made_private.status_code, 200)

            # A cache obeying the original must-revalidate response cannot use
            # its old body: revalidation is now denied, including Range paths.
            for path in paths:
                denied = diana.get(
                    path, headers={"If-None-Match": shared_etags[path]}
                )
                self.assertEqual(denied.status_code, 404, path)
                self.assertEqual(denied.headers["Cache-Control"], no_store, path)
            denied_range = diana.get(
                f"/media/play/{photo_id}",
                headers={
                    "Range": "bytes=2-5",
                    "If-Range": shared_etags[f"/media/play/{photo_id}"],
                },
            )
            self.assertEqual(denied_range.status_code, 404)
            self.assertEqual(denied_range.headers["Cache-Control"], no_store)

            # David still has owner access, but private responses cannot enter
            # a browser cache and carry a row-version-bound new validator.
            private_etags = {}
            for path in paths:
                private = self.client.get(path)
                self.assertEqual(private.status_code, 200, path)
                self.assertEqual(private.headers["Cache-Control"], no_store, path)
                self.assertNotIn("X-David-Pi-Media-Cache-Scope", private.headers, path)
                self.assertNotEqual(private.headers["ETag"], shared_etags[path], path)
                private_etags[path] = private.headers["ETag"]
            private_range = self.client.get(
                f"/media/play/{photo_id}",
                headers={
                    "Range": "bytes=2-5",
                    "If-Range": private_etags[f"/media/play/{photo_id}"],
                },
            )
            self.assertEqual(private_range.status_code, 206)
            self.assertEqual(private_range.headers["Cache-Control"], no_store)

            shared_again = self.client.patch(
                "/api/photos/visibility",
                json={
                    "items": [{"id": photo_id, "version": 2}],
                    "visibility": "shared",
                },
            )
            self.assertEqual(shared_again.status_code, 200)
            for path in paths:
                invalidated = diana.get(
                    path, headers={"If-None-Match": shared_etags[path]}
                )
                self.assertEqual(invalidated.status_code, 200, path)
                self.assertEqual(invalidated.headers["Cache-Control"], revalidate, path)
                self.assertNotEqual(invalidated.headers["ETag"], shared_etags[path], path)
        finally:
            for path in (original, preview, thumb, playback, viewer):
                path.unlink(missing_ok=True)

    def test_service_worker_never_caches_private_pages_or_media(self):
        root = Path(__file__).resolve().parents[1]
        worker = (root / "static" / "sw.js").read_text(encoding="utf-8")
        page = self.client.get("/photos").get_data(as_text=True)
        self.assertNotIn("const SHELL = ['/']", worker)
        self.assertIn("event.request.mode === 'navigate'", worker)
        self.assertIn("url.pathname.startsWith('/api/')", worker)
        self.assertIn("url.pathname.startsWith('/media/')", worker)
        self.assertLess(
            worker.index("url.pathname.startsWith('/media/')"),
            worker.index("if(cacheKey)"),
        )
        self.assertIn("david-pi-static-v44-portable-households", worker)
        self.assertIn("OFFLINE_AUDIOBOOK_PAGE", worker)
        self.assertNotIn("/api/audiobooks", worker.split("const SHELL =", 1)[1].split("];", 1)[0])
        self.assertNotIn("/media/", worker.split("const SHELL =", 1)[1].split("];", 1)[0])
        self.assertNotIn("cache.put(AUDIOBOOK_PAGE", worker)
        self.assertIn('id="gridDensityLabel"', page)

    def test_home_links_to_games_without_redundant_add_media_card(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn('href="/games"', html)
        self.assertNotIn('href="/photos?upload=1"', html)

    def test_home_groups_every_module_and_supports_recent_navigation(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('href="#moduleDirectory">Skip to apps</a>', html)
        self.assertIn('id="libraryTitle">Library</h3>', html)
        self.assertIn('id="togetherTitle">Together</h3>', html)
        self.assertIn('id="systemTitle">System</h3>', html)
        self.assertEqual(html.count(" data-module>"), 13)
        self.assertIn('/static/home-dashboard.css?v=3', html)
        self.assertIn('/static/home.js?v=6', html)
        self.assertIn('id="homeClearRecent"', html)
        self.assertIn('id="homeRecentStatus"', html)
        scope = html.split('data-recent-scope="', 1)[1].split('"', 1)[0]
        self.assertEqual(len(scope), 32)
        self.assertTrue(all(character in "0123456789abcdef" for character in scope))
        self.assertNotIn("david@example.test", scope)
        script = (Path(__file__).resolve().parents[1] / "static" / "home.js").read_text()
        self.assertIn("david-pi-recent-modules-v1", script)
        self.assertIn("david-pi-recent-modules-v2:", script)
        self.assertIn("localStorage.removeItem(LEGACY_RECENT_KEY)", script)
        self.assertIn("localStorage.removeItem(RECENT_KEY)", script)
        self.assertNotIn("localStorage.clear()", script)
        self.assertIn("data-dismiss-recent", script)
        self.assertIn("MAX_RECENTS = 3", script)
        self.assertIn("routeByPath.has(path)", script)

    def test_home_recent_scope_is_stable_normalized_and_principal_specific(self):
        david = portal.home_recent_storage_scope({"owner_id": " David@Example.Test "})
        self.assertEqual(
            david,
            portal.home_recent_storage_scope({"owner_id": "david@example.test"}),
        )
        self.assertNotEqual(
            david,
            portal.home_recent_storage_scope({"owner_id": "diana@example.test"}),
        )
        self.assertEqual(portal.home_recent_storage_scope({"owner_id": None}), "")

    def test_games_page_includes_all_local_games(self):
        response = self.client.get("/games")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Sudoku", html)
        self.assertIn("Klondike", html)
        self.assertIn("Memory", html)
        self.assertIn("Chess", html)
        self.assertIn("Checkers", html)
        self.assertIn('data-panel="chess"', html)
        self.assertIn('data-panel="checkers"', html)
        self.assertIn("Single player", html)
        self.assertIn('id="chessMode"', html)
        self.assertIn('id="checkersMode"', html)
        self.assertIn('id="solitaireDifficulty"', html)
        self.assertIn('id="memoryDifficulty"', html)
        self.assertIn('id="chessDifficulty"', html)
        self.assertIn('id="checkersDifficulty"', html)
        self.assertIn('data-open-scores', html)
        self.assertIn("David-Pi highscores", html)
        self.assertIn("Chess uses Elo", html)
        self.assertIn("Personal best", html)
        self.assertIn('id="chessDifficultyField"', html)
        self.assertIn('id="chessSideField"', html)
        self.assertIn('id="chessSide"', html)
        self.assertIn('<option value="alternate" selected>Alternate</option>', html)
        self.assertIn('id="checkersDifficultyField"', html)
        self.assertNotIn('id="scoreGame"', html)
        self.assertNotIn('id="scoreDifficulty"', html)
        self.assertIn("/static/chess-engine.js?v=1", html)
        self.assertIn("/static/games.js?v=40", html)
        self.assertGreater(len(self.client.get("/static/vendor/pdfjs/pdf.mjs").data), 100000)
        self.assertGreater(len(self.client.get("/static/vendor/pdfjs/pdf.worker.mjs").data), 100000)

    def test_completed_game_scores_are_ranked_by_difficulty_and_identity(self):
        incomplete = self.client.post("/api/games/scores", json={
            "game":"memory", "difficulty":"hard", "moves":20,
            "duration_ms":5000, "completed":False,
        })
        self.assertEqual(incomplete.status_code, 422)
        invalid = self.client.post("/api/games/scores", json={
            "game":"memory", "difficulty":"impossible", "moves":20,
            "duration_ms":5000, "completed":True,
        })
        self.assertEqual(invalid.status_code, 422)

        david_slow = self.client.post("/api/games/scores", json={
            "game":"memory", "difficulty":"hard", "moves":24,
            "duration_ms":80000, "completed":True,
        })
        david_best = self.client.post("/api/games/scores", json={
            "game":"memory", "difficulty":"hard", "moves":18,
            "duration_ms":70000, "completed":True,
        })
        self.assertEqual(david_slow.status_code, 201)
        self.assertEqual(david_best.status_code, 201)

        diana, token = self.paired_client("diana", "Diana")
        diana_score = diana.post("/api/games/scores", json={
            "game":"memory", "difficulty":"hard", "moves":16,
            "duration_ms":90000, "completed":True,
        }, headers={"X-CSRF-Token":token})
        self.assertEqual(diana_score.status_code, 201)

        board = self.client.get("/api/games/high-scores?game=memory&difficulty=hard")
        self.assertEqual(board.status_code, 200)
        payload = board.get_json()
        self.assertEqual([row["player"] for row in payload["household"]], ["Diana", "David"])
        self.assertEqual(payload["personal_best"]["moves"], 18)
        self.assertEqual(payload["completed_count"], 3)
        medium = self.client.get("/api/games/high-scores?game=memory&difficulty=medium").get_json()
        self.assertEqual(medium["household"], [])
        self.assertIsNone(medium["personal_best"])

    def test_game_overview_compares_difficulties_and_explains_rank(self):
        easy = self.client.post("/api/games/scores", json={
            "game":"memory", "difficulty":"easy", "moves":6,
            "duration_ms":60000, "completed":True,
        })
        self.assertEqual(easy.status_code, 201)
        diana, token = self.paired_client("diana", "Diana")
        hard = diana.post("/api/games/scores", json={
            "game":"memory", "difficulty":"hard", "moves":20,
            "duration_ms":120000, "completed":True,
        }, headers={"X-CSRF-Token":token})
        self.assertEqual(hard.status_code, 201)

        overview = self.client.get("/api/games/high-scores").get_json()
        self.assertEqual(overview["games"], ["sudoku", "solitaire", "memory", "chess", "checkers"])
        winner = overview["household"]["memory"][0]
        self.assertEqual(winner["player"], "David")
        self.assertEqual(winner["reason"], "6 moves · Easy · 1m 0s")
        self.assertGreater(winner["ranking_points"], overview["household"]["memory"][1]["ranking_points"])
        self.assertEqual(overview["personal"]["memory"]["difficulty"], "easy")

    def test_chess_elo_tracks_player_and_dynamic_opponents(self):
        initial = self.client.get("/api/games/chess-rating")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.get_json()["rating"], 1000)
        self.assertEqual(initial.get_json()["opponents"], {"easy":800, "medium":1000, "hard":1200})

        rated = self.client.post("/api/games/scores", json={
            "game":"chess", "difficulty":"hard", "moves":42,
            "duration_ms":300000, "completed":True, "mode":"solo", "result":"win",
        })
        self.assertEqual(rated.status_code, 201)
        score = rated.get_json()["score"]
        self.assertEqual(score["rating_before"], 1000)
        self.assertEqual(score["opponent_rating"], 1200)
        self.assertGreater(score["rating_after"], 1000)

        updated = self.client.get("/api/games/chess-rating").get_json()
        self.assertEqual(updated["rating"], score["rating_after"])
        self.assertEqual(updated["games_played"], 1)
        self.assertEqual(updated["wins"], 1)
        self.assertEqual(updated["opponents"]["medium"], score["rating_after"])

        unrated = self.client.post("/api/games/scores", json={
            "game":"chess", "difficulty":"medium", "moves":10,
            "duration_ms":1000, "completed":True, "mode":"two", "result":"win",
        })
        self.assertEqual(unrated.status_code, 422)
        self.assertEqual(self.client.get("/api/games/chess-rating").get_json()["games_played"], 1)

    def test_games_script_registers_all_five_completed_games_and_difficulties(self):
        script = self.client.get("/static/games.js").get_data(as_text=True)
        for game in ("sudoku", "solitaire", "memory", "chess", "checkers"):
            self.assertIn(f"registerCompletedGame('{game}'", script)
            self.assertIn(f"beginScoredGame('{game}')", script)
        self.assertIn("score_not_saved", script)
        self.assertIn("drawCount", script)
        self.assertIn("pairCount", script)
        self.assertIn("chess.difficulty", script)
        self.assertIn("checkers.difficulty", script)
        self.assertIn("syncBoardModeControls('chess')", script)
        self.assertIn("syncBoardModeControls('checkers')", script)
        self.assertIn("mode === 'two'", script)
        self.assertIn("Your Elo:", script)
        self.assertIn("ChessEngine.applyMove", script)
        self.assertIn("david-pi-chess-alternate-next", script)
        self.assertIn("chess.turn === chess.aiColor", script)
        self.assertIn("currentScores.household[game]", script)
        self.assertIn("${row.moves} moves · ${formatDifficulty(row.difficulty)}", script)

    def test_status_includes_shared_health_assessment(self):
        with patch.object(portal, "system_snapshot", return_value=SNAPSHOT):
            result = self.client.get("/api/status").get_json()
        self.assertEqual(result["health"]["overall"], "good")
        self.assertEqual(result["health"]["metrics"]["temperature"], "good")

    def test_system_snapshot_distinguishes_host_and_portal_uptime(self):
        status = {
            "subsystems": {
                "portal": {"details": {"cpu_percent": "2.5%", "uptime_seconds": 3600}},
                "storage": {"details": {"external": {}}},
                "temperature_power": {"details": {
                    "host_uptime_seconds": 90061, "load_average": [0.1, 0.2, 0.3],
                    "ram_total_gb": 4, "ram_available_gb": 3, "temperature_c": 51,
                }},
            }
        }
        with patch.object(portal, "server_status_snapshot", return_value=status):
            result = portal.system_snapshot()
        self.assertEqual(result["uptime"], 90061)
        self.assertEqual(result["host_uptime"], 90061)
        self.assertEqual(result["portal_uptime"], 3600)

    def test_system_snapshot_reports_missing_metrics_as_unknown(self):
        with patch.object(portal, "server_status_snapshot", return_value={"subsystems": {}}):
            result = portal.system_snapshot()

        for metric in (
            "cpu", "memory", "memory_used_gb", "memory_total_gb", "temperature",
            "disk_used", "disk_used_gb", "disk_total_gb", "disk_free_gb",
            "load1", "load5", "load15", "uptime", "host_uptime", "portal_uptime",
        ):
            self.assertIsNone(result[metric], metric)

    def test_readiness_is_read_only_bounded_and_skips_presence_writes(self):
        status_path = Path(TEST_DATA.name) / "ready-status.json"
        status_path.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state": "healthy",
            "subsystems": {},
            "databases": [],
            "privacy": {
                "contains_personal_filenames": False, "contains_domains": False,
                "contains_clients": False, "contains_secrets": False,
            },
        }), encoding="utf-8")
        before = portal.DB_PATH.stat().st_mtime_ns
        with patch.object(portal, "SERVER_STATUS", status_path), \
             patch.object(portal, "DATA_SENTINEL", DEVICE_SENTINEL):
            self.assertEqual(self.client.get("/health").status_code, 200)
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "reasons": []})
        self.assertEqual(portal.DB_PATH.stat().st_mtime_ns, before)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM portal_users").fetchone()[0], 0)

        with patch.object(portal, "SERVER_STATUS", status_path), \
             patch.object(portal, "DATA_SENTINEL", Path(TEST_DATA.name) / "missing"):
            unavailable = self.client.get("/ready")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.get_json()["reasons"], ["storage_identity"])
        self.assertNotIn(str(TEST_DATA.name), unavailable.get_data(as_text=True))

    def test_status_summary_is_sanitized_and_history_is_allowlisted(self):
        status_path = Path(TEST_DATA.name) / "server-status.json"
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-25T12:00:00+00:00",
            "state": "warning",
            "subsystems": {
                "portal": {
                    "state": "healthy", "summary": "Healthy", "updated_at": "2026-07-25T12:00:00+00:00",
                    "details": {}, "recommended_action": "", "evidence_code": "PORTAL_HEALTHY",
                },
                "access_control": {
                    "state": "warning", "summary": "Shadow mode", "updated_at": "2026-07-25T12:00:00+00:00",
                    "details": {
                        "configured_mode": "shadow", "effective_mode": "shadow",
                        "enforcement_active": False,
                    },
                    "recommended_action": "Complete the access canary.",
                    "evidence_code": "ACCESS_SHADOW_ACTIVE",
                },
            },
            "databases": [],
            "privacy": {
                "contains_personal_filenames": False, "contains_domains": False,
                "contains_clients": False, "contains_secrets": False,
            },
        }
        status_path.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(portal, "SERVER_STATUS", status_path):
            result = self.client.get("/api/status/summary")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()["ok"])
        self.assertEqual(
            result.get_json()["subsystems"]["access_control"]["evidence_code"],
            "ACCESS_SHADOW_ACTIVE",
        )
        self.assertEqual(result.get_json()["state"], "warning")
        self.assertEqual(self.client.get("/api/status/history?metric=secret&range=24h").status_code, 400)
        self.assertEqual(self.client.get("/api/status/history?metric=temperature&range=forever").status_code, 400)

    def test_status_page_has_health_cards_and_mobile_controls(self):
        html = self.client.get("/status").get_data(as_text=True)
        self.assertIn('id="healthCards"', html)
        self.assertIn('id="cpuMetric"', html)
        self.assertIn("Everything at a glance.", html)
        self.assertIn('id="historyMetric"', html)
        self.assertIn('id="historyRange"', html)
        self.assertIn("/static/status.js?v=13", html)
        self.assertIn("/static/status-enhancements.css?v=3", html)
        self.assertIn("Pi and portal", html)
        self.assertIn("Portal CPU", html)
        self.assertIn("Host memory", html)
        self.assertIn("Family storage", html)
        self.assertIn('id="databaseCount"', html)
        script = self.client.get("/static/status.js").get_data(as_text=True)
        self.assertIn("$('databaseCount').textContent", script)
        self.assertIn("Host up ${Math.floor(hostUptime / 86400)}d", script)
        self.assertIn('id="safeShutdownButton"', html)
        self.assertIn('id="safeShutdownDialog"', html)

    def test_safe_shutdown_requires_csrf_password_and_writes_fixed_request(self):
        request_path = Path(TEST_DATA.name) / "platform" / "control" / "shutdown.request"
        request_path.unlink(missing_ok=True)
        password_hash = generate_password_hash("shutdown")
        encoded = base64.b64encode(password_hash.encode("ascii")).decode("ascii")
        with patch.dict(os.environ, {portal.SHUTDOWN_PASSWORD_ENV: encoded}), \
             patch.object(portal, "SHUTDOWN_REQUEST", request_path):
            unprotected = portal.app.test_client()
            self.assertEqual(
                unprotected.post("/api/system/shutdown", json={"password": "shutdown"}).status_code,
                403,
            )
            denied = self.client.post("/api/system/shutdown", json={"password": "wrong"})
            self.assertEqual(denied.status_code, 401)
            self.assertFalse(request_path.exists())
            accepted = self.client.post("/api/system/shutdown", json={"password": "shutdown"})
            self.assertEqual(accepted.status_code, 202)
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["action"], "poweroff")
            self.assertEqual(payload["version"], 1)
            self.assertEqual(set(payload), {"action", "request_id", "requested_at", "version"})
            self.assertNotIn("shutdown", request_path.read_text(encoding="utf-8"))
        request_path.unlink(missing_ok=True)

    def test_safe_shutdown_is_unavailable_without_hash_and_get_does_not_mutate(self):
        request_path = Path(TEST_DATA.name) / "platform" / "control" / "shutdown.request"
        request_path.unlink(missing_ok=True)
        with patch.dict(os.environ, {portal.SHUTDOWN_PASSWORD_ENV: ""}), \
             patch.object(portal, "SHUTDOWN_REQUEST", request_path):
            self.assertEqual(self.client.get("/api/system/shutdown").status_code, 405)
            self.assertEqual(
                self.client.post("/api/system/shutdown", json={"password": "shutdown"}).status_code,
                503,
            )
            self.assertFalse(request_path.exists())

    def test_status_history_is_bounded_and_retained_for_thirty_days(self):
        now = 2_000_000_000
        with portal.metrics_db() as connection:
            connection.execute("DELETE FROM system_metrics")
            connection.execute(
                """INSERT INTO system_metrics
                   (timestamp,cpu,memory,temperature,disk_used,load1)
                   VALUES (?,?,?,?,?,?)""",
                (now - 31 * 86400, 1, 1, 40, 1, 0.1),
            )
        sample = dict(SNAPSHOT)
        sample["timestamp"] = now
        portal.write_metric_sample(sample, {}, now=now)
        with portal.metrics_db() as connection:
            old = connection.execute("SELECT COUNT(*) FROM system_metrics WHERE timestamp < ?", (now - 30 * 86400,)).fetchone()[0]
        self.assertEqual(old, 0)
        current = int(__import__("time").time())
        with portal.metrics_db() as connection:
            connection.executemany(
                """INSERT OR REPLACE INTO system_metrics
                   (timestamp,cpu,memory,temperature,disk_used,load1)
                   VALUES (?,?,?,?,?,?)""",
                [(current - 700 * 60 + index * 60, 1, 1, 40 + index % 3, 1, 0.1) for index in range(700)],
            )
        result = self.client.get("/api/status/history?metric=temperature&range=24h").get_json()
        self.assertLessEqual(len(result["points"]), 601)
        self.assertTrue(result["sampled"])

    def test_assistant_supported_and_unsupported_questions(self):
        self.add_photo("p1")
        self.add_photo("p2", "2026-01-02T00:00:00+00:00")
        self.add_collection("c1", "Vacation")
        photos = self.client.post("/api/assistant", json={"question": "How many photos do we have?"}).get_json()
        self.assertEqual(photos["intent"], "photos")
        self.assertIn("1 media item", photos["answer"])

        unsupported = self.client.post("/api/assistant", json={"question": "Run a shell command for me"}).get_json()
        self.assertEqual(unsupported["intent"], "refused")
        self.assertIn("cannot change", unsupported["answer"])

        providers = self.client.get("/api/assistant/providers").get_json()["providers"]
        self.assertEqual([provider["name"] for provider in providers], [
            "David-Pi Rules",
        ])
        self.assertTrue(providers[0]["available"])

        conversations = self.client.get("/api/assistant/conversations").get_json()["conversations"]
        self.assertGreaterEqual(len(conversations), 2)
        loaded = self.client.get(f"/api/assistant/conversations/{photos['conversation_id']}").get_json()
        self.assertEqual([item["role"] for item in loaded["messages"]], ["user", "assistant"])

    def test_assistant_health_and_aggregate_pihole(self):
        with patch.object(portal, "system_snapshot", return_value=SNAPSHOT):
            health = self.client.post("/api/assistant", json={"question": "Is the server healthy?"}).get_json()
        self.assertEqual(health["intent"], "health")
        self.assertIn("healthy ranges", health["answer"])

        totals = {"enabled": True, "total": 1000, "blocked": 125, "blocked_percent": 12.5, "clients": 4, "window": "Last 24 hours"}
        with patch.object(portal, "pihole_summary", return_value=totals):
            answer = self.client.post("/api/assistant", json={"question": "How much has Pi-hole blocked?"}).get_json()
        self.assertEqual(answer["intent"], "pihole")
        self.assertIn("125 of 1,000", answer["answer"])
        self.assertNotIn("domain", answer["answer"].lower())

        with patch.object(portal, "pihole_summary", return_value=totals):
            api_result = self.client.get("/api/pihole").get_json()
        self.assertEqual(set(api_result), {"ok", "enabled", "total", "blocked", "blocked_percent", "clients", "window"})

    def test_assistant_refuses_browsing_details(self):
        answer = self.client.post("/api/assistant", json={"question": "Which websites did Diana visit?"}).get_json()
        self.assertEqual(answer["intent"], "privacy")
        self.assertNotIn("diana", answer["answer"].lower())

    def test_assistant_never_contacts_deferred_external_provider(self):
        with patch("modules.assistant.WindowsCodexProvider.submit") as submit:
            remote = self.client.post("/api/assistant", json={"question":"What is five times five?"}).get_json()
            self.assertEqual(remote["intent"], "unsupported")
            deterministic = self.client.post("/api/assistant", json={"question":"How many photos do we have?"}).get_json()
            self.assertEqual(deterministic["intent"], "photos")
            submit.assert_not_called()

    def test_assistant_has_no_pi_generative_provider(self):
        providers = self.client.get("/api/assistant/providers").get_json()["providers"]
        names = {provider["name"] for provider in providers}
        self.assertIn("David-Pi Rules", names)
        self.assertNotIn("Local Pi", names)

    def test_assistant_requires_verified_allowlisted_identity_even_in_shadow_mode(self):
        anonymous = portal.app.test_client()
        self.assertEqual(anonymous.get("/assistant").status_code, 403)
        response = anonymous.get("/api/assistant/conversations")
        self.assertEqual(response.status_code, 403)
        self.assertIn("approved private Tailscale", response.get_json()["error"])

        unknown = portal.app.test_client()
        unknown.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "unknown@example.test"
        unknown.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Unknown"
        self.assertEqual(unknown.get("/assistant").status_code, 403)
        self.assertEqual(unknown.get("/api/assistant/providers").status_code, 403)
        self.assertEqual(self.client.get("/assistant").status_code, 200)

    def test_assistant_existing_conversation_rechecks_route_policy(self):
        created = self.client.post(
            "/api/assistant", json={"question": "How many photos do we have?"}
        ).get_json()
        calls = []
        original_authorize = assistant_module._authorize

        def track_authorization(route_id, actor, owner_id=None):
            calls.append((route_id, actor.principal_id, owner_id))
            return original_authorize(route_id, actor, owner_id)

        with patch.object(assistant_module, "_authorize", side_effect=track_authorization):
            response = self.client.post(
                "/api/assistant",
                json={
                    "question": "How many collections are there?",
                    "conversation_id": created["conversation_id"],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            (
                "assistant.prompt.create",
                "david@example.test",
                None,
            ),
            calls,
        )

    def test_assistant_discards_local_answer_if_conversation_version_changes(self):
        created=self.client.post("/api/assistant",json={"question":"How many photos?"}).get_json()
        original=assistant_module._store_message
        def concurrent_change(actor,conversation_id,role,content,provider,**kwargs):
            with assistant_module.connect(assistant_module.DB_PATH) as connection:
                connection.execute("UPDATE conversations SET version=version+1 WHERE id=?",(conversation_id,))
            return original(actor,conversation_id,role,content,provider,**kwargs)
        with patch.object(assistant_module,"_store_message",side_effect=concurrent_change):
            response=self.client.post("/api/assistant",json={"question":"How many photos?","conversation_id":created["conversation_id"]})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json["code"],"conversation_changed")

    def test_assistant_conversations_are_shared_with_household_attribution(self):
        david_result = self.client.post(
            "/api/assistant", json={"question": "How many photos do we have?"}
        ).get_json()
        conversation_id = david_result["conversation_id"]
        stale_version = david_result["conversation_version"]

        diana, diana_csrf = self.paired_client("diana")
        diana_list = diana.get("/api/assistant/conversations").get_json()["conversations"]
        shared = next(item for item in diana_list if item["id"] == conversation_id)
        self.assertEqual(shared["creator_name"], "David")
        self.assertNotIn("owner_id", shared)

        first_view = diana.get(
            f"/api/assistant/conversations/{conversation_id}"
        ).get_json()
        self.assertEqual(first_view["conversation"]["creator_name"], "David")
        self.assertEqual(first_view["messages"][0]["sender_name"], "David")
        self.assertFalse(first_view["messages"][0]["mine"])
        self.assertIsNone(first_view["messages"][1]["sender_name"])
        self.assertNotIn("sender_id", first_view["messages"][0])
        self.assertNotIn("sender_id", first_view["messages"][1])

        submitted_context = []

        def submit(messages, mode="general"):
            submitted_context.extend(messages)
            return {"answer": "Twenty-five.", "model": "test"}

        with patch("modules.assistant.WindowsCodexProvider.submit", side_effect=submit):
            continued = diana.post(
                "/api/assistant",
                json={
                    "question": "What is five times five?",
                    "conversation_id": conversation_id,
                },
                headers={"X-CSRF-Token": diana_csrf},
            )
        self.assertEqual(continued.status_code, 200)
        current_version = continued.get_json()["conversation_version"]
        self.assertGreater(current_version, stale_version)
        self.assertEqual(submitted_context, [])  # external providers are not called

        david_view = self.client.get(
            f"/api/assistant/conversations/{conversation_id}"
        ).get_json()
        user_messages = [
            item for item in david_view["messages"] if item["role"] == "user"
        ]
        self.assertEqual(
            [(item["sender_name"], item["mine"]) for item in user_messages],
            [("David", True), ("Diana", False)],
        )
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            owner_id = connection.execute(
                "SELECT owner_id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
            stored_senders = connection.execute(
                "SELECT role,sender_id FROM messages WHERE conversation_id=? "
                "ORDER BY created_at,id",
                (conversation_id,),
            ).fetchall()
            audit_actors = {
                row[0]
                for row in connection.execute(
                    "SELECT owner_id FROM assistant_audit "
                    "WHERE owner_id IN (?,?) ORDER BY created_at DESC LIMIT 20",
                    ("david@example.test", "diana@example.test"),
                ).fetchall()
            }
        self.assertEqual(owner_id, "david@example.test")
        self.assertEqual(
            [(row["role"], row["sender_id"]) for row in stored_senders],
            [
                ("user", "david@example.test"),
                ("assistant", None),
                ("user", "diana@example.test"),
                ("assistant", None),
            ],
        )
        self.assertTrue(
            {"david@example.test", "diana@example.test"}.issubset(audit_actors)
        )

        stale = diana.delete(
            f"/api/assistant/conversations/{conversation_id}",
            json={"version": stale_version},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(stale.status_code, 409)
        removed = diana.delete(
            f"/api/assistant/conversations/{conversation_id}",
            json={"version": current_version},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/assistant/conversations/{conversation_id}").status_code,
            404,
        )
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            deleted_by = connection.execute(
                "SELECT deleted_by FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
        self.assertEqual(deleted_by, "diana@example.test")

    def test_assistant_tasks_and_approvals_remain_actor_bound(self):
        now = "2026-01-01T00:00:00+00:00"
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            connection.executemany(
                "INSERT INTO remote_tasks"
                "(id,owner_id,provider,mode,state,task,repository_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        "task-david", "david@example.test", "test", "inspect", "ready",
                        "David task", None, now, now,
                    ),
                    (
                        "task-diana", "diana@example.test", "test", "inspect", "ready",
                        "Diana task", None, now, now,
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO approvals(id,task_id,owner_id,decision,created_at) "
                "VALUES (?,?,?,?,?)",
                [
                    ("approval-david", "task-david", "david@example.test", "allow", now),
                    ("approval-diana", "task-diana", "diana@example.test", "allow", now),
                ],
            )

        diana, _ = self.paired_client("diana")
        self.assertEqual(
            [item["id"] for item in self.client.get("/api/assistant/tasks").get_json()["tasks"]],
            ["task-david"],
        )
        self.assertEqual(
            [item["id"] for item in diana.get("/api/assistant/tasks").get_json()["tasks"]],
            ["task-diana"],
        )
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            approvals = connection.execute(
                "SELECT id,owner_id FROM approvals ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(row["id"], row["owner_id"]) for row in approvals],
            [
                ("approval-david", "david@example.test"),
                ("approval-diana", "diana@example.test"),
            ],
        )

    def test_assistant_api_keeps_principal_ids_as_private_provenance(self):
        client, token = self.allowlisted_client(
            "david@example.test", "david@example.test"
        )
        created = client.post(
            "/api/assistant",
            json={"question": "How many photos do we have?"},
            headers={"X-CSRF-Token": token},
        ).get_json()
        conversation_id = created["conversation_id"]
        listing = client.get("/api/assistant/conversations").get_json()
        detail = client.get(
            f"/api/assistant/conversations/{conversation_id}"
        ).get_json()
        public_payload = json.dumps({"listing": listing, "detail": detail})
        self.assertNotIn("david@example.test", public_payload)
        self.assertEqual(detail["conversation"]["creator_name"], "David")
        self.assertEqual(detail["messages"][0]["sender_name"], "David")
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            creator = connection.execute(
                "SELECT owner_id FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()[0]
            sender = connection.execute(
                "SELECT sender_id FROM messages WHERE conversation_id=? AND role='user'",
                (conversation_id,),
            ).fetchone()[0]
        self.assertEqual(creator, "david@example.test")
        self.assertEqual(sender, "david@example.test")

    def test_assistant_sender_migration_is_idempotent_and_preserves_legacy_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "assistant-legacy.db"
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            conversations = [
                (
                    f"legacy-{index}",
                    (
                        "unverified-home"
                        if index == 19
                        else (
                            "david@example.test"
                            if index % 2 == 0
                            else "diana@example.test"
                        )
                    ),
                    f"Conversation {index}",
                    f"2026-01-01T00:{index:02d}:00+00:00",
                    f"2026-01-01T00:{index:02d}:30+00:00",
                )
                for index in range(20)
            ]
            messages = [
                (
                    f"message-{index}",
                    f"legacy-{index % 20}",
                    ("user", "assistant", "system")[index % 3],
                    f"Original content {index}",
                    None if index % 3 != 1 else "Legacy provider",
                    f"2026-01-02T00:{index:02d}:00+00:00",
                )
                for index in range(56)
            ]
            connection.executemany(
                "INSERT INTO conversations(id,owner_id,title,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                conversations,
            )
            connection.executemany(
                "INSERT INTO messages(id,conversation_id,role,content,provider,created_at) "
                "VALUES (?,?,?,?,?,?)",
                messages,
            )
            connection.commit()

            assistant_module._migrate(connection)
            connection.commit()
            first_conversations = connection.execute(
                "SELECT id,owner_id,title,created_at,updated_at FROM conversations ORDER BY id"
            ).fetchall()
            first_messages = connection.execute(
                "SELECT id,conversation_id,role,content,provider,created_at,sender_id,sender_name "
                "FROM messages ORDER BY id"
            ).fetchall()
            assistant_module._migrate(connection)
            connection.commit()
            second_messages = connection.execute(
                "SELECT id,conversation_id,role,content,provider,created_at,sender_id,sender_name "
                "FROM messages ORDER BY id"
            ).fetchall()
            connection.close()

        expected_conversations = sorted(conversations)
        self.assertEqual([tuple(row) for row in first_conversations], expected_conversations)
        original_by_id = {row[0]: row for row in messages}
        owner_by_conversation = {row[0]: row[1] for row in conversations}
        self.assertEqual(len(first_messages), 56)
        self.assertEqual([tuple(row) for row in first_messages], [tuple(row) for row in second_messages])
        for row in first_messages:
            self.assertEqual(tuple(row[:6]), original_by_id[row["id"]])
            if row["role"] == "user":
                expected_owner = owner_by_conversation[row["conversation_id"]]
                self.assertEqual(row["sender_id"], expected_owner)
                expected_name = {
                    "david@example.test": "David",
                    "diana@example.test": "Diana",
                    "unverified-home": None,
                }[expected_owner]
                self.assertEqual(row["sender_name"], expected_name)
            else:
                self.assertIsNone(row["sender_id"])
                self.assertIsNone(row["sender_name"])

    def test_assistant_migration_never_commits_the_callers_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "assistant-rollback.db"
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE rollback_probe(id INTEGER PRIMARY KEY)")
            assistant_module._migrate(connection)
            self.assertTrue(connection.in_transaction)
            connection.rollback()
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
                ).fetchall()
            }
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            connection.close()

        self.assertNotIn("rollback_probe", names)
        self.assertNotIn("conversations", names)
        self.assertNotIn("messages", names)
        self.assertEqual(quick_check, "ok")

    def test_assistant_concurrent_legacy_migration_is_serialized_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "assistant-concurrent.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provider TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO conversations
                    (id,owner_id,title,created_at,updated_at)
                VALUES
                    ('legacy-shared','unverified-home','Keep title','created','updated');
                INSERT INTO messages
                    (id,conversation_id,role,content,provider,created_at)
                VALUES
                    ('legacy-message','legacy-shared','user','Keep content',NULL,'message-time');
                """
            )
            connection.close()

            first_schema = threading.Event()
            release_first = threading.Event()
            second_attempting = threading.Event()
            overlap = threading.Event()
            state_lock = threading.Lock()
            state = {"entries": 0, "active": 0, "maximum": 0}
            failures = []

            def synchronized_migration(worker_connection):
                with state_lock:
                    state["entries"] += 1
                    ordinal = state["entries"]
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                    if state["active"] > 1:
                        overlap.set()
                if ordinal == 1:
                    def trace(statement):
                        if (
                            "CREATE TABLE IF NOT EXISTS conversations" in statement
                            and not first_schema.is_set()
                        ):
                            first_schema.set()
                            release_first.wait(timeout=3)

                    worker_connection.set_trace_callback(trace)
                try:
                    assistant_module._migrate(worker_connection)
                finally:
                    worker_connection.set_trace_callback(None)
                    with state_lock:
                        state["active"] -= 1

            def run_worker(attempting=None):
                try:
                    if attempting is not None:
                        attempting.set()
                    assistant_module.migrate(database, synchronized_migration)
                except Exception as error:  # pragma: no cover - asserted below
                    failures.append(error)

            first_worker = threading.Thread(target=run_worker)
            first_worker.start()
            reached_schema = first_schema.wait(timeout=3)
            second_worker = threading.Thread(
                target=run_worker, args=(second_attempting,)
            )
            second_worker.start()
            attempted_while_paused = second_attempting.wait(timeout=3)
            overlapped_while_paused = overlap.wait(timeout=0.5)
            release_first.set()
            workers = (first_worker, second_worker)
            for worker in workers:
                worker.join(timeout=5)
            self.assertTrue(reached_schema)
            self.assertTrue(attempted_while_paused)
            self.assertFalse(overlapped_while_paused)
            self.assertFalse(any(worker.is_alive() for worker in workers))

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            conversation = connection.execute(
                "SELECT id,owner_id,title,created_at,updated_at,version "
                "FROM conversations"
            ).fetchone()
            message = connection.execute(
                "SELECT id,conversation_id,role,content,provider,created_at,"
                "sender_id,sender_name FROM messages"
            ).fetchone()
            conversation_columns = [
                row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
            ]
            message_columns = [
                row["name"] for row in connection.execute("PRAGMA table_info(messages)")
            ]
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            connection.close()

        self.assertEqual(failures, [])
        self.assertEqual(state["entries"], 2)
        self.assertEqual(state["maximum"], 1)
        self.assertEqual(
            tuple(conversation),
            ("legacy-shared", "unverified-home", "Keep title", "created", "updated", 1),
        )
        self.assertEqual(
            tuple(message),
            (
                "legacy-message", "legacy-shared", "user", "Keep content", None,
                "message-time", "unverified-home", None,
            ),
        )
        self.assertEqual(len(conversation_columns), len(set(conversation_columns)))
        self.assertEqual(len(message_columns), len(set(message_columns)))
        self.assertEqual(conversation_columns.count("version"), 1)
        self.assertEqual(message_columns.count("sender_id"), 1)
        self.assertEqual(message_columns.count("sender_name"), 1)
        self.assertEqual(quick_check, "ok")

    def test_assistant_delete_is_versioned_audited_and_soft_retained(self):
        created = self.client.post(
            "/api/assistant", json={"question": "How many photos do we have?"}
        ).get_json()
        conversation_id = created["conversation_id"]
        stale_version = created["conversation_version"]
        continued = self.client.post(
            "/api/assistant",
            json={
                "question": "How many collections are there?",
                "conversation_id": conversation_id,
            },
        ).get_json()
        current_version = continued["conversation_version"]
        self.assertGreater(current_version, stale_version)

        stale = self.client.delete(
            f"/api/assistant/conversations/{conversation_id}",
            json={"version": stale_version},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["latest_version"], current_version)
        self.assertEqual(
            self.client.delete(
                f"/api/assistant/conversations/{conversation_id}", json={}
            ).status_code,
            400,
        )

        removed = self.client.delete(
            f"/api/assistant/conversations/{conversation_id}",
            json={"version": current_version},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIn("retained", removed.get_json()["message"])
        self.assertIn("shared household history", removed.get_json()["message"])
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            message_count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
            audit = connection.execute(
                "SELECT actor_id,action,before_digest,after_digest FROM mutation_audit "
                "WHERE domain='assistant_conversation' AND object_id=? "
                "ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        self.assertEqual(row["deleted_by"], "david@example.test")
        self.assertIsNotNone(row["deleted_at"])
        self.assertIsNotNone(row["purge_after"])
        self.assertEqual(message_count, 4)
        self.assertEqual((audit["actor_id"], audit["action"]), ("david@example.test", "trash"))
        self.assertEqual(len(audit["before_digest"]), 64)
        self.assertEqual(len(audit["after_digest"]), 64)
        self.assertEqual(
            self.client.get(f"/api/assistant/conversations/{conversation_id}").status_code,
            404,
        )

    def test_assistant_limit_never_silently_deletes_saved_conversations(self):
        now = "2026-01-01T00:00:00+00:00"
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            connection.executemany(
                "INSERT INTO conversations"
                "(id,owner_id,title,created_at,updated_at,version) VALUES (?,?,?,?,?,1)",
                [
                    (f"limit-{index}", "david@example.test", f"Conversation {index}", now, now)
                    for index in range(assistant_module.MAX_CONVERSATIONS)
                ],
            )
        response = self.client.post(
            "/api/assistant", json={"question": "How many photos do we have?"}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "conversation_limit_reached")
        diana, diana_csrf = self.paired_client("diana")
        diana_response = diana.post(
            "/api/assistant",
            json={"question": "How many collections are there?"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(diana_response.status_code, 409)
        self.assertEqual(
            diana_response.get_json()["code"], "conversation_limit_reached"
        )
        with assistant_module.connect(assistant_module.DB_PATH) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE deleted_at IS NULL",
            ).fetchone()[0]
            deleted = connection.execute(
                "SELECT COUNT(*) FROM conversations WHERE deleted_at IS NOT NULL",
            ).fetchone()[0]
        self.assertEqual(count, assistant_module.MAX_CONVERSATIONS)
        self.assertEqual(deleted, 0)

    def test_security_headers_and_host_allowlist(self):
        response = self.client.get("/", base_url="https://localhost")
        self.assertEqual(response.status_code, 200)
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            self.client.get("/", headers={"Host": "attacker.invalid"}).status_code, 400
        )

    def test_media_mutations_require_per_browser_csrf(self):
        unprotected = portal.app.test_client()
        self.assertEqual(unprotected.post("/api/photos/restore-all").status_code, 403)
        token = "browser-specific-csrf-token-with-adequate-length"
        unprotected.set_cookie("david_pi_csrf", token, domain="localhost")
        self.assertEqual(
            unprotected.post(
                "/api/photos/restore-all", headers={"X-CSRF-Token": token}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.post("/api/photos/restore-all").status_code, 200)

    def test_active_media_cannot_be_permanently_purged(self):
        self.add_photo(
            "active", owner_id="david@example.test", owner_name="David",
        )
        response = self.client.post("/api/photos/purge", json={
            "items": self.photo_items("active"),
            "confirmation": "permanently-delete-media",
        })
        self.assertEqual(response.status_code, 409)
        with portal.db() as connection:
            self.assertIsNotNone(
                connection.execute("SELECT 1 FROM photos WHERE id='active'").fetchone()
            )

    def test_permanent_purge_is_disabled_without_all_retention_and_backup_gates(self):
        recent_deleted_at = datetime.now(timezone.utc).isoformat()
        for photo_id, deleted_at in (
            ("recent-delete", recent_deleted_at),
            ("old-delete", "2020-01-01T00:00:00+00:00"),
        ):
            self.add_photo(
                photo_id,
                deleted_at=deleted_at,
                owner_id="david@example.test",
                owner_name="David",
            )
            (portal.ORIGINALS / f"{photo_id}.jpg").write_bytes(
                f"retained:{photo_id}".encode()
            )
        missing_confirmation = self.client.post(
            "/api/photos/purge",
            json={"items": self.photo_items("recent-delete")},
        )
        self.assertEqual(missing_confirmation.status_code, 400)
        missing_all_confirmation = self.client.post("/api/photos/purge-all", json={})
        self.assertEqual(missing_all_confirmation.status_code, 400)

        for photo_id in ("recent-delete", "old-delete"):
            response = self.client.post(
                "/api/photos/purge",
                json={
                    "items": self.photo_items(photo_id),
                    "confirmation": "permanently-delete-media",
                },
            )
            self.assertEqual(response.status_code, 503)
            self.assertTrue(response.get_json()["retained"])
        purge_all = self.client.post(
            "/api/photos/purge-all",
            json={"confirmation": "empty-recently-deleted"},
        )
        self.assertEqual(purge_all.status_code, 503)
        self.assertTrue(purge_all.get_json()["retained"])

        with portal.db() as connection:
            rows = [
                tuple(row)
                for row in connection.execute(
                    """SELECT id,deleted_at,version FROM photos
                       WHERE id IN ('recent-delete','old-delete') ORDER BY id"""
                ).fetchall()
            ]
            audits = connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE object_id IN ('recent-delete','old-delete')"""
            ).fetchone()[0]
        self.assertEqual(
            rows,
            [
                ("old-delete", "2020-01-01T00:00:00+00:00", 1),
                ("recent-delete", recent_deleted_at, 1),
            ],
        )
        self.assertEqual(audits, 0)
        for photo_id in ("recent-delete", "old-delete"):
            self.assertEqual(
                (portal.ORIGINALS / f"{photo_id}.jpg").read_bytes(),
                f"retained:{photo_id}".encode(),
            )

    def test_deleted_media_requires_explicit_deleted_route(self):
        self.add_photo("deleted", "2026-01-02T00:00:00+00:00")
        self.assertEqual(self.client.get("/media/preview/deleted").status_code, 404)

    def test_managed_paths_reject_traversal_and_symlink_escape(self):
        with self.assertRaises(ValueError):
            portal.managed_path(portal.ORIGINALS, "../../etc/passwd")
        with self.assertRaises(ValueError):
            files_module.managed_object("../../etc/passwd")

    def test_uploaded_active_content_is_forced_to_download(self):
        client, csrf = self.paired_client()
        uploaded = client.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"<script>alert(1)</script>"), "page.html")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        file_id = uploaded.get_json()["added"] and client.get("/api/files").get_json()["files"][0]["id"]
        response = client.get(f"/api/files/{file_id}/content")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.mimetype, "application/octet-stream")

    def test_recipe_get_does_not_mutate_last_viewed(self):
        client, csrf = self.paired_client()
        recipe_id = client.post(
            "/api/recipes",
            json={"title": "Read only", "ingredients": ["Rice"], "instructions": ["Cook"]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]["id"]
        self.assertEqual(client.get(f"/api/recipes/{recipe_id}").status_code, 200)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            value = connection.execute(
                "SELECT last_viewed_at FROM recipes WHERE id=?", (recipe_id,)
            ).fetchone()[0]
        self.assertIsNone(value)

    def test_recipe_controls_expose_accessible_names_and_selected_state(self):
        client, _ = self.paired_client()
        page = client.get("/recipes")
        self.assertEqual(page.status_code, 200)
        markup = page.get_data(as_text=True)
        self.assertIn('id="recipeMeals" aria-label="Recipe sections"', markup)
        self.assertIn('data-library-meal="" aria-pressed="true"', markup)
        self.assertIn('id="closeRecipeView" type="button" aria-label="Close recipe"', markup)
        self.assertIn('aria-label="Search recipes or ingredients"', markup)
        script = (Path(__file__).resolve().parents[1] / "static" / "recipes.js").read_text(encoding="utf-8")
        self.assertIn("DavidPiFilterGroup.create('#recipeMeals'", script)
        self.assertIn("`Restore ${recipe.title}` : `Open ${recipe.title}`", script)

    def test_recipe_initialization_never_recategorizes_existing_rows(self):
        import sqlite3
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        recipes_module.initialize_recipes(connection)
        connection.execute(
            """INSERT INTO recipes
               (id, title, meal_type, content_hash, created_by, created_at, updated_at)
               VALUES ('legacy', 'Existing dinner', 'dinner', 'hash', 'home', 'then', 'then')"""
        )
        recipes_module.initialize_recipes(connection)
        self.assertEqual(
            connection.execute("SELECT meal_type FROM recipes WHERE id='legacy'").fetchone()[0],
            "dinner",
        )
        connection.close()

    def test_recipe_library_supports_bounded_summary_pages(self):
        client, csrf = self.paired_client()
        for index in range(61):
            response = client.post(
                "/api/recipes",
                json={
                    "title": f"Recipe {index:02d}",
                    "ingredients": [f"Ingredient {index}"],
                    "instructions": ["Cook"],
                    "total_minutes": 20 if index == 0 else None,
                },
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status_code, 201)
        first = client.get("/api/recipes?limit=60&offset=0").get_json()
        default_page = client.get("/api/recipes").get_json()
        self.assertEqual(len(default_page["recipes"]), 30)
        self.assertTrue(default_page["has_more"])
        self.assertEqual(client.get("/api/recipes?limit=all").status_code, 400)
        second = client.get("/api/recipes?limit=60&offset=60&summary=0").get_json()
        self.assertEqual(first["total"], 61)
        self.assertEqual(len(first["recipes"]), 60)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["facets"]["timed"], 1)
        self.assertEqual(first["facets"]["sections"]["main"], 61)
        self.assertEqual(first["facets"]["sections"]["other"], 0)
        self.assertNotIn("ingredients", first["recipes"][0])
        self.assertEqual(len(second["recipes"]), 1)
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["total"])
        self.assertIsNone(second["facets"])

    def test_recipe_edit_refreshes_duplicate_fingerprint(self):
        client, csrf = self.paired_client()
        first = client.post(
            "/api/recipes",
            json={"title": "First", "ingredients": ["Rice"], "instructions": ["Cook"]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        second = client.post(
            "/api/recipes",
            json={"title": "Second", "ingredients": ["Beans"], "instructions": ["Heat"]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        edited = client.put(
            f"/api/recipes/{first['id']}",
            json={"title": "Changed", "meal_type": "main", "ingredients": ["Rice"], "instructions": ["Cook"], "version": first["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(edited.status_code, 200)
        duplicate = client.put(
            f"/api/recipes/{second['id']}",
            json={"title": "Changed", "meal_type": "main", "ingredients": ["Rice"], "instructions": ["Cook"], "version": second["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertTrue(duplicate.get_json()["duplicate"])

    def test_remote_svg_is_not_relayed(self):
        client, csrf = self.paired_client()
        recipe = client.post(
            "/api/recipes",
            json={
                "title": "SVG Recipe", "ingredients": ["Rice"],
                "instructions": ["Cook"], "image_url": "https://images.example.com/a.svg",
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        with patch.object(
            recipes_module,
            "fetch_public",
            return_value=(b"<svg><script/></svg>", "image/svg+xml", "https://images.example.com/a.svg"),
        ):
            self.assertEqual(client.get(recipe["image"]).status_code, 404)

    def test_membership_state_is_read_only_and_tri_state(self):
        self.add_photo("p1", owner_id="david@example.test", owner_name="David")
        self.add_photo("p2", owner_id="david@example.test", owner_name="David")
        self.add_collection("c1", "Mixed", owner_id="david@example.test", owner_name="David")
        self.add_collection("c2", "Empty", owner_id="david@example.test", owner_name="David")
        self.add_membership("c1", "p1", "david@example.test")
        before = self.memberships()
        response = self.client.post("/api/collections/membership-state", json={"ids": ["p1", "p2"]})
        self.assertEqual(response.status_code, 200)
        states = {item["id"]: item["state"] for item in response.get_json()["collections"]}
        self.assertEqual(states, {"c2": "none", "c1": "mixed"})
        self.assertEqual(self.memberships(), before)

    def test_creating_collection_does_not_assign_photos(self):
        self.add_photo("p1")
        response = self.client.post("/api/collections", json={"name": "New collection"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.memberships(), set())

    def test_private_media_and_collection_are_visible_only_to_owner(self):
        self.add_photo(
            "private-photo", owner_id="david@example.test",
            owner_name="David", visibility="private",
        )
        self.add_photo("shared-photo")
        self.add_collection(
            "private-collection", "David only", owner_id="david@example.test",
            owner_name="David", visibility="private",
        )
        self.add_membership("private-collection", "shared-photo", "david@example.test")
        david, _ = self.paired_client("david")
        diana, _ = self.paired_client("diana")
        self.assertEqual(
            {item["id"] for item in david.get("/api/photos").get_json()["photos"]},
            {"shared-photo"},
        )
        self.assertEqual(
            {item["id"] for item in david.get("/api/photos?view=mine").get_json()["photos"]},
            {"private-photo"},
        )
        self.assertEqual(
            {item["id"] for item in diana.get("/api/photos").get_json()["photos"]},
            {"shared-photo"},
        )
        self.assertEqual(
            david.get("/api/collections").get_json()["collections"],
            [],
        )
        self.assertEqual(
            [item["id"] for item in david.get("/api/collections?view=mine").get_json()["collections"]],
            ["private-collection"],
        )
        self.assertEqual(diana.get("/api/collections").get_json()["collections"], [])
        self.assertEqual(
            diana.get("/api/photos?collection=private-collection").status_code, 404
        )

    def test_collection_privacy_does_not_silently_change_member_privacy(self):
        self.add_photo(
            "shared-photo", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        self.add_collection(
            "shared-collection", "Shared", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        self.add_membership("shared-collection", "shared-photo", "david@example.test")
        response = self.client.patch(
            "/api/collections/shared-collection",
            json={"name": "Private collection", "visibility": "private", "version": 1},
        )
        self.assertEqual(response.status_code, 200)
        with portal.db() as connection:
            photo = connection.execute(
                "SELECT visibility FROM photos WHERE id='shared-photo'"
            ).fetchone()
            collection = connection.execute(
                "SELECT visibility FROM collections WHERE id='shared-collection'"
            ).fetchone()
        self.assertEqual(photo["visibility"], "shared")
        self.assertEqual(collection["visibility"], "private")

    def test_shared_media_destructive_actions_are_owner_only(self):
        self.add_photo(
            "david-shared-photo", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        diana, diana_csrf = self.paired_client("diana")
        denied_trash = diana.post(
            "/api/photos/trash", json={"items": self.photo_items("david-shared-photo")},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(denied_trash.status_code, 403)
        with portal.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT deleted_at FROM photos WHERE id='david-shared-photo'"
            ).fetchone()["deleted_at"])

        self.assertEqual(self.client.post(
            "/api/photos/trash", json={"items": self.photo_items("david-shared-photo")}
        ).status_code, 200)
        denied_purge = diana.post(
            "/api/photos/purge", json={
                "items": self.photo_items("david-shared-photo"),
                "confirmation": "permanently-delete-media",
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(denied_purge.status_code, 403)
        with portal.db() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT id FROM photos WHERE id='david-shared-photo'"
            ).fetchone())

    def test_all_bulk_and_single_media_destruction_stays_owner_scoped(self):
        deleted_at = "2026-01-02T00:00:00+00:00"
        self.add_photo(
            "david-active", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        self.add_photo(
            "david-deleted", deleted_at=deleted_at,
            owner_id="david@example.test", owner_name="David", visibility="shared",
        )
        self.add_photo(
            "diana-deleted", deleted_at=deleted_at,
            owner_id="diana@example.test", owner_name="Diana", visibility="shared",
        )
        diana, diana_csrf = self.paired_client("diana")

        self.assertEqual(
            diana.delete(
                "/api/photos/david-active", json={"version": 1},
                headers={"X-CSRF-Token": diana_csrf}
            ).status_code,
            403,
        )
        self.assertEqual(
            diana.post(
                "/api/photos/purge", json={
                    "items": self.photo_items("david-deleted"),
                    "confirmation": "permanently-delete-media",
                },
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        purged = diana.post(
            "/api/photos/purge-all",
            json={"confirmation": "empty-recently-deleted"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(purged.status_code, 503)
        self.assertTrue(purged.get_json()["retained"])
        self.assertEqual(purged.get_json()["count"], 0)
        with portal.db() as connection:
            remaining = {
                row[0] for row in connection.execute(
                    "SELECT id FROM photos ORDER BY id"
                ).fetchall()
            }
        self.assertEqual(remaining, {"david-active", "david-deleted", "diana-deleted"})

    def test_shared_restore_is_owner_only(self):
        self.add_photo(
            "david-deleted", deleted_at="2026-01-02T00:00:00+00:00",
            owner_id="david@example.test", owner_name="David", visibility="shared",
        )
        diana, diana_csrf = self.paired_client("diana")
        restored = diana.post(
            "/api/photos/restore", json={"items": self.photo_items("david-deleted")},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(restored.status_code, 403)
        with portal.db() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT deleted_at FROM photos WHERE id='david-deleted'"
            ).fetchone()["deleted_at"])

    def test_restore_bulk_and_restore_all_isolate_owners_without_touching_files(self):
        for photo_id, owner, name in (
            ("david-deleted-one", "david@example.test", "David"),
            ("david-deleted-two", "david@example.test", "David"),
            ("diana-deleted", "diana@example.test", "Diana"),
        ):
            self.add_photo(
                photo_id,
                deleted_at="2026-01-02T00:00:00+00:00",
                owner_id=owner,
                owner_name=name,
                visibility="shared",
            )
            (portal.ORIGINALS / f"{photo_id}.jpg").write_bytes(
                f"bytes:{photo_id}".encode()
            )
        diana, diana_csrf = self.paired_client("diana")
        headers = {"X-CSRF-Token": diana_csrf}

        single = diana.post(
            "/api/photos/restore",
            json={"items": [{"id": "david-deleted-one", "version": 1}]},
            headers=headers,
        )
        self.assertEqual(single.status_code, 403)
        mixed = diana.post(
            "/api/photos/restore",
            json={
                "items": [
                    {"id": "diana-deleted", "version": 1},
                    {"id": "david-deleted-two", "version": 1},
                ]
            },
            headers=headers,
        )
        self.assertEqual(mixed.status_code, 403)

        restored = diana.post("/api/photos/restore-all", headers=headers)
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.get_json()["count"], 1)
        with portal.db() as connection:
            rows = {
                row["id"]: row
                for row in connection.execute(
                    """SELECT id,deleted_at,version FROM photos
                       WHERE id IN ('david-deleted-one','david-deleted-two','diana-deleted')"""
                ).fetchall()
            }
            audits = [
                tuple(row)
                for row in connection.execute(
                    """SELECT object_id,action FROM mutation_audit
                       WHERE object_id IN
                         ('david-deleted-one','david-deleted-two','diana-deleted')
                       ORDER BY id"""
                ).fetchall()
            ]
            outbox = [
                tuple(row)
                for row in connection.execute(
                    """SELECT object_id,event_type,object_version FROM domain_outbox
                       WHERE object_id IN
                         ('david-deleted-one','david-deleted-two','diana-deleted')
                       ORDER BY id"""
                ).fetchall()
            ]
        self.assertIsNotNone(rows["david-deleted-one"]["deleted_at"])
        self.assertIsNotNone(rows["david-deleted-two"]["deleted_at"])
        self.assertEqual(rows["david-deleted-one"]["version"], 1)
        self.assertEqual(rows["david-deleted-two"]["version"], 1)
        self.assertIsNone(rows["diana-deleted"]["deleted_at"])
        self.assertEqual(rows["diana-deleted"]["version"], 2)
        self.assertEqual(audits, [("diana-deleted", "restore")])
        self.assertEqual(outbox, [("diana-deleted", "restore", 2)])
        for photo_id in rows:
            self.assertEqual(
                (portal.ORIGINALS / f"{photo_id}.jpg").read_bytes(),
                f"bytes:{photo_id}".encode(),
            )

    def test_shared_media_cannot_be_made_private_by_another_household_member(self):
        self.add_photo(
            "david-shared", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        diana, diana_csrf = self.paired_client("diana")
        denied = diana.patch(
            "/api/photos/visibility",
            json={"items": self.photo_items("david-shared"), "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(denied.status_code, 403)
        with portal.db() as connection:
            row = connection.execute(
                "SELECT owner_id,visibility FROM photos WHERE id='david-shared'"
            ).fetchone()
        self.assertEqual((row["owner_id"], row["visibility"]), ("david@example.test", "shared"))

    def test_shared_collection_changes_are_owner_only_but_organization_remains_shared(self):
        self.add_photo(
            "diana-shared-photo", owner_id="diana@example.test",
            owner_name="Diana", visibility="shared",
        )
        self.add_collection(
            "david-shared-collection", "David album", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        diana, diana_csrf = self.paired_client("diana")
        renamed = diana.patch(
            "/api/collections/david-shared-collection",
            json={"name": "Changed by Diana", "visibility": "shared", "version": 1},
            headers={"X-CSRF-Token": diana_csrf},
        )
        deleted = diana.delete(
            "/api/collections/david-shared-collection",
            json={"version": 1},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(renamed.status_code, 403)
        self.assertEqual(deleted.status_code, 403)

        organized = diana.post(
            "/api/collections/membership",
            json={
                "items": self.photo_items("diana-shared-photo"),
                "changes": [{
                    "collection_id": "david-shared-collection", "action": "add",
                    "collection_version": 1,
                }],
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(organized.status_code, 200)
        self.assertIn(
            ("david-shared-collection", "diana-shared-photo"), self.memberships()
        )
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT name FROM collections WHERE id='david-shared-collection'"
            ).fetchone()["name"], "David album")

    def test_legacy_shared_media_is_readable_neutral_and_immutable(self):
        self.add_photo("legacy-photo")
        self.add_collection("legacy-collection", "Old album")
        self.add_membership("legacy-collection", "legacy-photo")

        media = self.client.get("/api/photos").get_json()["photos"][0]
        self.assertEqual(media["ownership_status"], "legacy_unclaimed")
        self.assertEqual(media["owner_display"], "Legacy (unclaimed)")
        self.assertFalse(media["can_edit"])
        self.assertNotIn("owner_id", media)
        self.assertNotIn("owner_name", media)
        collection = self.client.get("/api/collections").get_json()["collections"][0]
        self.assertEqual(collection["ownership_status"], "legacy_unclaimed")
        self.assertEqual(collection["owner_display"], "Legacy (unclaimed)")
        self.assertFalse(collection["can_edit"])
        self.assertNotIn("owner_id", collection)

        self.assertEqual(self.client.patch(
            "/api/photos/visibility",
            json={"items": [{"id": "legacy-photo", "version": 1}], "visibility": "private"},
        ).status_code, 403)
        self.assertEqual(self.client.delete(
            "/api/photos/legacy-photo", json={"version": 1},
        ).status_code, 403)
        self.assertEqual(self.client.patch(
            "/api/collections/legacy-collection",
            json={"name": "Claimed", "visibility": "private", "version": 1},
        ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/collections/membership",
            json={
                "items": [{"id": "legacy-photo", "version": 1}],
                "changes": [{
                    "collection_id": "legacy-collection", "action": "remove",
                    "collection_version": 1,
                }],
            },
        ).status_code, 403)
        self.assertEqual(self.memberships(), {("legacy-collection", "legacy-photo")})

    def test_diana_keeps_full_control_of_her_media_without_admin_override(self):
        self.add_photo(
            "diana-photo", owner_id="diana@example.test", owner_name="Diana",
        )
        diana, diana_csrf = self.paired_client("diana")
        changed = diana.patch(
            "/api/photos/visibility",
            json={"items": [{"id": "diana-photo", "version": 1}], "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.get_json()["items"], [{"id": "diana-photo", "version": 2}])
        self.assertEqual(self.client.delete(
            "/api/photos/diana-photo", json={"version": 2},
        ).status_code, 404)
        trashed = diana.delete(
            "/api/photos/diana-photo", json={"version": 2},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        restored = diana.post(
            "/api/photos/restore",
            json={"items": [{"id": "diana-photo", "version": 3}]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(restored.status_code, 200)
        with portal.db() as connection:
            row = connection.execute(
                "SELECT owner_id,visibility,deleted_at,version FROM photos WHERE id='diana-photo'"
            ).fetchone()
        self.assertEqual(
            (row["owner_id"], row["visibility"], row["deleted_at"], row["version"]),
            ("diana@example.test", "private", None, 4),
        )

    def test_media_stale_and_mixed_owner_batches_are_atomic(self):
        self.add_photo("david-one", owner_id="david@example.test", owner_name="David")
        self.add_photo("david-two", owner_id="david@example.test", owner_name="David")
        self.add_photo("diana-one", owner_id="diana@example.test", owner_name="Diana")
        with portal.db() as connection:
            connection.execute("UPDATE photos SET version=2 WHERE id='david-two'")

        stale = self.client.patch("/api/photos/visibility", json={
            "items": [
                {"id": "david-one", "version": 1},
                {"id": "david-two", "version": 1},
            ],
            "visibility": "private",
        })
        self.assertEqual(stale.status_code, 409)
        mixed = self.client.post("/api/photos/trash", json={
            "items": [
                {"id": "david-one", "version": 1},
                {"id": "diana-one", "version": 1},
            ],
        })
        self.assertEqual(mixed.status_code, 403)
        with portal.db() as connection:
            rows = connection.execute(
                "SELECT id,visibility,deleted_at,version FROM photos ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [(row["id"], row["visibility"], row["deleted_at"], row["version"]) for row in rows],
            [
                ("david-one", "shared", None, 1),
                ("david-two", "shared", None, 2),
                ("diana-one", "shared", None, 1),
            ],
        )

    def test_media_audit_is_atomic_and_records_only_success(self):
        self.add_photo("audited", owner_id="david@example.test", owner_name="David")
        self.add_photo("foreign", owner_id="diana@example.test", owner_name="Diana")
        denied = self.client.post("/api/photos/trash", json={
            "items": [{"id": "foreign", "version": 1}],
        })
        self.assertEqual(denied.status_code, 403)
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='media' AND object_id='foreign'"
            ).fetchone()[0], 0)

        with patch.object(portal, "audit_mutation", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
                self.client.post("/api/photos/trash", json={
                    "items": [{"id": "audited", "version": 1}],
                })
        with portal.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT deleted_at FROM photos WHERE id='audited'"
            ).fetchone()["deleted_at"])
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='media' AND object_id='audited'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM domain_outbox WHERE domain='media' AND object_id='audited'"
            ).fetchone()[0], 0)

        completed = self.client.post("/api/photos/trash", json={
            "items": [{"id": "audited", "version": 1}],
        })
        self.assertEqual(completed.status_code, 200)
        with portal.db() as connection:
            events = connection.execute(
                """SELECT action,actor_id FROM mutation_audit
                   WHERE domain='media' AND object_id='audited' ORDER BY id"""
            ).fetchall()
            outbox = connection.execute(
                """SELECT event_type,object_version FROM domain_outbox
                   WHERE domain='media' AND object_id='audited' ORDER BY id"""
            ).fetchall()
        self.assertEqual([tuple(row) for row in events], [("trash", "david@example.test")])
        self.assertEqual([tuple(row) for row in outbox], [("trash", 2)])

    def test_collection_soft_delete_retains_photos_and_memberships(self):
        self.add_photo("kept-photo", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "kept-collection", "Keep contents", owner_id="david@example.test",
            owner_name="David",
        )
        self.add_membership("kept-collection", "kept-photo", "david@example.test")
        deleted = self.client.delete(
            "/api/collections/kept-collection", json={"version": 1},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["retained"])
        self.assertEqual(self.client.get("/api/collections").get_json()["collections"], [])
        with portal.db() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT deleted_at FROM collections WHERE id='kept-collection'"
            ).fetchone()["deleted_at"])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM photos WHERE id='kept-photo'"
            ).fetchone())
            self.assertIsNotNone(connection.execute(
                """SELECT 1 FROM collection_photos
                   WHERE collection_id='kept-collection' AND photo_id='kept-photo'"""
            ).fetchone())
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        """SELECT action,actor_id FROM mutation_audit
                           WHERE domain='media_collection'
                             AND object_id='kept-collection' ORDER BY id"""
                    ).fetchall()
                ],
                [("trash", "david@example.test")],
            )
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        """SELECT event_type,object_version FROM domain_outbox
                           WHERE domain='media_collection'
                             AND object_id='kept-collection' ORDER BY id"""
                    ).fetchall()
                ],
                [("trash", 2)],
            )

    def test_unknown_tailscale_identity_cannot_read_or_create_media(self):
        outsider, outsider_csrf = self.allowlisted_client(
            "outsider@example.test", "Outsider"
        )
        self.assertEqual(outsider.get("/api/photos").status_code, 403)
        self.assertEqual(outsider.post(
            "/api/collections", json={"name": "Unauthorized"},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code, 403)

    def test_upload_into_private_collection_inherits_privacy_and_membership(self):
        self.add_collection(
            "private-collection", "Private", owner_id="david@example.test",
            owner_name="David", visibility="private",
        )
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(image, "PNG")
        image.seek(0)
        response = self.client.post(
            "/api/upload",
            data={
                "collection_id": "private-collection",
                "visibility": "shared",
                "media": (image, "tiny.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        photo_id = response.get_json()["added_items"][0]["id"]
        with portal.db() as connection:
            photo = connection.execute(
                "SELECT visibility, owner_id FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
        self.assertEqual((photo["visibility"], photo["owner_id"]), ("private", "david@example.test"))
        self.assertIn(("private-collection", photo_id), self.memberships())
        diana, _ = self.paired_client("diana")
        self.assertEqual(diana.get("/api/photos").get_json()["photos"], [])

    def test_private_collection_rejects_foreign_media_without_mutation(self):
        self.add_photo(
            "diana-shared",
            owner_id="diana@example.test",
            owner_name="Diana",
            visibility="shared",
        )
        self.add_collection(
            "david-private",
            "David private",
            owner_id="david@example.test",
            owner_name="David",
            visibility="private",
        )
        response = self.client.post(
            "/api/collections/membership",
            json={
                "items": [{"id": "diana-shared", "version": 1}],
                "changes": [{
                    "collection_id": "david-private",
                    "action": "add",
                    "collection_version": 1,
                }],
            },
        )
        self.assertEqual(response.status_code, 403)
        with portal.db() as connection:
            self.assertIsNone(connection.execute(
                """SELECT 1 FROM collection_photos
                   WHERE collection_id='david-private' AND photo_id='diana-shared'"""
            ).fetchone())
            self.assertEqual(connection.execute(
                "SELECT version FROM collections WHERE id='david-private'"
            ).fetchone()["version"], 1)
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE object_id='david-private'
                     AND action='membership_update'"""
            ).fetchone()[0], 0)

    def test_private_collection_direct_views_are_consistent_and_name_stays_private(self):
        self.add_photo(
            "legacy-foreign-member",
            owner_id="diana@example.test",
            owner_name="Diana",
            visibility="shared",
        )
        self.add_collection(
            "private-with-legacy-member",
            "Private legacy membership",
            owner_id="david@example.test",
            owner_name="David",
            visibility="private",
        )
        self.add_membership(
            "private-with-legacy-member", "legacy-foreign-member", "david@example.test"
        )
        default = self.client.get(
            "/api/photos?collection=private-with-legacy-member"
        )
        mine = self.client.get(
            "/api/photos?view=mine&collection=private-with-legacy-member"
        )
        self.assertEqual(default.status_code, 200)
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(
            [item["id"] for item in default.get_json()["photos"]],
            ["legacy-foreign-member"],
        )
        self.assertEqual(default.get_json()["photos"], mine.get_json()["photos"])
        diana, _ = self.paired_client("diana")
        self.assertEqual(
            diana.get(
                "/api/photos?collection=private-with-legacy-member"
            ).status_code,
            404,
        )

    def test_upload_binds_initial_collection_visibility_and_version(self):
        self.add_collection(
            "upload-race",
            "Upload race",
            owner_id="david@example.test",
            owner_name="David",
            visibility="shared",
        )
        image = BytesIO()
        portal.Image.new("RGB", (8, 8), "orange").save(image, "PNG")
        image.seek(0)
        original_prepare = portal._prepare_media_intent
        changed = False

        def change_then_prepare(*args, **kwargs):
            nonlocal changed
            if not changed:
                with portal.db() as connection:
                    connection.execute(
                        """UPDATE collections SET visibility='private',version=version+1
                           WHERE id='upload-race'"""
                    )
                changed = True
            return original_prepare(*args, **kwargs)

        with patch.object(
            portal, "_prepare_media_intent", side_effect=change_then_prepare
        ):
            response = self.client.post(
                "/api/upload",
                data={
                    "collection_id": "upload-race",
                    "visibility": "shared",
                    "media": (image, "race.png"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added_items"], [])
        self.assertEqual(len(response.get_json()["errors"]), 1)
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM photos"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM media_publish_intents"
            ).fetchone()[0], 0)
            collection = connection.execute(
                "SELECT visibility,version FROM collections WHERE id='upload-race'"
            ).fetchone()
        self.assertEqual(tuple(collection), ("private", 2))

    def test_final_ingest_rechecks_collection_snapshot_and_retains_intent(self):
        self.add_collection(
            "finalize-race",
            "Finalize race",
            owner_id="david@example.test",
            owner_name="David",
            visibility="shared",
        )
        staged = Path(TEST_DATA.name) / "finalize-race.png"
        portal.Image.new("RGB", (9, 9), "teal").save(staged, "PNG")
        original_recover = portal._recover_media_intent
        changed = False

        def change_then_recover(*args, **kwargs):
            nonlocal changed
            if not changed:
                with portal.db() as connection:
                    connection.execute(
                        """UPDATE collections SET visibility='private',version=version+1
                           WHERE id='finalize-race'"""
                    )
                changed = True
            return original_recover(*args, **kwargs)

        with patch.object(
            portal, "_recover_media_intent", side_effect=change_then_recover
        ):
            with self.assertRaises(portal.MediaIntentDeferred):
                portal.canonical_ingest_media(
                    staged_path=staged,
                    original_filename="finalize-race.png",
                    mime_type="image/png",
                    owner_user_id="david@example.test",
                    owner_name="David",
                    collection_id="finalize-race",
                    expected_collection_owner_id="david@example.test",
                    expected_collection_visibility="shared",
                    expected_collection_version=1,
                )
        with portal.db() as connection:
            intent = connection.execute(
                "SELECT * FROM media_publish_intents WHERE state='prepared'"
            ).fetchone()
            self.assertIsNotNone(intent)
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
            ).fetchone())
            self.assertIsNone(connection.execute(
                """SELECT 1 FROM collection_photos
                   WHERE collection_id='finalize-race'"""
            ).fetchone())
        self.assertTrue((portal.ORIGINALS / intent["stored_path"]).is_file())
        self.assertTrue(staged.is_file())

    def test_membership_changes_apply_together(self):
        self.add_photo("p1", owner_id="david@example.test", owner_name="David")
        self.add_photo("p2", owner_id="david@example.test", owner_name="David")
        self.add_collection("c1", "Mixed", owner_id="david@example.test", owner_name="David")
        self.add_collection("c2", "Full", owner_id="david@example.test", owner_name="David")
        self.add_membership("c1", "p1", "david@example.test")
        self.add_membership("c2", "p1", "david@example.test")
        self.add_membership("c2", "p2", "david@example.test")
        response = self.client.post("/api/collections/membership", json={
            "items": self.photo_items("p1", "p2"),
            "changes": [
                {"collection_id": "c1", "action": "add", "collection_version": 1},
                {"collection_id": "c2", "action": "remove", "collection_version": 1},
            ],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added"], 1)
        self.assertEqual(response.get_json()["removed"], 2)
        self.assertEqual(self.memberships(), {("c1", "p1"), ("c1", "p2")})

    def test_invalid_membership_batch_rolls_back(self):
        self.add_photo("p1", owner_id="david@example.test", owner_name="David")
        self.add_collection("c1", "Keep", owner_id="david@example.test", owner_name="David")
        before = self.memberships()
        response = self.client.post("/api/collections/membership", json={
            "items": self.photo_items("p1"),
            "changes": [
                {"collection_id": "c1", "action": "add", "collection_version": 1},
                {"collection_id": "missing", "action": "add", "collection_version": 1},
            ],
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.memberships(), before)

    def test_restore_all_only_restores_deleted_photos(self):
        self.add_photo("active", owner_id="david@example.test", owner_name="David")
        self.add_photo("deleted-1", "2026-01-02T00:00:00+00:00", owner_id="david@example.test", owner_name="David")
        self.add_photo("deleted-2", "2026-01-03T00:00:00+00:00", owner_id="david@example.test", owner_name="David")
        response = self.client.post("/api/photos/restore-all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 2)
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM photos WHERE deleted_at IS NOT NULL"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 3)

    def test_purge_all_requires_explicit_confirmation_and_only_purges_deleted(self):
        self.add_photo(
            "active", owner_id="david@example.test", owner_name="David",
        )
        self.add_photo(
            "deleted", "2026-01-02T00:00:00+00:00",
            owner_id="david@example.test", owner_name="David",
        )
        denied = self.client.post("/api/photos/purge-all", json={})
        self.assertEqual(denied.status_code, 400)
        with portal.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)

        retained = self.client.post(
            "/api/photos/purge-all", json={"confirmation": "empty-recently-deleted"}
        )
        self.assertEqual(retained.status_code, 503)
        self.assertTrue(retained.get_json()["retained"])
        self.assertEqual(retained.get_json()["count"], 0)
        with portal.db() as connection:
            rows = connection.execute("SELECT id FROM photos").fetchall()
        self.assertEqual({row[0] for row in rows}, {"active", "deleted"})

    def test_system_collection_controls_render(self):
        page = self.client.get("/photos").data
        for control in (
            b"allPhotoActions", b"addFromAll", b"selectFromAll",
            b"deletedCollectionActions", b"selectDeleted",
            b"restoreAllDeleted", b"emptyDeleted",
        ):
            self.assertIn(control, page)
        self.assertIn(b"openSlideshow", page)
        self.assertIn(b"slideshowTransition", page)

    def test_slideshow_job_uses_collection_photos_and_explicit_settings(self):
        self.add_photo("p1", owner_id="david@example.test", owner_name="David")
        self.add_photo("p2", owner_id="david@example.test", owner_name="David")
        self.add_collection("c1", "Vacation", owner_id="david@example.test", owner_name="David")
        self.add_membership("c1", "p1", "david@example.test")
        self.add_membership("c1", "p2", "david@example.test")
        options = self.client.get("/api/slideshows/options").get_json()["collections"]
        self.assertEqual(options[0]["image_count"], 2)
        with patch.object(threading.Thread, "start"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "c1", "duration_seconds": 20,
                "collection_version": 1,
                "transition": "mixed", "layout": "fill",
                "music_id": "jrpg-piano", "loop_playback": True,
            })
        self.assertEqual(response.status_code, 202)
        status = self.client.get(f"/api/slideshows/{response.get_json()['id']}").get_json()
        self.assertEqual(status["status"], "queued")
        with portal.db() as connection:
            parameters = json.loads(connection.execute(
                "SELECT parameters_json FROM slideshow_jobs WHERE id = ?",
                (response.get_json()["id"],),
            ).fetchone()[0])
        self.assertEqual([item["id"] for item in parameters["media_items"]], ["p1", "p2"])
        for item in parameters["media_items"]:
            self.assertEqual(item["version"], 1)
            self.assertEqual(item["owner_id"], "david@example.test")
            self.assertEqual(item["visibility"], "shared")
            self.assertIsNone(item["deleted_at"])
            self.assertRegex(item["content_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(item["byte_size"], 10)
            self.assertEqual(item["content_type"], "image/jpeg")
        self.assertEqual(parameters["collection_owner_id"], "david@example.test")
        self.assertEqual(parameters["collection_visibility"], "shared")
        self.assertEqual(parameters["transition"], "mixed")
        self.assertEqual(parameters["layout"], "fill")
        self.assertEqual(parameters["music_id"], "jrpg-piano")
        self.assertTrue(parameters["loop_playback"])

    def test_slideshow_deduplication_is_scoped_to_the_stable_principal(self):
        self.add_photo(
            "shared-frame", owner_id="david@example.test", owner_name="Shared Name"
        )
        self.add_collection(
            "shared-video-sources", "Shared sources",
            owner_id="david@example.test", owner_name="Shared Name",
        )
        self.add_membership(
            "shared-video-sources", "shared-frame", "david@example.test"
        )
        david, david_csrf = self.paired_client("david", "Same display name")
        diana, diana_csrf = self.paired_client("diana", "Same display name")
        request_body = {
            "collection_id": "shared-video-sources",
            "collection_version": 1,
            "duration_seconds": 30,
            "transition": "fade",
            "layout": "fill",
            "loop_playback": False,
        }

        david_first = david.post(
            "/api/slideshows", json=request_body,
            headers={"X-CSRF-Token": david_csrf},
        )
        david_retry = david.post(
            "/api/slideshows", json=request_body,
            headers={"X-CSRF-Token": david_csrf},
        )
        diana_first = diana.post(
            "/api/slideshows", json=request_body,
            headers={"X-CSRF-Token": diana_csrf},
        )
        diana_retry = diana.post(
            "/api/slideshows", json=request_body,
            headers={"X-CSRF-Token": diana_csrf},
        )

        for response in (david_first, david_retry, diana_first, diana_retry):
            self.assertEqual(response.status_code, 202, response.get_json())
        self.assertFalse(david_first.get_json().get("duplicate", False))
        self.assertTrue(david_retry.get_json()["duplicate"])
        self.assertFalse(diana_first.get_json().get("duplicate", False))
        self.assertTrue(diana_retry.get_json()["duplicate"])
        self.assertEqual(david_retry.get_json()["id"], david_first.get_json()["id"])
        self.assertEqual(diana_retry.get_json()["id"], diana_first.get_json()["id"])
        self.assertNotEqual(david_first.get_json()["id"], diana_first.get_json()["id"])

        with portal.db() as connection:
            jobs = connection.execute(
                "SELECT id,owner_id,owner_name,parameters_json FROM slideshow_jobs "
                "ORDER BY owner_id"
            ).fetchall()
        self.assertEqual(len(jobs), 2)
        self.assertEqual(
            {(job["id"], job["owner_id"]) for job in jobs},
            {
                (david_first.get_json()["id"], "david@example.test"),
                (diana_first.get_json()["id"], "diana@example.test"),
            },
        )
        self.assertEqual({job["owner_name"] for job in jobs}, {"Same display name"})
        self.assertEqual(len({job["parameters_json"] for job in jobs}), 1)

    def test_slideshow_idempotent_retry_wins_over_the_global_queue_limit(self):
        self.add_photo(
            "retry-frame", owner_id="david@example.test", owner_name="David"
        )
        self.add_collection(
            "retry-sources", "Retry sources",
            owner_id="david@example.test", owner_name="David",
        )
        self.add_membership("retry-sources", "retry-frame", "david@example.test")
        request_body = {
            "collection_id": "retry-sources",
            "collection_version": 1,
            "duration_seconds": 30,
            "transition": "fade",
            "layout": "fill",
        }

        with patch.object(portal, "SLIDESHOW_QUEUE_LIMIT", 1):
            first = self.client.post("/api/slideshows", json=request_body)
            retry = self.client.post("/api/slideshows", json=request_body)

        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(retry.status_code, 202, retry.get_json())
        self.assertTrue(retry.get_json()["duplicate"])
        self.assertEqual(retry.get_json()["id"], first.get_json()["id"])
        with portal.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM slideshow_jobs").fetchone()[0],
                1,
            )

    def test_slideshow_filter_supports_cuts_and_transitions(self):
        items = [
            {"is_video": False, "source_duration": 0},
            {"is_video": False, "source_duration": 0},
            {"is_video": False, "source_duration": 0},
        ]
        cuts, durations, starts = portal.slideshow_filter(items, 30, "none", "fill")
        self.assertIn("concat=n=3", ";".join(cuts))
        self.assertEqual(durations, [10, 10, 10])
        self.assertEqual(starts, [0, 10, 20])
        mixed, _, _ = portal.slideshow_filter(items, 30, "mixed", "fit")
        mixed_graph = ";".join(mixed)
        self.assertIn("xfade=transition=fade", mixed_graph)
        self.assertIn("xfade=transition=dissolve", mixed_graph)
        self.assertIn("force_original_aspect_ratio=decrease", mixed_graph)

    def test_slideshow_supports_twenty_minutes_and_music_preview(self):
        self.add_photo("p1", owner_id="david@example.test", owner_name="David")
        self.add_collection("c1", "Memories", owner_id="david@example.test", owner_name="David")
        self.add_membership("c1", "p1", "david@example.test")
        with patch.object(threading.Thread, "start"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "c1", "duration_seconds": 1200,
                "collection_version": 1,
                "transition": "fade", "layout": "fit",
            })
        self.assertEqual(response.status_code, 202)
        options = self.client.get("/api/slideshows/options").get_json()
        self.assertGreaterEqual(len(options["music"]), 4)
        preview = self.client.get(options["music"][0]["preview_url"])
        self.assertEqual(preview.status_code, 200)

    def test_slideshow_rejects_stale_collections_and_legacy_sources(self):
        self.add_photo("current-source", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "stale-source", "Stale", owner_id="david@example.test", owner_name="David"
        )
        self.add_membership("stale-source", "current-source", "david@example.test")
        with portal.db() as connection:
            connection.execute("UPDATE collections SET version=2 WHERE id='stale-source'")
        stale = self.client.post("/api/slideshows", json={
            "collection_id": "stale-source", "collection_version": 1,
            "duration_seconds": 30, "transition": "fade", "layout": "fill",
        })
        self.assertEqual(stale.status_code, 409)

        self.add_photo("legacy-source")
        self.add_collection(
            "legacy-sources", "Legacy source", owner_id="david@example.test",
            owner_name="David",
        )
        self.add_membership("legacy-sources", "legacy-source")
        legacy = self.client.post("/api/slideshows", json={
            "collection_id": "legacy-sources", "collection_version": 1,
            "duration_seconds": 30, "transition": "fade", "layout": "fill",
        })
        self.assertEqual(legacy.status_code, 403)
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM slideshow_jobs"
            ).fetchone()[0], 0)

    def test_slideshow_worker_stops_when_source_version_changes(self):
        self.add_photo("changed-source", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "changed-sources", "Changed", owner_id="david@example.test", owner_name="David"
        )
        self.add_membership("changed-sources", "changed-source", "david@example.test")
        with patch.object(threading.Thread, "start"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "changed-sources", "collection_version": 1,
                "duration_seconds": 30, "transition": "fade", "layout": "fill",
            })
        self.assertEqual(response.status_code, 202)
        with portal.db() as connection:
            connection.execute("UPDATE photos SET version=2 WHERE id='changed-source'")
            job = connection.execute(
                "SELECT * FROM slideshow_jobs WHERE id=?",
                (response.get_json()["id"],),
            ).fetchone()
            parameters = json.loads(job["parameters_json"])
            expected = {
                item["id"]: {key: value for key, value in item.items() if key != "id"}
                for item in parameters["media_items"]
            }
            with self.assertRaisesRegex(ValueError, "no longer available"):
                portal.slideshow_source_snapshot(connection, job, parameters, expected)

    def test_slideshow_snapshot_revalidates_privacy_and_owner(self):
        self.add_photo("publish-race", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "publish-races", "Publish race", owner_id="david@example.test",
            owner_name="David",
        )
        self.add_membership("publish-races", "publish-race", "david@example.test")
        response = self.client.post("/api/slideshows", json={
            "collection_id": "publish-races", "collection_version": 1,
            "duration_seconds": 30, "transition": "fade", "layout": "fill",
        })
        self.assertEqual(response.status_code, 202)
        with portal.db() as connection:
            job = connection.execute(
                "SELECT * FROM slideshow_jobs WHERE id=?", (response.get_json()["id"],)
            ).fetchone()
            parameters = json.loads(job["parameters_json"])
            expected = {
                item["id"]: {key: value for key, value in item.items() if key != "id"}
                for item in parameters["media_items"]
            }
            connection.execute(
                "UPDATE photos SET visibility='private' WHERE id='publish-race'"
            )
            with self.assertRaisesRegex(ValueError, "no longer available"):
                portal.slideshow_source_snapshot(connection, job, parameters, expected)

    def test_slideshow_with_any_private_source_keeps_result_private(self):
        self.add_photo(
            "private-frame", owner_id="david@example.test", owner_name="David",
            visibility="private",
        )
        self.add_collection(
            "shared-container", "Shared container", owner_id="david@example.test",
            owner_name="David", visibility="shared",
        )
        self.add_membership("shared-container", "private-frame", "david@example.test")
        response = self.client.post("/api/slideshows", json={
            "collection_id": "shared-container", "collection_version": 1,
            "duration_seconds": 30, "transition": "fade", "layout": "fill",
        })
        self.assertEqual(response.status_code, 202)
        with portal.db() as connection:
            job = connection.execute(
                "SELECT * FROM slideshow_jobs WHERE id=?", (response.get_json()["id"],)
            ).fetchone()
            parameters = json.loads(job["parameters_json"])
        self.assertEqual(job["visibility"], "private")
        self.assertEqual(parameters["result_visibility"], "private")

    def test_slideshow_queue_persists_worker_fencing_before_accepting(self):
        self.add_photo("render-source", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "render-sources", "Render", owner_id="david@example.test", owner_name="David"
        )
        self.add_membership("render-sources", "render-source", "david@example.test")
        response = self.client.post("/api/slideshows", json={
            "collection_id": "render-sources", "collection_version": 1,
            "duration_seconds": 30, "transition": "fade", "layout": "fill",
            "loop_playback": True,
        })
        self.assertEqual(response.status_code, 202)
        with portal.db() as connection:
            job = connection.execute(
                "SELECT * FROM slideshow_jobs WHERE id=?", (response.get_json()["id"],)
            ).fetchone()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["generation"], 1)
        self.assertEqual(job["attempt_count"], 0)
        self.assertRegex(job["target_photo_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(job["publish_intent_id"], job["target_photo_id"])
        self.assertEqual(job["publish_state"], "none")
        self.assertRegex(job["source_snapshot_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(hasattr(portal, "generate_slideshow_locked"))

    def test_slideshow_endpoint_is_queue_only(self):
        self.add_photo("queue-only", owner_id="david@example.test", owner_name="David")
        self.add_collection(
            "queue-only-collection", "Queue", owner_id="david@example.test",
            owner_name="David",
        )
        self.add_membership("queue-only-collection", "queue-only", "david@example.test")
        with patch.object(portal, "SLIDESHOW_EXECUTOR_MODE", "worker"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "queue-only-collection", "collection_version": 1,
                "duration_seconds": 30, "transition": "fade", "layout": "fill",
            })
        self.assertEqual(response.status_code, 503)
        with portal.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM slideshow_jobs").fetchone()[0], 0
            )

    def test_video_upload_preserves_original_and_returns_playback_metadata(self):
        def fake_prepare(path, photo_id, extension):
            return self.staged_video_derivatives(photo_id)

        with patch.object(portal, "prepare_video", side_effect=fake_prepare):
            response = self.client.post(
                "/api/upload",
                data={"media": (BytesIO(b"video-original"), "clip.mov")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added"], ["clip.mov"])
        listing = self.client.get("/api/photos").get_json()["photos"]
        self.assertEqual(len(listing), 1)
        self.assertTrue(listing[0]["is_video"])
        self.assertEqual(listing[0]["playback"], f"/media/play/{listing[0]['id']}")
        with portal.db() as connection:
            stored_path = connection.execute(
                "SELECT stored_path FROM photos WHERE id = ?", (listing[0]["id"],)
            ).fetchone()[0]
        self.assertEqual((portal.ORIGINALS / stored_path).read_bytes(), b"video-original")
        self.assertEqual(self.client.get(listing[0]["playback"]).data, b"playback")

    def test_media_home_and_viewer_wording(self):
        home = self.client.get("/").data
        photos = self.client.get("/photos").data
        self.assertIn(b"<strong>Media</strong>", home)
        self.assertNotIn(b"<strong>Add media</strong>", home)
        self.assertIn(b"All media", photos)
        self.assertIn(b'id="viewerVideo"', photos)

    def test_schema_initialization_is_safe_across_workers(self):
        race_db = Path(TEST_DATA.name) / "worker-race.db"
        errors = []
        with patch.object(portal, "DB_PATH", race_db):
            threads = [
                threading.Thread(target=lambda: self._record_initialize_error(errors))
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        connection = portal.sqlite3.connect(race_db)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(photos)")}
        connection.close()
        self.assertIn("playback_name", columns)

    def test_platform_schema_migrations_are_safe_across_workers(self):
        migration_cases = (
            (Path(TEST_DATA.name) / "notes-worker-race.db", notes_module.initialize_notes),
            (Path(TEST_DATA.name) / "files-worker-race.db", files_module.initialize_files),
        )
        for path, migration in migration_cases:
            errors = []
            threads = [
                threading.Thread(
                    target=lambda p=path, m=migration: self._record_migration_error(errors, p, m)
                )
                for _ in range(6)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [], path.name)

    def test_household_modules_open_without_pairing_and_keep_csrf(self):
        for path in ("/notes", "/movies", "/recipes"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
        david, csrf = self.paired_client()
        denied = david.post("/api/notes", json={"visibility": "shared"}, base_url="https://localhost")
        self.assertEqual(denied.status_code, 403)
        allowed = david.post(
            "/api/notes", json={"visibility": "shared"},
            headers={"X-CSRF-Token": csrf}, base_url="https://localhost",
        )
        self.assertEqual(allowed.status_code, 201)

    def test_private_note_is_visible_only_to_its_tailscale_owner(self):
        david, csrf = self.paired_client("david")
        private = david.post(
            "/api/notes", json={"visibility": "private"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["note"]
        self.assertEqual(private["visibility"], "private")
        self.assertTrue(private["is_mine"])
        self.assertEqual(david.get("/api/notes").get_json()["notes"], [])
        self.assertEqual(
            [item["id"] for item in david.get("/api/notes?view=mine").get_json()["notes"]],
            [private["id"]],
        )
        diana, diana_csrf = self.paired_client("diana")
        self.assertEqual(diana.get(f"/api/notes/{private['id']}").status_code, 404)
        self.assertEqual(
            diana.post(
                f"/api/notes/{private['id']}/state",
                json={"action": "trash", "version": private["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            404,
        )
        self.assertEqual(diana.get("/api/notes").get_json()["notes"], [])
        self.assertEqual(david.get(f"/api/notes/{private['id']}").status_code, 200)

    def test_historical_unowned_private_note_stays_hidden(self):
        david, _ = self.paired_client("david")
        with notes_module.connect(notes_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO notes
                   (id, visibility, owner, created_at, updated_at)
                   VALUES ('historical-private', 'private', 'david', ?, ?)""",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        self.assertEqual(david.get("/api/notes/historical-private").status_code, 404)

    def test_note_owner_is_verified_not_selected_by_the_client(self):
        david, csrf = self.paired_client("david")
        david_note = david.post(
            "/api/notes", json={"owner": "diana", "visibility": "shared"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["note"]
        diana, diana_csrf = self.paired_client("diana")
        diana_note = diana.post(
            "/api/notes", json={"owner": "david", "visibility": "shared"},
            headers={"X-CSRF-Token": diana_csrf},
        ).get_json()["note"]
        self.assertEqual(david_note["owner_display"], "David")
        self.assertEqual(diana_note["owner_display"], "Diana")
        self.assertEqual(
            [note["id"] for note in david.get("/api/notes?view=mine").get_json()["notes"]],
            [david_note["id"]],
        )
        self.assertEqual(
            [note["id"] for note in diana.get("/api/notes?view=mine").get_json()["notes"]],
            [diana_note["id"]],
        )
        notes_page = david.get("/notes").get_data(as_text=True)
        self.assertIn("Added by me", notes_page)
        self.assertIn("/static/notes.js?v=11", notes_page)

    def test_shared_notes_are_readable_but_only_the_verified_owner_can_mutate(self):
        david, david_csrf = self.allowlisted_client("david@example.test", "David")
        diana, diana_csrf = self.allowlisted_client("diana@example.test", "Diana")
        note = david.post(
            "/api/notes", json={"visibility": "shared"},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()["note"]
        shared = diana.get(f"/api/notes/{note['id']}")
        self.assertEqual(shared.status_code, 200)
        self.assertFalse(shared.get_json()["note"]["can_edit"])
        attempted = diana.put(
            f"/api/notes/{note['id']}",
            json={
                "version": note["version"], "title": "Changed by Diana", "body": "",
                "visibility": "shared", "note_type": "text", "tags": [], "checklist": [],
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(attempted.status_code, 403)
        attempted_state = diana.post(
            f"/api/notes/{note['id']}/state",
            json={"action": "trash", "version": note["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(attempted_state.status_code, 403)
        saved = david.put(
            f"/api/notes/{note['id']}",
            json={
                "version": note["version"], "title": "Owner update", "body": "",
                "visibility": "shared", "note_type": "text", "tags": [], "checklist": [],
            },
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(saved.status_code, 200)
        with notes_module.connect(notes_module.DB_PATH) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM mutation_audit WHERE domain='note' AND object_id=? ORDER BY id",
                (note["id"],),
            )]
        self.assertEqual(actions, ["create", "update"])

    def test_legacy_shared_note_is_neutral_read_only_and_never_assigned_silently(self):
        with notes_module.connect(notes_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO notes
                   (id,visibility,owner,created_at,updated_at,version)
                   VALUES ('legacy-shared-note','shared','home',?,?,1)""",
                ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        for login, name in (
            ("david@example.test", "David"),
            ("diana@example.test", "Diana"),
        ):
            client, csrf = self.allowlisted_client(login, name)
            note = client.get("/api/notes/legacy-shared-note").get_json()["note"]
            self.assertEqual(note["owner_display"], "Legacy (unclaimed)")
            self.assertEqual(note["ownership_status"], "legacy_unclaimed")
            self.assertFalse(note["can_edit"])
            response = client.post(
                "/api/notes/legacy-shared-note/state",
                json={"action": "trash", "version": 1},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status_code, 403)
        with notes_module.connect(notes_module.DB_PATH) as connection:
            row = connection.execute(
                "SELECT owner_id,deleted_at,version FROM notes WHERE id='legacy-shared-note'"
            ).fetchone()
        self.assertEqual(tuple(row), (None, None, 1))

    def test_note_mutation_and_audit_commit_or_roll_back_together(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        note = david.post(
            "/api/notes", json={"visibility": "shared"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["note"]
        with patch.object(notes_module, "audit_mutation", side_effect=sqlite3.Error("audit failed")):
            with self.assertRaises(sqlite3.Error):
                david.put(
                    f"/api/notes/{note['id']}",
                    json={
                        "version": note["version"], "title": "must roll back", "body": "",
                        "visibility": "shared", "note_type": "text", "tags": [], "checklist": [],
                    },
                    headers={"X-CSRF-Token": csrf},
                )
        current = david.get(f"/api/notes/{note['id']}").get_json()["note"]
        self.assertEqual(current["title"], "")
        self.assertEqual(current["version"], note["version"])
        with notes_module.connect(notes_module.DB_PATH) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM mutation_audit WHERE domain='note' AND object_id=? ORDER BY id",
                (note["id"],),
            )]
        self.assertEqual(actions, ["create"])

    def test_note_list_uses_compact_summaries_and_detail_keeps_full_content(self):
        david, csrf = self.paired_client("david")
        note = david.post(
            "/api/notes", json={"visibility": "shared"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["note"]
        body = "A useful opening sentence. " + ("private detail " * 400)
        payload = {
            "version": note["version"], "title": "Payload check", "body": body,
            "visibility": "shared", "note_type": "text", "tags": ["Audit"],
            "checklist": [],
        }
        saved = david.put(
            f"/api/notes/{note['id']}", json=payload,
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(saved.status_code, 200)

        listing = david.get("/api/notes?view=mine").get_json()
        self.assertEqual(listing["count"], 1)
        summary = listing["notes"][0]
        self.assertNotIn("body", summary)
        self.assertNotIn("checklist", summary)
        self.assertLessEqual(len(summary["summary"]), 240)
        self.assertTrue(summary["summary"].startswith("A useful opening sentence."))

        detail = david.get(f"/api/notes/{note['id']}").get_json()["note"]
        self.assertEqual(detail["body"], body)
        self.assertEqual(detail["checklist"], [])

    def test_note_stale_save_does_not_overwrite(self):
        david, csrf = self.paired_client()
        note = david.post("/api/notes", json={"visibility": "shared"}, headers={"X-CSRF-Token": csrf}).get_json()["note"]
        payload = {"version": note["version"], "title": "First", "body": "New", "visibility": "shared", "note_type": "text", "tags": [], "checklist": []}
        self.assertEqual(david.put(f"/api/notes/{note['id']}", json=payload, headers={"X-CSRF-Token": csrf}).status_code, 200)
        stale = david.put(f"/api/notes/{note['id']}", json={**payload, "title": "Stale"}, headers={"X-CSRF-Token": csrf})
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(stale.get_json()["conflict"])

    def test_note_trash_restore_and_permanent_delete_are_explicit(self):
        david, csrf = self.paired_client()
        note = david.post("/api/notes", json={"visibility": "shared"}, headers={"X-CSRF-Token": csrf}).get_json()["note"]
        trashed = david.post(
            f"/api/notes/{note['id']}/state",
            json={"action": "trash", "version": note["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        note = trashed.get_json()["note"]
        self.assertEqual(len(david.get("/api/notes?view=deleted").get_json()["notes"]), 1)
        restored = david.post(
            f"/api/notes/{note['id']}/state",
            json={"action": "restore", "version": note["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(restored.status_code, 200)
        note = restored.get_json()["note"]
        trashed = david.post(
            f"/api/notes/{note['id']}/state",
            json={"action": "trash", "version": note["version"]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["note"]
        retained = david.delete(
            f"/api/notes/{note['id']}",
            json={"confirm": "permanently delete", "version": trashed["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(retained.status_code, 503)
        self.assertTrue(retained.get_json()["retained"])
        self.assertEqual(david.get(f"/api/notes/{note['id']}").status_code, 200)

    def test_movie_manual_mode_and_random_pick(self):
        david, csrf = self.paired_client()
        created = david.post(
            "/api/movies", json={"title": "Moonrise Kingdom", "release_year": 2012},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        listing = david.get("/api/movies").get_json()
        self.assertEqual(listing["provider_mode"], "manual")
        self.assertEqual(listing["movies"][0]["title"], "Moonrise Kingdom")
        self.assertEqual(david.get("/api/movies/pick").status_code, 200)
        no_key = david.post("/api/movies/check", headers={"X-CSRF-Token": csrf})
        self.assertEqual(no_key.status_code, 503)
        self.assertTrue(no_key.get_json()["needs_api_key"])
        duplicate = david.post(
            "/api/movies", json={"title": "moonrise kingdom", "release_year": 2012},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_movie_listing_has_bounded_pagination_and_full_counts(self):
        david, _csrf = self.paired_client()
        with movies_module.connect(movies_module.DB_PATH) as connection:
            connection.executemany(
                """INSERT INTO movies
                   (id, tmdb_id, title, release_year, overview, runtime, genres_json,
                    poster_url, added_by, added_at, watched_at, media_type)
                   VALUES (?, NULL, ?, 2026, '', NULL, '[]', NULL, 'home', ?, NULL, 'movie')""",
                [(f"page-{index:02d}", f"Paged movie {index:02d}", f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00")
                 for index in range(31)],
            )

        first = david.get("/api/movies?type=movie").get_json()
        self.assertEqual(first["count"], 31)
        self.assertEqual(first["available_count"], 0)
        self.assertEqual(len(first["movies"]), 24)
        self.assertEqual(first["limit"], 24)
        self.assertEqual(first["offset"], 0)
        self.assertTrue(first["has_more"])

        last = david.get("/api/movies?type=movie&limit=10&offset=24").get_json()
        self.assertEqual(len(last["movies"]), 7)
        self.assertEqual(last["count"], 31)
        self.assertFalse(last["has_more"])

        bounded = david.get("/api/movies?type=movie&limit=1000&offset=-4").get_json()
        self.assertEqual(bounded["limit"], 100)
        self.assertEqual(bounded["offset"], 0)

    def test_movie_and_tv_watchlists_are_separate(self):
        david, csrf = self.paired_client()
        movie = david.post(
            "/api/movies", json={"title": "Shared Title", "release_year": 2020, "media_type": "movie"},
            headers={"X-CSRF-Token": csrf},
        )
        show = david.post(
            "/api/movies", json={"title": "Shared Title", "release_year": 2020, "media_type": "tv"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(movie.status_code, 201)
        self.assertEqual(show.status_code, 201)
        movies = david.get("/api/movies?type=movie").get_json()["movies"]
        shows = david.get("/api/movies?type=tv").get_json()["movies"]
        self.assertEqual([item["media_type"] for item in movies], ["movie"])
        self.assertEqual([item["media_type"] for item in shows], ["tv"])
        self.assertEqual(david.get("/api/movies/pick?type=tv").status_code, 200)
        html = david.get("/movies").get_data(as_text=True)
        self.assertIn('data-type="movie"', html)
        self.assertIn('data-type="tv"', html)

    def test_tmdb_failure_has_specific_recovery_and_preserves_watchlist(self):
        import urllib.error
        created=self.client.post("/api/movies",json={"title":"Offline fixture","media_type":"movie","visibility":"shared"})
        self.assertEqual(created.status_code,201)
        for status,code in ((401,"provider_credentials"),(429,"provider_rate_limit"),(503,"provider_unavailable")):
            with patch.object(movies_module,"TMDB_TOKEN","synthetic-secret"), patch("urllib.request.urlopen",side_effect=urllib.error.HTTPError("https://api.themoviedb.org/3/search/movie",status,"provider failure",{},None)):
                response=self.client.get("/api/movies/search?q=Fixture")
            self.assertEqual(response.json["code"],code)
            self.assertTrue(response.json["local_content_available"])
            self.assertNotIn("synthetic-secret",response.text)
            self.assertEqual(self.client.get("/api/movies").json["movies"][0]["title"],"Offline fixture")

    def test_movie_availability_and_pick_use_selected_country(self):
        created=self.client.post("/api/movies",json={"title":"Regional fixture","media_type":"movie","visibility":"shared"})
        self.assertEqual(created.status_code,201)
        movie_id=created.json.get("id") or created.json.get("movie",{}).get("id")
        if not movie_id: movie_id=self.client.get("/api/movies").json["movies"][0]["id"]
        with movies_module.connect(movies_module.DB_PATH) as connection:
            connection.execute("INSERT OR REPLACE INTO subscriptions(provider_id,name,enabled) VALUES(8,'Fixture provider',1)")
            connection.execute("INSERT INTO availability(movie_id,provider_id,provider_name,kind,region,checked_at) VALUES(?,8,'Fixture provider','included','US',?)",(movie_id,platform_module.utcnow()))
        with patch.object(movies_module,"REGION","CA"):
            listed=self.client.get("/api/movies").json
            self.assertEqual(listed["available_count"],0)
            self.assertFalse(listed["movies"][0]["available_now"])
            self.assertFalse(self.client.get("/api/movies/pick").json["prioritized_available"])
        with patch.object(movies_module,"REGION","US"):
            self.assertEqual(self.client.get("/api/movies").json["available_count"],1)

    def test_tmdb_movie_and_tv_search_and_add_flow(self):
        david, csrf = self.paired_client()

        def fake_tmdb(path, parameters=None):
            if path == "/search/movie":
                return {"results": [{
                    "id": 101, "title": "Test Movie", "release_date": "2026-01-02",
                    "overview": "Movie search result", "poster_path": "/movie.jpg",
                }]}
            if path == "/search/tv":
                return {"results": [{
                    "id": 202, "name": "Test Series", "first_air_date": "2025-03-04",
                    "overview": "TV search result", "poster_path": "/series.jpg",
                }]}
            if path == "/movie/101":
                return {
                    "id": 101, "title": "Test Movie", "release_date": "2026-01-02",
                    "overview": "Movie details", "runtime": 110,
                    "genres": [{"name": "Drama"}], "poster_path": "/movie.jpg",
                }
            if path == "/tv/202":
                return {
                    "id": 202, "name": "Test Series", "first_air_date": "2025-03-04",
                    "overview": "TV details", "episode_run_time": [48],
                    "genres": [{"name": "Mystery"}], "poster_path": "/series.jpg",
                }
            self.fail(f"Unexpected TMDB path: {path}")

        with patch.object(movies_module, "TMDB_TOKEN", "test-token"), patch.object(
            movies_module, "tmdb", side_effect=fake_tmdb
        ):
            movie_result = david.get("/api/movies/search?q=test&type=movie")
            tv_result = david.get("/api/movies/search?q=test&type=tv")
            self.assertEqual(movie_result.status_code, 200)
            self.assertEqual(tv_result.status_code, 200)
            movie = movie_result.get_json()["results"][0]
            show = tv_result.get_json()["results"][0]
            self.assertEqual(movie["media_type"], "movie")
            self.assertEqual(show["media_type"], "tv")
            self.assertEqual(
                david.post(
                    "/api/movies", json={**movie, "media_type": "movie"},
                    headers={"X-CSRF-Token": csrf},
                ).status_code,
                201,
            )
            self.assertEqual(
                david.post(
                    "/api/movies", json={**show, "media_type": "tv"},
                    headers={"X-CSRF-Token": csrf},
                ).status_code,
                201,
            )

        self.assertEqual(len(david.get("/api/movies?type=movie").get_json()["movies"]), 1)
        self.assertEqual(len(david.get("/api/movies?type=tv").get_json()["movies"]), 1)
        html = david.get("/movies").get_data(as_text=True)
        self.assertIn('id="movieSearchMessage"', html)
        self.assertIn('/static/movies.js?v=11', html)

    def test_movie_posters_use_bounded_same_origin_cache(self):
        david, csrf = self.paired_client()
        created = david.post(
            "/api/movies",
            json={
                "title": "Poster Test",
                "poster_url": "https://image.tmdb.org/t/p/w342/safePoster123.jpg",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        poster_url = created.get_json()["movie"]["poster_url"]
        self.assertEqual(poster_url, "/api/movies/poster/safePoster123.jpg")
        listed = david.get("/api/movies?type=movie").get_json()["movies"]
        self.assertEqual(listed[0]["poster_url"], poster_url)

        image_data = BytesIO()
        from PIL import Image
        Image.new("RGB", (8, 12), "navy").save(image_data, "JPEG")

        class PosterResponse(BytesIO):
            headers = {"Content-Type": "image/jpeg"}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()

        cache_path = movies_module.POSTER_CACHE / "safePoster123.jpg"
        cache_path.unlink(missing_ok=True)
        with patch.object(
            movies_module.urllib.request,
            "urlopen",
            return_value=PosterResponse(image_data.getvalue()),
        ) as fetch:
            response = david.get(poster_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertTrue(cache_path.is_file())
        fetch.assert_called_once()
        with patch.object(movies_module.urllib.request, "urlopen") as fetch:
            self.assertEqual(david.get(poster_url).status_code, 200)
        fetch.assert_not_called()
        self.assertEqual(david.get("/api/movies/poster/not-valid.txt").status_code, 404)

    def test_manual_movie_posters_are_proxied_and_owner_authorized(self):
        david, csrf = self.paired_client()
        created = david.post(
            "/api/movies",
            json={
                "title": "Private Poster Test",
                "poster_url": "https://posters.example.test/art/private.jpg",
                "visibility": "private",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        movie = created.get_json()["movie"]
        poster_url = f"/api/movies/{movie['id']}/poster"
        self.assertEqual(movie["poster_url"], poster_url)

        image_data = BytesIO()
        from PIL import Image
        Image.new("RGB", (8, 12), "green").save(image_data, "PNG")
        with patch.object(
            movies_module,
            "fetch_public",
            return_value=(image_data.getvalue(), "image/png", "https://posters.example.test/art/private.jpg"),
        ) as fetch:
            response = david.get(poster_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        fetch.assert_called_once_with(
            "https://posters.example.test/art/private.jpg", image_only=True
        )

        diana, _ = self.paired_client("diana")
        with patch.object(movies_module, "fetch_public") as fetch:
            self.assertEqual(diana.get(poster_url).status_code, 404)
        fetch.assert_not_called()

    def test_recipe_manual_creation_recommendation_and_ssrf_guard(self):
        david, csrf = self.paired_client()
        recipe = david.post(
            "/api/recipes",
            json={"title": "Tomato Soup", "meal_type": "dinner", "total_minutes": 25, "ingredients": ["Tomatoes", "Fresh basil"], "instructions": ["Simmer"], "tags": ["Quick"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(recipe.status_code, 201)
        recommendations = david.get("/api/recipes/recommend?meal=dinner&quick=1").get_json()["recipes"]
        self.assertEqual(recommendations[0]["title"], "Tomato Soup")
        multi_ingredient = david.get("/api/recipes/recommend?meal=dinner&ingredient=tomatoes,basil").get_json()["recipes"]
        self.assertEqual(multi_ingredient[0]["title"], "Tomato Soup")
        with self.assertRaises(ValueError):
            recipes_module.public_url("http://127.0.0.1/private")
        with self.assertRaises(ValueError):
            recipes_module.public_url("file:///etc/passwd")
        duplicate = david.post(
            "/api/recipes",
            json={"title": "Tomato Soup", "meal_type": "dinner", "ingredients": ["Tomatoes", "Fresh basil"], "instructions": ["Simmer"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_recipe_json_ld_parser(self):
        page = b'''<html><head><script type="application/ld+json">{
          "@context":"https://schema.org","@type":"Recipe","name":"Pancakes",
          "recipeIngredient":["1 cup flour"],"recipeInstructions":[{"@type":"HowToStep","text":"Mix and cook."}],
          "totalTime":"PT20M","recipeYield":"4 servings"
        }</script></head></html>'''
        result = recipes_module.parse_recipe_page(page, "https://example.com/pancakes")
        self.assertEqual(result["title"], "Pancakes")
        self.assertEqual(result["total_minutes"], 20)
        self.assertEqual(result["instructions"], ["Mix and cook."])

    def test_recipe_json_ld_parser_sums_prep_and_cook_time(self):
        page = b'''<html><head><script type="application/ld+json">{
          "@context":"https://schema.org","@type":"Recipe","name":"Soup",
          "recipeIngredient":["water"],"recipeInstructions":["Cook."],
          "prepTime":"PT10M","cookTime":"PT25M"
        }</script></head></html>'''
        result = recipes_module.parse_recipe_page(page, "https://example.com/soup")
        self.assertEqual(result["total_minutes"], 35)

    def test_recipe_image_proxy_returns_image_data(self):
        david, csrf = self.paired_client()
        recipe = david.post(
            "/api/recipes",
            json={
                "title": "Picture Recipe", "meal_type": "dinner",
                "ingredients": ["Rice"], "instructions": ["Cook"],
                "image_url": "https://images.example.com/recipe.jpg",
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        image_data = BytesIO()
        from PIL import Image
        Image.new("RGB", (4, 4), "red").save(image_data, "JPEG")
        with patch.object(recipes_module, "fetch_public", return_value=(image_data.getvalue(), "image/jpeg", "https://images.example.com/recipe.jpg")):
            response = david.get(recipe["image"])
        self.assertEqual(response.status_code, 200)
        self.assertGreater(len(response.data), 100)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        with patch.object(recipes_module, "fetch_public") as fetch:
            cached = david.get(recipe["image"])
        self.assertEqual(cached.status_code, 200)
        self.assertEqual(cached.data, response.data)
        fetch.assert_not_called()
        conditional = david.get(recipe["image"], headers={"If-None-Match": response.headers["ETag"]})
        self.assertEqual(conditional.status_code, 304)
        self.assertEqual(conditional.headers["Cache-Control"], "private, no-cache, max-age=0, must-revalidate")
        diana, _ = self.paired_client("diana")
        self.assertEqual(diana.get(recipe["image"], headers={"If-None-Match": response.headers["ETag"]}).status_code, 304)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            connection.execute("UPDATE recipes SET visibility='private' WHERE id=?", (recipe["id"],))
        denied = diana.get(recipe["image"], headers={"If-None-Match": response.headers["ETag"]})
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.headers["Cache-Control"], "private, no-store")
        private_owner = david.get(recipe["image"], headers={"If-None-Match": response.headers["ETag"]})
        self.assertEqual(private_owner.status_code, 304)
        self.assertEqual(private_owner.headers["Cache-Control"], "private, no-store")

    def test_recipe_quality_review_is_owner_scoped_and_never_automatic(self):
        david, csrf = self.paired_client()
        diana, diana_csrf = self.paired_client("diana")
        created = david.post(
            "/api/recipes",
            json={
                "title": "Chocolate Cake", "meal_type": "main",
                "ingredients": ["Chocolate", "Flour"],
                "instructions": ["Bake"],
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]

        refreshed = david.post(
            "/api/recipes/quality-review/refresh",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.get_json()["recipes_changed"], 0)
        self.assertEqual(
            david.get(f"/api/recipes/{created['id']}").get_json()["recipe"]["meal_type"],
            "main",
        )
        review = david.get("/api/recipes/quality-review").get_json()
        self.assertEqual(review["counts"]["pending"], 1)
        self.assertFalse(review["safety"]["automatic_recipe_changes"])
        proposal = review["proposals"][0]
        self.assertEqual(proposal["current"], "main")
        self.assertEqual(proposal["proposed"], "dessert")
        self.assertEqual(diana.get("/api/recipes/quality-review").get_json()["proposals"], [])
        denied = diana.put(
            f"/api/recipes/quality-review/{proposal['id']}",
            json={"action": "accept", "version": proposal["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(denied.status_code, 403)

        accepted = david.put(
            f"/api/recipes/quality-review/{proposal['id']}",
            json={"action": "accept", "version": proposal["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()["meal_type"], "dessert")
        recipe = david.get(f"/api/recipes/{created['id']}").get_json()["recipe"]
        self.assertEqual(recipe["meal_type"], "dessert")
        self.assertEqual(recipe["version"], created["version"] + 1)

    def test_recipe_quality_rejection_is_durable_and_stale_reviews_fail_closed(self):
        david, csrf = self.paired_client()
        first = david.post(
            "/api/recipes",
            json={
                "title": "Breakfast Pancakes", "meal_type": "main",
                "ingredients": ["Flour", "Milk"], "instructions": ["Cook"],
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        david.post(
            "/api/recipes/quality-review/refresh",
            headers={"X-CSRF-Token": csrf},
        )
        proposal = david.get("/api/recipes/quality-review").get_json()["proposals"][0]
        rejected = david.put(
            f"/api/recipes/quality-review/{proposal['id']}",
            json={"action": "reject", "version": proposal["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(rejected.status_code, 200)
        rescanned = david.post(
            "/api/recipes/quality-review/refresh",
            headers={"X-CSRF-Token": csrf},
        ).get_json()
        self.assertEqual(rescanned["created"], 0)
        self.assertEqual(rescanned["pending"], 0)
        self.assertEqual(
            david.get(f"/api/recipes/{first['id']}").get_json()["recipe"]["meal_type"],
            "main",
        )

        second = david.post(
            "/api/recipes",
            json={
                "title": "Vanilla Cake", "meal_type": "main",
                "ingredients": ["Vanilla", "Flour"], "instructions": ["Bake"],
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        david.post(
            "/api/recipes/quality-review/refresh",
            headers={"X-CSRF-Token": csrf},
        )
        proposal = david.get("/api/recipes/quality-review").get_json()["proposals"][0]
        updated_payload = {
            **second,
            "title": second["title"],
            "ingredients": second["ingredients"],
            "instructions": second["instructions"],
            "meal_type": "breakfast",
            "favorite": second["favorite"],
        }
        self.assertEqual(
            david.put(
                f"/api/recipes/{second['id']}",
                json=updated_payload,
                headers={"X-CSRF-Token": csrf},
            ).status_code,
            200,
        )
        stale = david.put(
            f"/api/recipes/quality-review/{proposal['id']}",
            json={"action": "accept", "version": proposal["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["status"], "stale")
        self.assertEqual(
            david.get(f"/api/recipes/{second['id']}").get_json()["recipe"]["meal_type"],
            "breakfast",
        )

    def test_recipe_discovery_requires_explicit_add_and_prevents_duplicates(self):
        david, csrf = self.paired_client()
        meal = {
            "idMeal": "52772", "strMeal": "Teriyaki Chicken Casserole",
            "strCategory": "Chicken", "strArea": "Japanese",
            "strMealThumb": "https://www.themealdb.com/images/media/meals/wvpsxx1468256321.jpg",
            "strInstructions": "Mix ingredients. Bake until ready.",
            "strIngredient1": "chicken", "strMeasure1": "500 g",
            "strTags": "Casserole", "strSource": "https://example.com/teriyaki",
        }
        with patch.object(recipes_module, "mealdb_request", return_value={"meals": [meal]}):
            discovery = david.get("/api/recipes/discover?q=chicken")
            self.assertEqual(discovery.status_code, 200)
            self.assertEqual(discovery.get_json()["results"][0]["mealdb_id"], "52772")
            self.assertEqual(
                discovery.get_json()["results"][0]["image_url"],
                "/api/recipes/discover/image/wvpsxx1468256321.jpg",
            )
            self.assertEqual(david.get("/api/recipes").get_json()["recipes"], [])
            imported = david.post(
                "/api/recipes/import-mealdb",
                json={"mealdb_id": "52772", "meal_type": "dinner"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(imported.status_code, 201)
            self.assertEqual(imported.get_json()["recipe"]["ingredients"], ["500 g chicken"])
            duplicate = david.post(
                "/api/recipes/import-mealdb",
                json={"mealdb_id": "52772", "meal_type": "dinner"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(duplicate.status_code, 409)

    def test_recipe_discovery_images_are_same_origin_and_normalized(self):
        david, _csrf = self.paired_client()
        self.assertIsNone(
            recipes_module.mealdb_image_proxy(
                "https://www.themealdb.com:444/images/media/meals/example.jpg"
            )
        )
        self.assertIsNone(
            recipes_module.mealdb_image_proxy(
                "https://attacker.invalid/images/media/meals/example.jpg"
            )
        )

        image_data = BytesIO()
        from PIL import Image
        Image.new("RGB", (10, 10), "orange").save(image_data, "WEBP")
        with patch.object(
            recipes_module,
            "fetch_public",
            return_value=(
                image_data.getvalue(),
                "image/webp",
                "https://www.themealdb.com/images/media/meals/example.webp",
            ),
        ) as fetch:
            response = david.get("/api/recipes/discover/image/example.webp")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        fetch.assert_called_once_with(
            "https://www.themealdb.com/images/media/meals/example.webp",
            image_only=True,
        )
        self.assertEqual(
            david.get("/api/recipes/discover/image/not-an-image.txt").status_code,
            404,
        )

    def test_files_folder_upload_preview_trash_restore_and_purge(self):
        client, csrf = self.paired_client()
        folder_response = client.post(
            "/api/files/folders", json={"name": "Documents"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(folder_response.status_code, 201)
        folder_id = folder_response.get_json()["folder"]["id"]
        upload = client.post(
            "/api/files/upload",
            data={"folder_id": folder_id, "files": (BytesIO(b"hello David-Pi"), "readme.txt")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        listing = client.get(f"/api/files?folder={folder_id}").get_json()
        self.assertEqual(listing["files"][0]["name"], "readme.txt")
        self.assertTrue(listing["files"][0]["viewable"])
        file_record = listing["files"][0]
        file_id = file_record["id"]
        self.assertEqual(client.get(f"/api/files/{file_id}/text").get_json()["text"], "hello David-Pi")
        self.assertEqual(client.get(f"/api/files/{file_id}/content").data, b"hello David-Pi")
        trashed = client.delete(
            f"/api/files/{file_id}", json={"version": file_record["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        file_record = trashed.get_json()["file"]
        self.assertEqual(len(client.get("/api/files?view=deleted").get_json()["files"]), 1)
        restored = client.post(
            f"/api/files/{file_id}/restore", json={"version": file_record["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(restored.status_code, 200)
        file_record = restored.get_json()["file"]
        file_record = client.delete(
            f"/api/files/{file_id}", json={"version": file_record["version"]},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["file"]
        denied = client.post(
            f"/api/files/{file_id}/purge", json={"confirm": "yes", "version": file_record["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(denied.status_code, 400)
        purged = client.post(
            f"/api/files/{file_id}/purge",
            json={"confirm": "permanently delete", "version": file_record["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(purged.status_code, 503)
        self.assertTrue(purged.get_json()["retained"])
        self.assertEqual(len(list(files_module.OBJECTS.glob("*"))), 1)

    def test_private_file_is_visible_only_to_verified_owner(self):
        david, csrf = self.paired_client("david")
        uploaded = david.post(
            "/api/files/upload",
            data={
                "visibility": "private",
                "files": (BytesIO(b"private test content"), "private.txt"),
            },
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(david.get("/api/files").get_json()["files"], [])
        david_files = david.get("/api/files?owner=mine").get_json()["files"]
        self.assertEqual(len(david_files), 1)
        self.assertTrue(david_files[0]["is_mine"])
        file_id = david_files[0]["id"]
        diana, diana_csrf = self.paired_client("diana")
        self.assertEqual(diana.get("/api/files").get_json()["files"], [])
        self.assertEqual(diana.get(f"/api/files/{file_id}/content").status_code, 404)
        self.assertEqual(
            diana.delete(
                f"/api/files/{file_id}", json={"version": david_files[0]["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            404,
        )
        self.assertEqual(david.get(f"/api/files/{file_id}/content").status_code, 200)

    def test_legacy_shared_file_is_visible_to_both_people(self):
        david, csrf = self.paired_client("david")
        uploaded = david.post(
            "/api/files/upload",
            data={"visibility": "shared", "files": (BytesIO(b"shared"), "shared.txt")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        diana, _ = self.paired_client("diana")
        self.assertEqual(len(diana.get("/api/files").get_json()["files"]), 1)

    def test_shared_files_and_folders_are_owner_mutable_without_admin_override(self):
        david, david_csrf = self.allowlisted_client("david@example.test", "David")
        diana, diana_csrf = self.allowlisted_client("diana@example.test", "Diana")
        folder = david.post(
            "/api/files/folders", json={"name": "David shared"},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()["folder"]
        upload = david.post(
            "/api/files/upload",
            data={"folder_id": folder["id"], "files": (BytesIO(b"owned"), "owned.txt")},
            headers={"X-CSRF-Token": david_csrf}, content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 201)
        file_record = diana.get(f"/api/files?folder={folder['id']}").get_json()["files"][0]
        self.assertFalse(file_record["can_edit"])
        self.assertEqual(
            diana.put(
                f"/api/files/{file_record['id']}",
                json={"name": "taken.txt", "version": file_record["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        self.assertEqual(
            diana.delete(
                f"/api/files/{file_record['id']}", json={"version": file_record["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        denied_upload = diana.post(
            "/api/files/upload",
            data={"folder_id": folder["id"], "files": (BytesIO(b"other"), "other.txt")},
            headers={"X-CSRF-Token": diana_csrf}, content_type="multipart/form-data",
        )
        self.assertEqual(denied_upload.status_code, 403)
        owner_file = david.get(f"/api/files?folder={folder['id']}").get_json()["files"][0]
        self.assertEqual(
            david.delete(
                f"/api/files/{owner_file['id']}", json={"version": owner_file["version"]},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            200,
        )
        with files_module.connect(files_module.DB_PATH) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM mutation_audit WHERE domain='file' AND object_id=? ORDER BY id",
                (file_record["id"],),
            )]
        self.assertEqual(actions, ["upload", "trash"])

    def test_legacy_shared_file_is_readable_but_immutable_and_unclaimed(self):
        object_path = files_module.OBJECTS / "legacy-unclaimed.txt"
        object_path.write_bytes(b"legacy")
        with files_module.connect(files_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO stored_files
                   (id,name,folder_id,stored_name,content_type,byte_size,sha256,
                    created_at,updated_at,uploaded_by,deleted_at,owner_id,owner_name,visibility,version)
                   VALUES ('legacy-unclaimed','legacy.txt',NULL,'legacy-unclaimed.txt','text/plain',6,?,
                           ?,?,'Home',NULL,NULL,NULL,'shared',1)""",
                (hashlib.sha256(b"legacy").hexdigest(), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )
        for login, name in (
            ("david@example.test", "David"),
            ("diana@example.test", "Diana"),
        ):
            client, csrf = self.allowlisted_client(login, name)
            record = client.get("/api/files").get_json()["files"][0]
            self.assertEqual(record["ownership_status"], "legacy_unclaimed")
            self.assertEqual(record["owner_display"], "Legacy (unclaimed)")
            self.assertFalse(record["can_edit"])
            self.assertEqual(client.get("/api/files/legacy-unclaimed/content").data, b"legacy")
            self.assertEqual(
                client.delete(
                    "/api/files/legacy-unclaimed", json={"version": 1},
                    headers={"X-CSRF-Token": csrf},
                ).status_code,
                403,
            )
        with files_module.connect(files_module.DB_PATH) as connection:
            row = connection.execute(
                "SELECT owner_id,deleted_at,version FROM stored_files WHERE id='legacy-unclaimed'"
            ).fetchone()
        self.assertEqual(tuple(row), (None, None, 1))
        self.assertTrue(object_path.is_file())

    def test_file_bulk_upload_is_all_or_nothing_on_stage_or_transaction_failure(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        with patch.object(files_module, "MAX_FILE_BYTES", 3):
            rejected = david.post(
                "/api/files/upload",
                data={"files": [(BytesIO(b"ok"), "ok.txt"), (BytesIO(b"large"), "large.txt")]},
                headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
            )
        self.assertEqual(rejected.status_code, 400)
        with patch.object(files_module, "audit_mutation", side_effect=sqlite3.Error("audit failed")):
            rolled_back = david.post(
                "/api/files/upload",
                data={"files": [(BytesIO(b"one"), "one.txt"), (BytesIO(b"two"), "two.txt")]},
                headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
            )
        self.assertEqual(rolled_back.status_code, 202)
        batch=rolled_back.json["upload_id"]
        with files_module.connect(files_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stored_files").fetchone()[0], 0)
            connection.execute("UPDATE file_upload_batches SET next_attempt=0 WHERE id=?",(batch,))
        self.assertTrue(files_module.file_jobs.process_upload_once(batch))
        status=david.get(f"/api/files/uploads/{batch}").json
        self.assertEqual(status["state"],"completed")
        self.assertEqual(len(status["added"]),2)
        self.assertEqual(list(files_module.INCOMING.glob("*.part")),[])

    def test_file_library_pages_and_filters_before_serialization(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": [
                (BytesIO(b"one"), "one.txt"),
                (BytesIO(b"two"), "two.pdf"),
                (BytesIO(b"three"), "three.docx"),
                (BytesIO(b"four"), "four.mp3"),
                (BytesIO(b"five"), "five.jpg"),
            ]},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        first = david.get("/api/files?limit=2&offset=0&summary=1").get_json()
        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["files"]), 2)
        self.assertTrue(first["has_more"])
        second = david.get(
            f"/api/files?limit=2&offset={first['next_offset']}&summary=0"
        ).get_json()
        self.assertIsNone(second["total"])
        self.assertEqual(len(second["files"]), 2)
        self.assertTrue(second["has_more"])
        self.assertEqual(
            [item["name"] for item in david.get("/api/files?kind=document&limit=10").get_json()["files"]],
            ["three.docx"],
        )
        self.assertEqual(
            [item["name"] for item in david.get("/api/files?kind=pdf&limit=10").get_json()["files"]],
            ["two.pdf"],
        )
        self.assertEqual(david.get("/api/files?kind=unknown").status_code, 400)

    def test_file_upload_collisions_never_overwrite_or_remove_existing_inodes(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        fixed_uuid = SimpleNamespace(hex="fixed-upload-id")
        existing_temp = files_module.INCOMING / "fixed-upload-id.part"
        existing_temp.write_bytes(b"another request")
        with patch.object(files_module.uuid, "uuid4", return_value=fixed_uuid):
            response = david.post(
                "/api/files/upload", data={"files": (BytesIO(b"new"), "new.txt")},
                headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(existing_temp.read_bytes(), b"another request")
        existing_temp.unlink()

        existing_object = files_module.OBJECTS / "fixed-upload-id.txt"
        existing_object.write_bytes(b"existing object")
        with patch.object(files_module.uuid, "uuid4", return_value=fixed_uuid):
            response = david.post(
                "/api/files/upload", data={"files": (BytesIO(b"new"), "new.txt")},
                headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(existing_object.read_bytes(), b"existing object")
        with files_module.connect(files_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM stored_files").fetchone()[0], 0)
        self.assertEqual(list(files_module.INCOMING.glob("*.part")), [])

    def test_private_file_and_folder_names_are_owner_isolated(self):
        david, david_csrf = self.allowlisted_client("david@example.test", "David")
        diana, diana_csrf = self.allowlisted_client("diana@example.test", "Diana")
        david_folder = david.post(
            "/api/files/folders",
            json={"name": "Private", "visibility": "private"},
            headers={"X-CSRF-Token": david_csrf},
        )
        diana_folder = diana.post(
            "/api/files/folders",
            json={"name": "Private", "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(david_folder.status_code, 201)
        self.assertEqual(diana_folder.status_code, 201)
        self.assertEqual(david_folder.get_json()["folder"]["name"], "Private")
        self.assertEqual(diana_folder.get_json()["folder"]["name"], "Private")

        for client, csrf, payload in (
            (david, david_csrf, b"David private"),
            (diana, diana_csrf, b"Diana private"),
        ):
            response = client.post(
                "/api/files/upload",
                data={"visibility": "private", "files": (BytesIO(payload), "private.txt")},
                headers={"X-CSRF-Token": csrf},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.get_json()["added"], ["private.txt"])

        david_files = david.get("/api/files?owner=mine").get_json()["files"]
        diana_files = diana.get("/api/files?owner=mine").get_json()["files"]
        self.assertEqual([item["name"] for item in david_files], ["private.txt"])
        self.assertEqual([item["name"] for item in diana_files], ["private.txt"])
        self.assertEqual(diana.get(
            f"/api/files/{david_files[0]['id']}/content"
        ).status_code, 404)
        self.assertEqual(david.get(
            f"/api/files/{diana_files[0]['id']}/content"
        ).status_code, 404)

    def test_serving_keeps_verified_descriptor_across_symlink_swap(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"safe"), "safe.txt")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        with files_module.connect(files_module.DB_PATH) as connection:
            row = connection.execute("SELECT * FROM stored_files").fetchone()
        object_path = files_module.OBJECTS / row["stored_name"]
        parked_path = files_module.OBJECTS / f"parked-{row['stored_name']}"
        outside = Path(TEST_DATA.name) / "outside-private.txt"
        outside.write_bytes(b"leak")
        original_open = files_module.OBJECT_STORAGE.open_regular
        swapped = False

        def open_then_swap(*args, **kwargs):
            nonlocal swapped
            result = original_open(*args, **kwargs)
            if not swapped:
                object_path.rename(parked_path)
                object_path.symlink_to(outside)
                swapped = True
            return result

        try:
            with patch.object(
                files_module.OBJECT_STORAGE, "open_regular", side_effect=open_then_swap
            ):
                response = david.get(f"/api/files/{row['id']}/content")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"safe")
            self.assertNotEqual(response.data, outside.read_bytes())
        finally:
            if object_path.is_symlink():
                object_path.unlink()
            if parked_path.exists():
                parked_path.rename(object_path)
            outside.unlink(missing_ok=True)

    def test_restored_file_with_identical_bytes_and_new_inode_remains_readable(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"portable restore bytes"), "restored.txt")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        with files_module.connect(files_module.DB_PATH) as connection:
            row = connection.execute("SELECT * FROM stored_files").fetchone()
        object_path = files_module.OBJECTS / row["stored_name"]
        old_identity = (object_path.stat().st_dev, object_path.stat().st_ino)
        replacement = files_module.OBJECTS / f"replacement-{row['stored_name']}"
        shutil.copyfile(object_path, replacement)
        self.assertNotEqual(
            (replacement.stat().st_dev, replacement.stat().st_ino), old_identity
        )
        os.replace(replacement, object_path)

        response = david.get(f"/api/files/{row['id']}/content")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"portable restore bytes")

    def test_pdf_cache_symlink_is_rejected_without_exposing_its_target(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"%PDF-1.4\n"), "private.pdf")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        row = david.get("/api/files").get_json()["files"][0]
        files_module.pdf_page_counts[row["id"]] = 1
        cache = files_module.PDF_CACHE / f"{row['id']}-1-1600.jpg"
        outside = Path(TEST_DATA.name) / "outside-pdf-cache.jpg"
        outside.write_bytes(b"\xff\xd8\xffSECRET-OUTSIDE-CACHE\xff\xd9")
        cache.symlink_to(outside)
        try:
            response = david.get(f"/api/files/{row['id']}/pdf/pages/1")
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(b"SECRET-OUTSIDE-CACHE", response.data)
        finally:
            cache.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_pdf_cache_serve_keeps_verified_descriptor_across_symlink_swap(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"%PDF-1.4\n"), "shared.pdf")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        row = david.get("/api/files").get_json()["files"][0]
        files_module.pdf_page_counts[row["id"]] = 1
        cache_name = f"{row['id']}-1-1600.jpg"
        cache = files_module.PDF_CACHE / cache_name
        parked = files_module.PDF_CACHE / f"parked-{cache_name}"
        outside = Path(TEST_DATA.name) / "outside-swapped-cache.jpg"
        safe = b"\xff\xd8\xffSAFE-CACHE\xff\xd9"
        cache.write_bytes(safe)
        outside.write_bytes(b"\xff\xd8\xffSECRET-OUTSIDE-CACHE\xff\xd9")
        original_open = files_module.PDF_STORAGE.open_regular
        swapped = False

        def open_then_swap(*args, **kwargs):
            nonlocal swapped
            result = original_open(*args, **kwargs)
            if not swapped and args[0] == cache_name:
                cache.rename(parked)
                cache.symlink_to(outside)
                swapped = True
            return result

        try:
            with patch.object(
                files_module.PDF_STORAGE, "open_regular", side_effect=open_then_swap
            ):
                response = david.get(f"/api/files/{row['id']}/pdf/pages/1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, safe)
            self.assertNotIn(b"SECRET-OUTSIDE-CACHE", response.data)
        finally:
            if cache.is_symlink():
                cache.unlink()
            if parked.exists():
                parked.rename(cache)
            outside.unlink(missing_ok=True)

    def test_shared_pdf_preview_revalidates_and_private_transition_denies_peer(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        diana, _ = self.allowlisted_client("diana@example.test", "Diana")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"%PDF-1.4\n"), "cache-policy.pdf")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        row = david.get("/api/files").get_json()["files"][0]
        files_module.pdf_page_counts[row["id"]] = 1
        (files_module.PDF_CACHE / f"{row['id']}-1-1200.jpg").write_bytes(
            b"\xff\xd8\xffSAFE-CACHE\xff\xd9"
        )
        response = david.get(f"/api/files/{row['id']}/pdf/pages/1?width=1200")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-cache, max-age=0, must-revalidate")
        conditional = diana.get(
            f"/api/files/{row['id']}/pdf/pages/1?width=1200",
            headers={"If-None-Match": response.headers["ETag"]},
        )
        self.assertEqual(conditional.status_code, 304)
        with files_module.connect(files_module.DB_PATH) as connection:
            connection.execute(
                "UPDATE stored_files SET visibility='private' WHERE id=?", (row["id"],)
            )
        denied = diana.get(
            f"/api/files/{row['id']}/pdf/pages/1?width=1200",
            headers={"If-None-Match": response.headers["ETag"]},
        )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(denied.headers["Cache-Control"], "private, no-store")
        owner = david.get(
            f"/api/files/{row['id']}/pdf/pages/1?width=1200",
            headers={"If-None-Match": response.headers["ETag"]},
        )
        self.assertEqual(owner.status_code, 304)
        self.assertEqual(owner.headers["Cache-Control"], "private, no-store")

    def test_pdf_cache_render_publishes_from_pinned_incoming_descriptor(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        uploaded = david.post(
            "/api/files/upload",
            data={"files": (BytesIO(b"%PDF-1.4\n"), "render.pdf")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        row = david.get("/api/files").get_json()["files"][0]
        files_module.pdf_page_counts[row["id"]] = 1
        rendered_bytes = b"\xff\xd8\xffPINNED-RENDER\xff\xd9"
        observed_prefixes = []

        def render_to_requested_prefix(command, **_kwargs):
            output_prefix = command[-1]
            observed_prefixes.append(output_prefix)
            Path(f"{output_prefix}.jpg").write_bytes(rendered_bytes)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        with patch.object(files_module.subprocess, "run", side_effect=render_to_requested_prefix):
            response = david.get(f"/api/files/{row['id']}/pdf/pages/1")
            self.assertEqual(response.status_code,202)
            self.assertTrue(files_module.file_jobs.process_pdf_once())
            response = david.get(f"/api/files/{row['id']}/pdf/pages/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, rendered_bytes)
        self.assertEqual(len(observed_prefixes), 1)
        self.assertTrue(observed_prefixes[0].startswith("/proc/self/fd/"))
        cache = files_module.PDF_CACHE / f"{row['id']}-1-1600.jpg"
        self.assertEqual(cache.read_bytes(), rendered_bytes)
        self.assertEqual(list(files_module.INCOMING.glob("pdf-*.jpg")), [])

    @unittest.skipUnless(hasattr(os, "fork"), "requires Linux process semantics")
    def test_upload_recovers_idempotently_after_abrupt_post_link_termination(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        process_id = os.fork()
        if process_id == 0:  # pragma: no cover - the parent verifies durable effects
            def terminate_before_database_commit(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGKILL)

            with patch.object(
                files_module,
                "_finalize_upload_batch",
                side_effect=terminate_before_database_commit,
            ):
                david.post(
                    "/api/files/upload",
                    data={"files": (BytesIO(b"survives restart"), "restart.txt")},
                    headers={"X-CSRF-Token": csrf},
                    content_type="multipart/form-data",
                )
            os._exit(3)

        _waited, status = os.waitpid(process_id, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        with files_module.connect(files_module.DB_PATH) as connection:
            intent = connection.execute("SELECT * FROM file_upload_intents").fetchone()
            self.assertEqual(intent["state"], "prepared")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM stored_files"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='file' AND object_id=?",
                (intent["id"],),
            ).fetchone()[0], 0)
        incoming = files_module.INCOMING / intent["temp_name"]
        published = files_module.OBJECTS / intent["stored_name"]
        self.assertEqual(incoming.stat().st_ino, published.stat().st_ino)

        self.assertEqual(files_module.recover_upload_intents(), {
            "recovered_batches": 1, "unresolved_batches": 0,
        })
        self.assertEqual(files_module.recover_upload_intents(), {
            "recovered_batches": 0, "unresolved_batches": 0,
        })
        with files_module.connect(files_module.DB_PATH) as connection:
            saved = connection.execute(
                "SELECT * FROM stored_files WHERE id=?", (intent["id"],)
            ).fetchone()
            self.assertIsNotNone(saved)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='file' AND object_id=? AND action='upload'",
                (intent["id"],),
            ).fetchone()[0], 1)
        self.assertFalse(incoming.exists())
        self.assertEqual(david.get(f"/api/files/{intent['id']}/content").data, b"survives restart")

    def test_committed_note_and_upload_survive_connection_close_errors(self):
        david, csrf = self.allowlisted_client("david@example.test", "David")
        real_connect = sqlite3.connect

        class CloseFaultProxy:
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

            def close(self):
                self._connection.close()
                raise sqlite3.OperationalError("synthetic close failure")

        def faulty_connect(*args, **kwargs):
            return CloseFaultProxy(real_connect(*args, **kwargs))

        with patch.object(platform_module.sqlite3, "connect", side_effect=faulty_connect):
            note_response = david.post(
                "/api/notes", json={"visibility": "private"},
                headers={"X-CSRF-Token": csrf},
            )
            upload_response = david.post(
                "/api/files/upload",
                data={"visibility": "private", "files": (BytesIO(b"kept"), "kept.txt")},
                headers={"X-CSRF-Token": csrf},
                content_type="multipart/form-data",
            )
        self.assertEqual(note_response.status_code, 201)
        self.assertEqual(upload_response.status_code, 201)
        note_id = note_response.get_json()["note"]["id"]
        with notes_module.connect(notes_module.DB_PATH) as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM notes WHERE id=?", (note_id,)
            ).fetchone())
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='note' AND object_id=?",
                (note_id,),
            ).fetchone()[0], 1)
        with files_module.connect(files_module.DB_PATH) as connection:
            saved = connection.execute(
                "SELECT * FROM stored_files WHERE name='kept.txt'"
            ).fetchone()
            self.assertIsNotNone(saved)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='file' AND object_id=?",
                (saved["id"],),
            ).fetchone()[0], 1)
        self.assertEqual(
            (files_module.OBJECTS / saved["stored_name"]).read_bytes(), b"kept"
        )

    def test_committed_media_survives_connection_close_errors(self):
        real_connect = sqlite3.connect

        class CloseFaultProxy:
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

            def close(self):
                self._connection.close()
                raise sqlite3.OperationalError("synthetic close failure")

        def faulty_connect(*args, **kwargs):
            return CloseFaultProxy(real_connect(*args, **kwargs))

        image = BytesIO()
        portal.Image.new("RGB", (9, 7), "navy").save(image, "PNG")
        image.seek(0)
        with patch.object(platform_module.sqlite3, "connect", side_effect=faulty_connect):
            response = self.client.post(
                "/api/upload",
                data={"visibility": "private", "media": (image, "close-safe.png")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        photo_id = response.get_json()["added_items"][0]["id"]
        with portal.db() as connection:
            row = connection.execute(
                "SELECT * FROM photos WHERE id=?", (photo_id,)
            ).fetchone()
            intent = connection.execute(
                "SELECT state,source_path FROM media_publish_intents WHERE id=?", (photo_id,)
            ).fetchone()
            audit_count = connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE domain='media' AND object_id=? AND action='create'""",
                (photo_id,),
            ).fetchone()[0]
            outbox = connection.execute(
                """SELECT event_type,object_version FROM domain_outbox
                   WHERE domain='media' AND object_id=? ORDER BY id""",
                (photo_id,),
            ).fetchall()
        self.assertIsNotNone(row)
        self.assertEqual(intent["state"], "committed")
        self.assertEqual(audit_count, 1)
        self.assertEqual([tuple(row) for row in outbox], [("create", 1)])
        self.assertTrue((portal.ORIGINALS / row["stored_path"]).is_file())
        self.assertFalse((portal.DATA / intent["source_path"]).exists())

    def test_media_publish_intent_recovers_kill_after_physical_publication(self):
        staged = Path(TEST_DATA.name) / "kill-window.png"
        portal.Image.new("RGB", (12, 10), "gold").save(staged, "PNG")
        with patch.object(
            portal, "_finalize_media_intent", side_effect=SystemExit("synthetic kill")
        ):
            with self.assertRaisesRegex(SystemExit, "synthetic kill"):
                portal.canonical_ingest_media(
                    staged_path=staged,
                    original_filename="kill-window.png",
                    mime_type="image/png",
                    owner_user_id="david@example.test",
                    owner_name="David",
                    visibility="private",
                )
        with portal.db() as connection:
            intent = dict(connection.execute(
                "SELECT * FROM media_publish_intents WHERE state='prepared'"
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
            ).fetchone())
        self.assertTrue((portal.ORIGINALS / intent["stored_path"]).is_file())
        self.assertTrue(staged.is_file())

        self.assertEqual(
            portal.recover_media_publish_intents(),
            {"recovered_intents": 1, "unresolved_intents": 0},
        )
        self.assertEqual(
            portal.recover_media_publish_intents(),
            {"recovered_intents": 0, "unresolved_intents": 0},
        )
        with portal.db() as connection:
            photo = connection.execute(
                "SELECT * FROM photos WHERE id=?", (intent["id"],)
            ).fetchone()
            audit_count = connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE domain='media' AND object_id=? AND action='create'""",
                (intent["id"],),
            ).fetchone()[0]
        self.assertIsNotNone(photo)
        self.assertEqual(audit_count, 1)
        self.assertFalse(staged.exists())

    @unittest.skipUnless(hasattr(os, "fork"), "requires Linux process semantics")
    def test_media_publish_intent_recovers_after_actual_process_kill(self):
        staged = Path(TEST_DATA.name) / "process-kill-window.png"
        portal.Image.new("RGB", (11, 9), "teal").save(staged, "PNG")
        process_id = os.fork()
        if process_id == 0:  # pragma: no cover - parent verifies durable state
            def terminate_before_database_commit(*_args, **_kwargs):
                os.kill(os.getpid(), signal.SIGKILL)

            with patch.object(
                portal,
                "_finalize_media_intent",
                side_effect=terminate_before_database_commit,
            ):
                portal.canonical_ingest_media(
                    staged_path=staged,
                    original_filename="process-kill-window.png",
                    mime_type="image/png",
                    owner_user_id="david@example.test",
                    owner_name="David",
                    visibility="private",
                )
            os._exit(3)

        _waited, status = os.waitpid(process_id, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        with portal.db() as connection:
            intent = dict(connection.execute(
                "SELECT * FROM media_publish_intents WHERE state='prepared'"
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
            ).fetchone())
        for artifact in json.loads(intent["artifacts_json"]):
            storage = portal.MEDIA_ARTIFACT_STORAGE[artifact["kind"]]
            descriptor, _metadata = storage.open_regular_path(
                artifact["name"], expected_size=artifact["byte_size"]
            )
            os.close(descriptor)
            self.assertTrue((portal.DATA / artifact["source_path"]).is_file())

        self.assertEqual(portal.recover_media_publish_intents(), {
            "recovered_intents": 1,
            "unresolved_intents": 0,
        })
        self.assertEqual(portal.recover_media_publish_intents(), {
            "recovered_intents": 0,
            "unresolved_intents": 0,
        })
        with portal.db() as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
            ).fetchone())
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM mutation_audit
                   WHERE domain='media' AND object_id=? AND action='create'""",
                (intent["id"],),
            ).fetchone()[0], 1)
        self.assertFalse(staged.exists())

    def test_media_publish_recovery_retains_ambiguous_object_and_source(self):
        staged = Path(TEST_DATA.name) / "ambiguous-window.png"
        portal.Image.new("RGB", (10, 10), "purple").save(staged, "PNG")
        with patch.object(portal, "_finalize_media_intent", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                portal.canonical_ingest_media(
                    staged_path=staged,
                    original_filename="ambiguous-window.png",
                    mime_type="image/png",
                    owner_user_id="david@example.test",
                    owner_name="David",
                )
        with portal.db() as connection:
            intent = dict(connection.execute(
                "SELECT * FROM media_publish_intents WHERE state='prepared'"
            ).fetchone())
        published = portal.ORIGINALS / intent["stored_path"]
        parked = published.with_name(f"parked-{published.name}")
        published.rename(parked)
        published.write_bytes(b"x" * int(intent["byte_size"]))
        try:
            result = portal.recover_media_publish_intents()
            self.assertEqual(result, {"recovered_intents": 0, "unresolved_intents": 1})
            self.assertEqual(published.read_bytes(), b"x" * int(intent["byte_size"]))
            self.assertTrue(staged.is_file())
            with portal.db() as connection:
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT state FROM media_publish_intents WHERE id=?", (intent["id"],)
                ).fetchone()["state"], "prepared")
        finally:
            published.unlink(missing_ok=True)
            parked.rename(published)
            for artifact in json.loads(intent["artifacts_json"]):
                (
                    portal.MEDIA_ARTIFACT_STORAGE[artifact["kind"]].path
                    / artifact["name"]
                ).unlink(missing_ok=True)
                if artifact.get("source_path"):
                    (portal.DATA / artifact["source_path"]).unlink(missing_ok=True)
            staged.unlink(missing_ok=True)

    def test_media_recovery_never_adopts_or_deletes_replaced_stage_inode(self):
        staged = Path(TEST_DATA.name) / "replaced-stage-window.png"
        portal.Image.new("RGB", (10, 8), "maroon").save(staged, "PNG")
        with patch.object(portal, "_finalize_media_intent", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                portal.canonical_ingest_media(
                    staged_path=staged,
                    original_filename="replaced-stage-window.png",
                    mime_type="image/png",
                    owner_user_id="david@example.test",
                    owner_name="David",
                )
        with portal.db() as connection:
            intent = dict(connection.execute(
                "SELECT * FROM media_publish_intents WHERE state='prepared'"
            ).fetchone())
        artifacts = json.loads(intent["artifacts_json"])
        original = next(
            artifact for artifact in artifacts if artifact["kind"] == "original"
        )
        published = portal.ORIGINALS / original["name"]
        source = portal.DATA / original["source_path"]
        parked_source = source.with_name(f"parked-{source.name}")
        content = source.read_bytes()
        published.unlink()
        source.rename(parked_source)
        source.write_bytes(content)
        try:
            result = portal.recover_media_publish_intents()
            self.assertEqual(
                result, {"recovered_intents": 0, "unresolved_intents": 1}
            )
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual(parked_source.read_bytes(), content)
            self.assertFalse(published.exists())
            with portal.db() as connection:
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM photos WHERE id=?", (intent["id"],)
                ).fetchone())
                self.assertEqual(connection.execute(
                    "SELECT state FROM media_publish_intents WHERE id=?",
                    (intent["id"],),
                ).fetchone()["state"], "prepared")
        finally:
            source.unlink(missing_ok=True)
            parked_source.rename(source)
            for artifact in artifacts:
                (
                    portal.MEDIA_ARTIFACT_STORAGE[artifact["kind"]].path
                    / artifact["name"]
                ).unlink(missing_ok=True)
                if artifact.get("source_path"):
                    (portal.DATA / artifact["source_path"]).unlink(missing_ok=True)

    def test_unallowlisted_tailscale_member_cannot_create_personal_content(self):
        guest, csrf = self.paired_client("guest", name="Alex")
        guest.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "alex@example.test"
        created = guest.post(
            "/api/notes",
            json={"visibility": "private"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 403)
        self.assertEqual(guest.get("/api/notes").status_code, 403)
        with notes_module.connect(notes_module.DB_PATH) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notes").fetchone()[0], 0)

    def test_docx_files_have_safe_text_preview(self):
        client, csrf = self.paired_client()
        document = BytesIO()
        with zipfile.ZipFile(document, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                <w:body><w:p><w:r><w:t>Hello from Word</w:t></w:r></w:p>
                <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p></w:body></w:document>""",
            )
        document.seek(0)
        uploaded = client.post(
            "/api/files/upload",
            data={"files": (document, "letter.docx")},
            headers={"X-CSRF-Token": csrf},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        item = client.get("/api/files").get_json()["files"][0]
        self.assertEqual(item["kind"], "document")
        self.assertTrue(item["viewable"])
        preview = client.get(f"/api/files/{item['id']}/text").get_json()["text"]
        self.assertEqual(preview, "Hello from Word\n\nSecond paragraph")

    def test_device_pairing_is_one_use_and_revocable(self):
        token = self.client.post(
            "/api/device-backup/pairing-token",
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["pairing_token"]
        first = self.client.post(
            "/api/v1/device-backup/pair",
            json={"pairing_token": token, "device_name": "Pixel"},
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            "/api/v1/device-backup/pair",
            json={"pairing_token": token, "device_name": "Other"},
        )
        self.assertIn(second.status_code, (401, 409))
        paired = first.get_json()
        revoked = self.client.post(
            f"/api/device-backup/devices/{paired['device_id']}/revoke",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertTrue(revoked.get_json()["removed"])
        self.assertTrue(revoked.get_json()["deleted"])
        with portal.db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT id FROM backup_devices WHERE id=?", (paired["device_id"],)
            ).fetchone())
        self.assertNotIn(b"Pixel", self.client.get("/device-backup").data)
        denied = self.client.get(
            "/api/v1/device-backup/status",
            headers={"Authorization": f"Bearer {paired['device_credential']}"},
        )
        self.assertEqual(denied.status_code, 401)

    def test_pairing_link_origin_cannot_be_changed_by_an_allowed_host_header(self):
        alternate = portal.app.test_client()
        alternate.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "david@example.test"
        alternate.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "David"
        alternate.set_cookie("david_pi_csrf", self.csrf, domain="test.localhost")
        response = alternate.post(
            "/api/device-backup/pairing-token",
            base_url="https://test.localhost",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        canonical = "https://server.example-tail.ts.net"
        self.assertEqual(payload["server_url"], canonical)
        self.assertIn("server=https%3A%2F%2Fserver.example-tail.ts.net", payload["deep_link"])
        self.assertNotIn("test.localhost", payload["deep_link"])

    def test_android_preflight_does_not_reveal_another_owners_private_hash(self):
        private_image = BytesIO()
        portal.Image.new("RGB", (13, 9), "indigo").save(private_image, "PNG")
        private_bytes = private_image.getvalue()
        private_image.seek(0)
        created = self.client.post(
            "/api/upload",
            data={"visibility": "private", "media": (private_image, "david-private.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 200, created.get_data(as_text=True))
        private_id = created.get_json()["added_items"][0]["id"]
        with portal.db() as connection:
            private_row = connection.execute(
                "SELECT owner_id,visibility,content_sha256,byte_size FROM photos WHERE id=?",
                (private_id,),
            ).fetchone()
        self.assertEqual(private_row["owner_id"], "david@example.test")
        self.assertEqual(private_row["visibility"], "private")

        diana, diana_csrf = self.paired_client("diana", "Diana")
        paired = self.pair_backup_device(
            client=diana,
            owner="diana@example.test",
            name="Diana",
            csrf=diana_csrf,
        )
        authorization = {"Authorization": f"Bearer {paired['device_credential']}"}
        preflight = diana.post(
            "/api/v1/device-backup/uploads",
            headers=authorization,
            json={
                "client_item_id": "diana-known-private-hash",
                "original_filename": "diana-copy.png",
                "byte_size": private_row["byte_size"],
                "sha256": private_row["content_sha256"],
                "mime_type": "image/png",
            },
        )
        self.assertEqual(preflight.status_code, 201, preflight.get_data(as_text=True))
        preflight_payload = preflight.get_json()
        self.assertEqual(preflight_payload["state"], "uploading")
        self.assertNotIn("media_id", preflight_payload)

        appended = diana.patch(
            f"/api/v1/device-backup/uploads/{preflight_payload['upload_id']}",
            headers={**authorization, "Upload-Offset": "0"},
            data=private_bytes,
            content_type="application/offset+octet-stream",
        )
        self.assertEqual(appended.status_code, 204, appended.get_data(as_text=True))
        completed = diana.post(
            f"/api/v1/device-backup/uploads/{preflight_payload['upload_id']}/complete",
            headers=authorization,
        )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertEqual(completed.get_json()["state"], "primary_verified")

    def test_removing_uploaded_device_preserves_media_and_hidden_provenance(self):
        paired = self.pair_backup_device()
        device_id = paired["device_id"]
        self.add_photo(
            "device-owned-photo", owner_id="david@example.test",
            owner_name="David", visibility="private",
        )
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO device_media_records
                   (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                    content_sha256,byte_size,capture_timestamp,ingestion_source,
                    primary_verification_state,secondary_verification_state,ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "device-record", device_id, "client-item", "device-owned-photo",
                    "david@example.test", "photo.jpg", "hash-device-owned-photo", 10,
                    "2026-01-01T00:00:00+00:00", "android_backup",
                    "primary_verified", "secondary_pending", "2026-01-01T00:00:00+00:00",
                ),
            )
        removed = self.client.post(
            f"/api/device-backup/devices/{device_id}/revoke",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.get_json()["deleted"])
        with portal.db() as connection:
            device = connection.execute(
                "SELECT revoked_at FROM backup_devices WHERE id=?", (device_id,)
            ).fetchone()
            photo = connection.execute(
                "SELECT id FROM photos WHERE id='device-owned-photo'"
            ).fetchone()
            provenance = connection.execute(
                "SELECT id FROM device_media_records WHERE id='device-record'"
            ).fetchone()
        self.assertIsNotNone(device["revoked_at"])
        self.assertIsNotNone(photo)
        self.assertIsNotNone(provenance)
        self.assertNotIn(b"David's phone", self.client.get("/device-backup").data)

    def test_retired_iphone_endpoints_never_mint_devices_or_accept_uploads(self):
        for route in ("/api/ios-backup/shortcut", "/api/ios-backup/credential-file", "/api/v1/ios-backup/pair", "/api/v1/ios-backup/upload", "/api/v1/ios-backup/upload-file", "/api/v1/ios-backup/checkpoint"):
            self.assertEqual(self.client.post(route,json={}).status_code,404,route)
        with portal.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM backup_devices").fetchone()[0],0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM device_uploads").fetchone()[0],0)

















    def test_expired_pairing_and_missing_storage_fail_closed(self):
        token_response = self.client.post(
            "/api/device-backup/pairing-token",
            headers={"X-CSRF-Token": self.csrf},
        )
        token = token_response.get_json()["pairing_token"]
        with portal.db() as connection:
            connection.execute(
                "UPDATE device_pairing_tokens SET expires_at='2000-01-01T00:00:00+00:00'"
            )
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/pair",
                json={"pairing_token": token, "device_name": "Expired"},
            ).status_code,
            401,
        )

        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        original_sentinel = device_backup_module.PRIMARY_SENTINEL
        device_backup_module.PRIMARY_SENTINEL = Path(TEST_DATA.name) / "missing-sentinel"
        try:
            response = self.client.post(
                "/api/v1/device-backup/uploads",
                json={
                    "client_item_id": "missing-mount",
                    "original_filename": "safe.jpg",
                    "byte_size": 100,
                    "sha256": "b" * 64,
                    "mime_type": "image/jpeg",
                },
                headers=auth,
            )
        finally:
            device_backup_module.PRIMARY_SENTINEL = original_sentinel
        self.assertEqual(response.status_code, 503)

    def test_android_image_resumes_and_appears_for_paired_owner(self):
        paired = self.pair_backup_device()
        image = BytesIO()
        from PIL import Image
        Image.new("RGB", (16, 12), "coral").save(image, "PNG")
        content = image.getvalue()
        digest = __import__("hashlib").sha256(content).hexdigest()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        rejected_owner = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "media:101",
                "original_filename": "camera.png",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "image/png",
                "owner_user_id": "diana@example.test",
            },
            headers=auth,
        )
        self.assertEqual(rejected_owner.status_code, 422)
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "media:101",
                "original_filename": "camera.png",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "image/png",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        midpoint = len(content) // 2
        first = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content[:midpoint],
            headers={**auth, "Upload-Offset": "0", "Content-Type": "application/offset+octet-stream"},
        )
        self.assertEqual(first.status_code, 204)
        offset = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(int(offset.headers["Upload-Offset"]), midpoint)
        second = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content[midpoint:],
            headers={**auth, "Upload-Offset": str(midpoint), "Content-Type": "application/offset+octet-stream"},
        )
        self.assertEqual(second.status_code, 204)
        completed = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        media_id = completed.get_json()["media_id"]
        resumed = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(resumed.status_code, 204)
        self.assertEqual(int(resumed.headers["Upload-Offset"]), len(content))
        self.assertEqual(resumed.headers["Upload-State"], "primary_verified")
        repeated_complete = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(
            repeated_complete.status_code, 200, repeated_complete.get_data(as_text=True)
        )
        self.assertEqual(repeated_complete.get_json()["media_id"], media_id)
        with portal.db() as connection:
            row = connection.execute("SELECT * FROM photos WHERE id=?", (media_id,)).fetchone()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_media_records WHERE media_id=?", (media_id,)
                ).fetchone()[0],
                1,
            )
        self.assertEqual(row["owner_id"], "david@example.test")
        self.assertEqual(row["source_device_id"], paired["device_id"])
        self.assertEqual(row["ingestion_source"], "android_backup")
        self.assertEqual(row["content_sha256"], digest)
        self.assertEqual(self.client.get(f"/media/preview/{media_id}").status_code, 200)

    def test_android_append_releases_sqlite_writer_during_request_io(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        content = b"append-outside-sqlite-transaction"
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:append-concurrency",
                "original_filename": "append.bin",
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mime_type": "application/octet-stream",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        entered = threading.Event()
        release = threading.Event()
        response = []
        real_append = device_backup_module._append_request_to_descriptor

        def paused_append(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test append release timed out")
            return real_append(*args, **kwargs)

        def send_chunk():
            client = portal.app.test_client()
            response.append(
                client.patch(
                    f"/api/v1/device-backup/uploads/{upload_id}",
                    data=content,
                    headers={**auth, "Upload-Offset": "0"},
                )
            )

        with patch.object(
            device_backup_module,
            "_append_request_to_descriptor",
            side_effect=paused_append,
        ):
            worker = threading.Thread(target=send_chunk)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                writer = sqlite3.connect(portal.DB_PATH, timeout=0.25)
                try:
                    writer.execute("PRAGMA busy_timeout=250")
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        """INSERT INTO device_backup_events
                           (device_id,level,event_code,message,created_at)
                           VALUES (?,'info','append_concurrency_probe','probe',?)""",
                        (paired["device_id"], datetime.now(timezone.utc).isoformat()),
                    )
                    writer.commit()
                finally:
                    writer.close()
                competing = portal.app.test_client().patch(
                    f"/api/v1/device-backup/uploads/{upload_id}",
                    data=b"x",
                    headers={**auth, "Upload-Offset": "0"},
                )
                self.assertEqual(competing.status_code, 429)
                self.assertEqual(competing.get_json()["error"]["code"], "upload_busy")
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0].status_code, 204, response[0].get_data(as_text=True))
        self.assertEqual(int(response[0].headers["Upload-Offset"]), len(content))

    def test_android_complete_releases_sqlite_writer_during_file_hash(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        image = BytesIO()
        portal.Image.new("RGB", (10, 8), "seagreen").save(image, "PNG")
        content = image.getvalue()
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:complete-concurrency",
                "original_filename": "complete.png",
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "mime_type": "image/png",
            },
            headers=auth,
        )
        upload_id = created.get_json()["upload_id"]
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{upload_id}",
                data=content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )
        entered = threading.Event()
        release = threading.Event()
        response = []
        real_hash = device_backup_module._sha256_upload_descriptor

        def paused_hash(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test hash release timed out")
            return real_hash(*args, **kwargs)

        def complete():
            client = portal.app.test_client()
            response.append(
                client.post(
                    f"/api/v1/device-backup/uploads/{upload_id}/complete",
                    headers=auth,
                )
            )

        with patch.object(
            device_backup_module,
            "_sha256_upload_descriptor",
            side_effect=paused_hash,
        ):
            worker = threading.Thread(target=complete)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                writer = sqlite3.connect(portal.DB_PATH, timeout=0.25)
                try:
                    writer.execute("PRAGMA busy_timeout=250")
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        """INSERT INTO device_backup_events
                           (device_id,level,event_code,message,created_at)
                           VALUES (?,'info','complete_concurrency_probe','probe',?)""",
                        (paired["device_id"], datetime.now(timezone.utc).isoformat()),
                    )
                    writer.commit()
                finally:
                    writer.close()
                competing = portal.app.test_client().post(
                    f"/api/v1/device-backup/uploads/{upload_id}/complete",
                    headers=auth,
                )
                self.assertEqual(competing.status_code, 429)
                self.assertEqual(competing.get_json()["error"]["code"], "upload_busy")
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(response), 1)
        self.assertEqual(response[0].status_code, 200, response[0].get_data(as_text=True))

    def test_android_abandon_adopts_rename_before_database_commit_crash(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        payload = {
            "client_item_id": "image:abandon-crash-window",
            "original_filename": "abandon.jpg",
            "byte_size": 32,
            "sha256": "a" * 64,
            "mime_type": "image/jpeg",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        with portal.db() as connection:
            row = connection.execute(
                "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            part = Path(row["part_path"])
            connection.execute(
                """UPDATE device_uploads
                   SET io_lease_token='crashed-abandon',io_lease_kind='abandon',
                       io_lease_expires_at='2999-01-01T00:00:00+00:00'
                   WHERE id=?""",
                (upload_id,),
            )
        retiring = part.with_name(
            f"{part.name}.retiring-abandon-"
            f"{hashlib.sha256(upload_id.encode('utf-8')).hexdigest()[:16]}"
        )
        os.replace(part, retiring)

        recovered = self.client.delete(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(recovered.status_code, 204, recovered.get_data(as_text=True))
        self.assertFalse(part.exists())
        self.assertFalse(retiring.exists())
        with portal.db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()
            )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
            ).status_code,
            204,
        )

    def test_android_concurrent_abandon_does_not_steal_live_lease(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        payload = {
            "client_item_id": "image:concurrent-abandon",
            "original_filename": "concurrent-abandon.jpg",
            "byte_size": 64,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        with portal.db() as connection:
            part = Path(
                connection.execute(
                    "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()["part_path"]
            )
        retiring = part.with_name(
            f"{part.name}.retiring-abandon-"
            f"{hashlib.sha256(upload_id.encode('utf-8')).hexdigest()[:16]}"
        )
        entered = threading.Event()
        release = threading.Event()
        first_response = []
        call_lock = threading.Lock()
        call_count = 0
        real_open = device_backup_module._open_locked_upload_part

        def paused_first_open(*args, **kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                this_call = call_count
            if this_call == 1:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test abandon release timed out")
            return real_open(*args, **kwargs)

        def abandon_first():
            client = portal.app.test_client()
            first_response.append(
                client.delete(
                    f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
                )
            )

        with patch.object(
            device_backup_module,
            "_open_locked_upload_part",
            side_effect=paused_first_open,
        ):
            worker = threading.Thread(target=abandon_first)
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=3))
                competing = portal.app.test_client().delete(
                    f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
                )
                self.assertEqual(competing.status_code, 429)
                self.assertEqual(
                    competing.get_json()["error"]["code"], "upload_busy"
                )
                self.assertEqual(competing.headers["Retry-After"], "2")
            finally:
                release.set()
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(first_response), 1)
        self.assertEqual(
            first_response[0].status_code,
            204,
            first_response[0].get_data(as_text=True),
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
            ).status_code,
            204,
        )
        self.assertFalse(part.exists())
        self.assertFalse(retiring.exists())
        with portal.db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()
            )

    def test_android_changed_same_size_source_cannot_reuse_full_old_session(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        old_image = BytesIO()
        new_image = BytesIO()
        portal.Image.new("RGB", (8, 8), "navy").save(old_image, "PNG")
        portal.Image.new("RGB", (8, 8), "teal").save(new_image, "PNG")
        old_content = old_image.getvalue()
        new_content = new_image.getvalue()
        self.assertEqual(len(old_content), len(new_content))
        self.assertNotEqual(old_content, new_content)
        client_item_id = "image:lost-server-id-changed-same-size"
        old_hash = hashlib.sha256(old_content).hexdigest()
        new_hash = hashlib.sha256(new_content).hexdigest()
        old_payload = {
            "client_item_id": client_item_id,
            "original_filename": "old.png",
            "byte_size": len(old_content),
            "sha256": old_hash,
            "mime_type": "image/png",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=old_payload, headers=auth
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        old_upload_id = created.get_json()["upload_id"]
        uploaded = self.client.patch(
            f"/api/v1/device-backup/uploads/{old_upload_id}",
            data=old_content,
            headers={**auth, "Upload-Offset": "0"},
        )
        self.assertEqual(uploaded.status_code, 204, uploaded.get_data(as_text=True))

        new_payload = {
            **old_payload,
            "original_filename": "new.png",
            "sha256": new_hash,
        }
        conflict = self.client.post(
            "/api/v1/device-backup/uploads", json=new_payload, headers=auth
        )
        self.assertEqual(conflict.status_code, 409, conflict.get_data(as_text=True))
        self.assertEqual(
            conflict.get_json()["error"]["code"], "upload_session_conflict"
        )
        self.assertEqual(conflict.headers["Upload-Id"], old_upload_id)
        with portal.db() as connection:
            old_session = connection.execute(
                "SELECT * FROM device_uploads WHERE id=?", (old_upload_id,)
            ).fetchone()
            self.assertEqual(old_session["state"], "uploading")
            self.assertEqual(old_session["expected_sha256"], old_hash)
            self.assertEqual(old_session["expected_size"], len(old_content))
            self.assertEqual(old_session["accepted_offset"], len(old_content))
            old_part = Path(old_session["part_path"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_media_records WHERE device_id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_ingest_intents WHERE device_id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                0,
            )
        self.assertEqual(old_part.read_bytes(), old_content)

        # The conflict exposes only this authenticated device's stale session
        # ID.  Once its durable client recovery abandons that session, a retry
        # receives a fresh identity and can publish only the newly hashed bytes.
        abandoned = self.client.delete(
            f"/api/v1/device-backup/uploads/{conflict.headers['Upload-Id']}",
            headers=auth,
        )
        self.assertEqual(abandoned.status_code, 204, abandoned.get_data(as_text=True))
        recreated = self.client.post(
            "/api/v1/device-backup/uploads", json=new_payload, headers=auth
        )
        self.assertEqual(recreated.status_code, 201, recreated.get_data(as_text=True))
        new_upload_id = recreated.get_json()["upload_id"]
        self.assertNotEqual(new_upload_id, old_upload_id)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{new_upload_id}",
                data=new_content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )
        completed = self.client.post(
            f"/api/v1/device-backup/uploads/{new_upload_id}/complete", headers=auth
        )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        with portal.db() as connection:
            record = connection.execute(
                """SELECT content_sha256,byte_size FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], client_item_id),
            ).fetchone()
            self.assertEqual(tuple(record), (new_hash, len(new_content)))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM photos WHERE content_sha256=?", (old_hash,)
                ).fetchone()[0],
                0,
            )

    def test_android_lost_retryable_session_id_uses_authenticated_cleanup_contract(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        image = BytesIO()
        portal.Image.new("RGB", (9, 9), "purple").save(image, "PNG")
        content = image.getvalue()
        actual_hash = hashlib.sha256(content).hexdigest()
        payload = {
            "client_item_id": "image:lost-retryable-session",
            "original_filename": "retryable.png",
            "byte_size": len(content),
            "sha256": "d" * 64,
            "mime_type": "image/png",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        upload_id = created.get_json()["upload_id"]
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{upload_id}",
                data=content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )
        mismatch = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(mismatch.status_code, 422)
        self.assertEqual(mismatch.get_json()["error"]["code"], "sha256_mismatch")

        for retry_payload in (payload, {**payload, "sha256": actual_hash}):
            conflict = self.client.post(
                "/api/v1/device-backup/uploads", json=retry_payload, headers=auth
            )
            self.assertEqual(conflict.status_code, 409, conflict.get_data(as_text=True))
            self.assertEqual(
                conflict.get_json()["error"]["code"], "upload_session_conflict"
            )
            self.assertEqual(conflict.headers["Upload-Id"], upload_id)
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()[0],
                "retryable_error",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_media_records WHERE device_id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                0,
            )

        self.assertEqual(
            self.client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
            ).status_code,
            204,
        )
        fresh_payload = {**payload, "sha256": actual_hash}
        recreated = self.client.post(
            "/api/v1/device-backup/uploads", json=fresh_payload, headers=auth
        )
        self.assertEqual(recreated.status_code, 201, recreated.get_data(as_text=True))
        fresh_id = recreated.get_json()["upload_id"]
        self.assertNotEqual(fresh_id, upload_id)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{fresh_id}",
                data=content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )
        completed = self.client.post(
            f"/api/v1/device-backup/uploads/{fresh_id}/complete", headers=auth
        )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))

    def test_android_permanent_session_create_is_typed_for_same_and_changed_content(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        content = b"definitely-not-a-real-jpeg"
        changed_content = b"DEFINITELY-NOT-A-REAL-JPEG"
        self.assertEqual(len(content), len(changed_content))
        payload = {
            "client_item_id": "image:lost-permanent-session",
            "original_filename": "invalid.jpg",
            "byte_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mime_type": "image/jpeg",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        upload_id = created.get_json()["upload_id"]
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{upload_id}",
                data=content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )
        rejected = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(rejected.status_code, 422, rejected.get_data(as_text=True))
        self.assertEqual(rejected.get_json()["error"]["code"], "invalid_media")

        repeated = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        self.assertEqual(repeated.status_code, 422, repeated.get_data(as_text=True))
        self.assertEqual(repeated.get_json()["error"]["code"], "invalid_media")
        changed = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                **payload,
                "sha256": hashlib.sha256(changed_content).hexdigest(),
            },
            headers=auth,
        )
        self.assertEqual(changed.status_code, 422, changed.get_data(as_text=True))
        self.assertEqual(
            changed.get_json()["error"]["code"], "local_source_changed"
        )
        self.assertNotIn("Upload-Id", changed.headers)
        with portal.db() as connection:
            row = connection.execute(
                "SELECT state,error_code,part_path FROM device_uploads WHERE id=?",
                (upload_id,),
            ).fetchone()
            self.assertEqual(tuple(row), ("permanent_error", "invalid_media", None))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_media_records WHERE device_id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                0,
            )

    def test_android_missing_staging_session_is_typed_and_recreated(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        payload = {
            "client_item_id": "image:missing-staging-recovery",
            "original_filename": "missing.jpg",
            "byte_size": 40,
            "sha256": "b" * 64,
            "mime_type": "image/jpeg",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        upload_id = created.get_json()["upload_id"]
        with portal.db() as connection:
            part = Path(
                connection.execute(
                    "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()["part_path"]
            )
        part.unlink()

        missing = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(
            missing.headers["Upload-Error-Code"], "upload_session_missing"
        )
        recreated = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        self.assertEqual(recreated.status_code, 201, recreated.get_data(as_text=True))
        self.assertNotEqual(recreated.get_json()["upload_id"], upload_id)
        with portal.db() as connection:
            rows = connection.execute(
                """SELECT id FROM device_uploads
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], payload["client_item_id"]),
            ).fetchall()
        self.assertEqual([row["id"] for row in rows], [recreated.get_json()["upload_id"]])

    def test_android_publication_is_atomic_and_retryable_across_finalize_crash(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        image = BytesIO()
        portal.Image.new("RGB", (12, 10), "navy").save(image, "PNG")
        content = image.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "android-atomic-finalize",
                "original_filename": "atomic.png",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "image/png",
            },
            headers=auth,
        )
        upload_id = created.get_json()["upload_id"]
        self.assertEqual(
            self.client.patch(
                f"/api/v1/device-backup/uploads/{upload_id}",
                data=content,
                headers={**auth, "Upload-Offset": "0"},
            ).status_code,
            204,
        )

        with patch.object(portal, "audit_mutation", side_effect=RuntimeError("crash")):
            interrupted = self.client.post(
                f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
            )
        self.assertEqual(interrupted.status_code, 500)
        with portal.db() as connection:
            upload = connection.execute(
                "SELECT state,media_id FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            self.assertEqual(upload["state"], "ingesting")
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM photos WHERE id=?", (upload["media_id"],)
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM device_media_records WHERE media_id=?",
                    (upload["media_id"],),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM device_ingest_intents WHERE id=?",
                    (upload["media_id"],),
                ).fetchone()["state"],
                "prepared",
            )
            rollback_guard = connection.execute(
                """SELECT requires_live_validator,requires_publication_callback
                   FROM media_publish_intents WHERE id=?""",
                (upload["media_id"],),
            ).fetchone()
            # A previous release skips every live-validator intent at startup;
            # dual marking keeps callback-bound device media safe on rollback.
            self.assertEqual(tuple(rollback_guard), (1, 1))

        resumable = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(resumable.status_code, 204)
        self.assertEqual(resumable.headers["Upload-State"], "ingesting")
        self.assertEqual(int(resumable.headers["Upload-Offset"]), len(content))

        recovered = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
        media_id = recovered.get_json()["media_id"]
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM photos WHERE id=?", (media_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_media_records WHERE media_id=?",
                    (media_id,),
                ).fetchone()[0],
                1,
            )
            device = connection.execute(
                "SELECT uploaded_items,uploaded_bytes FROM backup_devices WHERE id=?",
                (paired["device_id"],),
            ).fetchone()
            self.assertEqual(tuple(device), (1, len(content)))

    def test_android_visible_dedupe_survives_lost_response_without_orphan(self):
        image = BytesIO()
        portal.Image.new("RGB", (9, 7), "gold").save(image, "PNG")
        content = image.getvalue()
        seeded = self.client.post(
            "/api/upload",
            data={"media": (BytesIO(content), "shared-seed.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(seeded.status_code, 200)
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        payload = {
            "client_item_id": "android-dedupe-lost-response",
            "original_filename": "shared-copy.png",
            "byte_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mime_type": "image/png",
        }
        with patch.object(portal, "ensure_viewer_preview", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.client.post(
                    "/api/v1/device-backup/uploads", json=payload, headers=auth
                )

        repeated = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=auth
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.get_json()["state"], "already_present")
        with portal.db() as connection:
            records = connection.execute(
                """SELECT media_id FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], payload["client_item_id"]),
            ).fetchall()
            self.assertEqual(len(records), 1)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM photos WHERE id=?", (records[0]["media_id"],)
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT uploaded_items FROM backup_devices WHERE id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                1,
            )

        changed_hash = hashlib.sha256(content + b"x").hexdigest()
        for changed_payload in (
            {**payload, "byte_size": len(content) + 1},
            {**payload, "sha256": changed_hash},
            {
                **payload,
                "byte_size": len(content) + 1,
                "sha256": changed_hash,
            },
        ):
            changed = self.client.post(
                "/api/v1/device-backup/uploads",
                json=changed_payload,
                headers=auth,
            )
            self.assertEqual(changed.status_code, 422)
            self.assertEqual(
                changed.get_json()["error"]["code"], "local_source_changed"
            )
        with portal.db() as connection:
            unchanged = connection.execute(
                """SELECT content_sha256,byte_size FROM device_media_records
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], payload["client_item_id"]),
            ).fetchone()
        self.assertEqual(unchanged["content_sha256"], payload["sha256"])
        self.assertEqual(unchanged["byte_size"], len(content))

    def test_android_inflight_intent_rejects_changed_same_size_generation(self):
        old_image = BytesIO()
        new_image = BytesIO()
        portal.Image.new("RGB", (8, 8), "navy").save(old_image, "PNG")
        portal.Image.new("RGB", (8, 8), "teal").save(new_image, "PNG")
        old_content = old_image.getvalue()
        new_content = new_image.getvalue()
        self.assertEqual(len(old_content), len(new_content))
        old_hash = hashlib.sha256(old_content).hexdigest()
        new_hash = hashlib.sha256(new_content).hexdigest()
        seeded = self.client.post(
            "/api/upload",
            data={"media": (BytesIO(old_content), "intent-seed.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(seeded.status_code, 200)
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        client_item_id = "image:inflight-intent-generation"
        old_payload = {
            "client_item_id": client_item_id,
            "original_filename": "old-generation.png",
            "byte_size": len(old_content),
            "sha256": old_hash,
            "mime_type": "image/png",
            "capture_timestamp": "2026-01-01T00:00:00Z",
        }
        with portal.db() as connection:
            device_backup_module.prepare_device_ingest_intent(
                connection,
                device_id=paired["device_id"],
                client_item_id=client_item_id,
                upload_id=None,
                original_filename=old_payload["original_filename"],
                content_sha256=old_hash,
                byte_size=len(old_content),
                mime_type=old_payload["mime_type"],
                capture_timestamp=old_payload["capture_timestamp"],
                ingestion_source="android_backup",
                now=datetime.now(timezone.utc).isoformat(),
            )

        changed = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                **old_payload,
                "original_filename": "new-generation.png",
                "sha256": new_hash,
            },
            headers=auth,
        )
        self.assertEqual(changed.status_code, 422, changed.get_data(as_text=True))
        self.assertEqual(
            changed.get_json()["error"]["code"], "local_source_changed"
        )
        self.assertNotIn("Upload-Id", changed.headers)
        with portal.db() as connection:
            intent = connection.execute(
                """SELECT content_sha256,byte_size,original_filename,state
                   FROM device_ingest_intents
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], client_item_id),
            ).fetchone()
            self.assertEqual(
                tuple(intent),
                (old_hash, len(old_content), "old-generation.png", "prepared"),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM device_media_records
                       WHERE device_id=? AND client_item_id=?""",
                    (paired["device_id"], client_item_id),
                ).fetchone()[0],
                0,
            )

        # The phone's content-bound generation re-key can now create an
        # independent upload without mutating or accepting the old intent.
        rekeyed = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                **old_payload,
                "client_item_id": f"{client_item_id}:sha256:{new_hash}",
                "original_filename": "new-generation.png",
                "sha256": new_hash,
            },
            headers=auth,
        )
        self.assertEqual(rekeyed.status_code, 201, rekeyed.get_data(as_text=True))

    def test_android_inflight_intent_resumes_with_frozen_metadata(self):
        image = BytesIO()
        portal.Image.new("RGB", (8, 8), "navy").save(image, "PNG")
        content = image.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        seeded = self.client.post(
            "/api/upload",
            data={"media": (BytesIO(content), "metadata-seed.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(seeded.status_code, 200)
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        payload = {
            "client_item_id": "image:inflight-intent-metadata",
            "original_filename": "frozen-name.png",
            "byte_size": len(content),
            "sha256": digest,
            "mime_type": "image/png",
            "capture_timestamp": "2026-01-01T00:00:00Z",
        }
        with portal.db() as connection:
            device_backup_module.prepare_device_ingest_intent(
                connection,
                device_id=paired["device_id"],
                client_item_id=payload["client_item_id"],
                upload_id=None,
                original_filename=payload["original_filename"],
                content_sha256=digest,
                byte_size=len(content),
                mime_type=payload["mime_type"],
                capture_timestamp=payload["capture_timestamp"],
                ingestion_source="android_backup",
                now=datetime.now(timezone.utc).isoformat(),
            )

        resumed = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                **payload,
                "original_filename": "drifted-name.jpg",
                "mime_type": "image/jpeg",
                "capture_timestamp": "2030-12-31T23:59:59Z",
            },
            headers=auth,
        )
        self.assertEqual(resumed.status_code, 200, resumed.get_data(as_text=True))
        self.assertEqual(resumed.get_json()["state"], "already_present")
        with portal.db() as connection:
            intent = connection.execute(
                """SELECT id,original_filename,mime_type,capture_timestamp,
                          ingestion_source,content_sha256,byte_size
                   FROM device_ingest_intents
                   WHERE device_id=? AND client_item_id=?""",
                (paired["device_id"], payload["client_item_id"]),
            ).fetchone()
            self.assertEqual(intent["original_filename"], "frozen-name.png")
            self.assertEqual(intent["mime_type"], "image/png")
            self.assertEqual(intent["capture_timestamp"], "2026-01-01T00:00:00Z")
            self.assertEqual(intent["ingestion_source"], "android_backup")
            self.assertEqual(intent["content_sha256"], digest)
            self.assertEqual(intent["byte_size"], len(content))
            record = connection.execute(
                """SELECT original_filename,capture_timestamp,ingestion_source,
                          content_sha256,byte_size
                   FROM device_media_records WHERE media_id=?""",
                (intent["id"],),
            ).fetchone()
            self.assertEqual(
                tuple(record),
                (
                    "frozen-name.png",
                    "2026-01-01T00:00:00Z",
                    "android_backup",
                    digest,
                    len(content),
                ),
            )

    def test_android_hash_mismatch_session_can_be_safely_reset_without_cross_device_access(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        other_client, other_csrf = self.paired_client(profile="diana")
        other = self.pair_backup_device(
            client=other_client,
            owner="diana@example.test",
            name="Diana",
            csrf=other_csrf,
        )
        other_auth = {"Authorization": f"Bearer {other['device_credential']}"}
        image = BytesIO()
        from PIL import Image
        Image.new("RGB", (18, 14), "teal").save(image, "PNG")
        changed_content = image.getvalue()
        changed_hash = hashlib.sha256(changed_content).hexdigest()
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:changed-in-place",
                "original_filename": "changed.png",
                "byte_size": len(changed_content),
                "sha256": "a" * 64,
                "mime_type": "image/png",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        accepted = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=changed_content,
            headers={**auth, "Upload-Offset": "0"},
        )
        self.assertEqual(accepted.status_code, 204)
        mismatch = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(mismatch.status_code, 422)
        self.assertEqual(mismatch.get_json()["error"]["code"], "sha256_mismatch")
        failed_head = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(failed_head.status_code, 422)
        self.assertEqual(failed_head.headers["Upload-Error-Code"], "sha256_mismatch")
        with portal.db() as connection:
            staged = Path(connection.execute(
                "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()["part_path"])
        self.assertTrue(staged.is_file())

        other_payload = {
            "client_item_id": "image:waiting-behind-changed-item",
            "original_filename": "waiting.png",
            "byte_size": 19,
            "sha256": "b" * 64,
            "mime_type": "image/png",
        }
        # A different paired device gets the same idempotent response but cannot
        # delete or even distinguish the first device's private staging session.
        self.assertEqual(
            other_client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=other_auth
            ).status_code,
            204,
        )
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()[0],
                1,
            )
        self.assertTrue(staged.is_file())

        self.assertEqual(
            self.client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
            ).status_code,
            204,
        )
        self.assertFalse(staged.exists())
        unblocked = other_client.post(
            "/api/v1/device-backup/uploads", json=other_payload, headers=other_auth
        )
        self.assertEqual(unblocked.status_code, 201, unblocked.get_data(as_text=True))
        self.assertEqual(
            other_client.delete(
                f"/api/v1/device-backup/uploads/{unblocked.get_json()['upload_id']}",
                headers=other_auth,
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
            ).status_code,
            204,
        )
        restarted = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:changed-in-place",
                "original_filename": "changed.png",
                "byte_size": len(changed_content),
                "sha256": changed_hash,
                "mime_type": "image/png",
            },
            headers=auth,
        )
        self.assertEqual(restarted.status_code, 201, restarted.get_data(as_text=True))
        self.assertNotEqual(restarted.get_json()["upload_id"], upload_id)

    def test_stale_upload_housekeeping_retires_database_slot_and_partial_file(self):
        first = self.pair_backup_device()
        first_auth = {"Authorization": f"Bearer {first['device_credential']}"}
        other_client, other_csrf = self.paired_client(profile="diana")
        other = self.pair_backup_device(
            client=other_client,
            owner="diana@example.test",
            name="Diana",
            csrf=other_csrf,
        )
        other_auth = {"Authorization": f"Bearer {other['device_credential']}"}
        payload = {
            "client_item_id": "image:stale-slot",
            "original_filename": "stale.jpg",
            "byte_size": 101,
            "sha256": "c" * 64,
            "mime_type": "image/jpeg",
        }
        created = self.client.post(
            "/api/v1/device-backup/uploads", json=payload, headers=first_auth
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        upload_id = created.get_json()["upload_id"]
        with portal.db() as connection:
            row = connection.execute(
                "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()
            staged = Path(row["part_path"])
            connection.execute(
                "UPDATE device_uploads SET updated_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), upload_id),
            )
        self.assertTrue(staged.is_file())
        blocked = other_client.post(
            "/api/v1/device-backup/uploads",
            json={**payload, "client_item_id": "image:after-stale", "sha256": "d" * 64},
            headers=other_auth,
        )
        self.assertEqual(blocked.status_code, 429)

        retired = device_backup_module.cleanup_stale_upload_sessions(
            portal.db,
            Path(TEST_DATA.name) / "incoming" / "device-backup",
        )

        self.assertEqual(retired, 1)
        self.assertFalse(staged.exists())
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()[0],
                0,
            )
        unblocked = other_client.post(
            "/api/v1/device-backup/uploads",
            json={**payload, "client_item_id": "image:after-stale", "sha256": "d" * 64},
            headers=other_auth,
        )
        self.assertEqual(unblocked.status_code, 201, unblocked.get_data(as_text=True))

    def test_android_video_uses_existing_playback_pipeline_and_rejects_wrong_offset(self):
        paired = self.pair_backup_device()
        content = b"original-video-bytes"
        digest = __import__("hashlib").sha256(content).hexdigest()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        rejected_owner = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "video:202",
                "original_filename": "../../camera.mp4",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "video/mp4",
                "owner_user_id": "attacker@example.test",
                "visibility": "private",
            },
            headers=auth,
        )
        self.assertEqual(rejected_owner.status_code, 422)
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "video:202",
                "original_filename": "../../camera.mp4",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "video/mp4",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.get_json()["upload_id"]
        mismatch = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content,
            headers={**auth, "Upload-Offset": "4"},
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.headers["Upload-Offset"], "0")
        accepted = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content,
            headers={**auth, "Upload-Offset": "0"},
        )
        self.assertEqual(accepted.status_code, 204)

        def prepare(path, photo_id, extension):
            return self.staged_video_derivatives(photo_id, playback=b"playable")

        with patch.object(portal, "prepare_video", side_effect=prepare):
            completed = self.client.post(
                f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
            )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        media_id = completed.get_json()["media_id"]
        with portal.db() as connection:
            row = connection.execute("SELECT * FROM photos WHERE id=?", (media_id,)).fetchone()
        self.assertEqual(row["owner_id"], "david@example.test")
        self.assertEqual(row["original_name"], "camera.mp4")
        self.assertTrue(row["stored_path"].endswith(".mp4"))
        self.assertEqual(self.client.get(f"/media/play/{media_id}").data, b"playable")

    def test_media_original_serves_open_descriptor_across_final_entry_swap(self):
        self.add_photo(
            "descriptor-media", owner_id="david@example.test", owner_name="David"
        )
        saved = b"saved-media"
        outside_bytes = b"outside-leak"
        object_path = portal.ORIGINALS / "descriptor-media.jpg"
        parked = portal.ORIGINALS / "parked-descriptor-media.jpg"
        outside = Path(TEST_DATA.name) / "outside-media.jpg"
        object_path.write_bytes(saved)
        outside.write_bytes(outside_bytes)
        with portal.db() as connection:
            connection.execute(
                "UPDATE photos SET byte_size=? WHERE id='descriptor-media'",
                (len(saved),),
            )
        original_open = portal.ORIGINAL_STORAGE.open_regular_path
        swapped = False

        def open_then_swap(*args, **kwargs):
            nonlocal swapped
            result = original_open(*args, **kwargs)
            if not swapped:
                object_path.rename(parked)
                object_path.symlink_to(outside)
                swapped = True
            return result

        try:
            with patch.object(
                portal.ORIGINAL_STORAGE,
                "open_regular_path",
                side_effect=open_then_swap,
            ):
                response = self.client.get("/media/original/descriptor-media")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, saved)
            self.assertNotEqual(response.data, outside_bytes)
        finally:
            if object_path.is_symlink():
                object_path.unlink()
            if parked.exists():
                parked.rename(object_path)
            outside.unlink(missing_ok=True)

    def test_descriptor_safe_video_serving_preserves_byte_ranges(self):
        self.add_photo(
            "range-video", owner_id="david@example.test", owner_name="David"
        )
        playback = b"0123456789"
        (portal.PREVIEWS / "range-video.mp4").write_bytes(playback)
        with portal.db() as connection:
            connection.execute(
                """UPDATE photos SET content_type='video/mp4',playback_name=?
                   WHERE id='range-video'""",
                ("range-video.mp4",),
            )

        metadata = self.client.head("/media/play/range-video")
        self.assertEqual(metadata.status_code, 200)
        self.assertEqual(metadata.headers["Content-Length"], "10")
        self.assertTrue(metadata.headers["ETag"])
        self.assertEqual(
            self.client.head("/media/play/range-video").headers["ETag"],
            metadata.headers["ETag"],
        )
        response = self.client.get(
            "/media/play/range-video",
            headers={
                "Range": "bytes=2-5",
                "If-Range": metadata.headers["ETag"],
            },
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"2345")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(response.headers["Accept-Ranges"], "bytes")

    def test_media_renderer_writes_only_to_exclusive_pinned_descriptor(self):
        rendered = b"descriptor-render"
        outside = Path(TEST_DATA.name) / "renderer-outside"
        outside.mkdir()
        parked = Path(TEST_DATA.name) / "parked-previews"
        target = portal.PREVIEWS / "descriptor-render.mp4"

        def swap_root_then_render(arguments):
            portal.PREVIEWS.rename(parked)
            portal.PREVIEWS.symlink_to(outside, target_is_directory=True)
            Path(arguments[-1]).write_bytes(rendered)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        try:
            with patch.object(
                portal, "run_media_command", side_effect=swap_root_then_render
            ):
                with self.assertRaises(portal.StorageSafetyError):
                    portal._run_descriptor_media_output(
                        portal.PREVIEW_STORAGE,
                        target.name,
                        ["ffmpeg", "-y"],
                        "mp4",
                    )
            self.assertFalse((outside / target.name).exists())
            self.assertEqual((parked / target.name).read_bytes(), rendered)
        finally:
            if portal.PREVIEWS.is_symlink():
                portal.PREVIEWS.unlink()
            if parked.exists():
                parked.rename(portal.PREVIEWS)
            target.unlink(missing_ok=True)

    def test_video_derivatives_are_rendered_through_pinned_descriptors(self):
        media_id = "descriptor-video-derivatives"
        outputs = []
        artifacts = []
        preview_bytes = BytesIO()
        portal.Image.new("RGB", (8, 6), "orange").save(
            preview_bytes, "JPEG"
        )

        def render_or_probe(arguments):
            if arguments[0] == "ffprobe":
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"streams": [
                        {"codec_type": "video", "codec_name": "h264"},
                        {"codec_type": "audio", "codec_name": "aac"},
                    ]}),
                    stderr="",
                )
            self.assertRegex(str(arguments[-1]), r"^/proc/self/fd/\d+$")
            outputs.append((arguments[-2], str(arguments[-1])))
            content = (
                preview_bytes.getvalue()
                if arguments[-2] == "image2"
                else b"descriptor-playback"
            )
            Path(arguments[-1]).write_bytes(content)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        try:
            with patch.object(
                portal, "run_media_command", side_effect=render_or_probe
            ):
                preview, thumb, playback, artifacts = portal.prepare_video(
                    "/proc/self/fd/999", media_id, ".mov"
                )
            self.assertEqual(outputs[0][0], "image2")
            self.assertEqual(outputs[1][0], "mp4")
            self.assertFalse((portal.PREVIEWS / preview).exists())
            self.assertFalse((portal.THUMBS / thumb).exists())
            self.assertFalse((portal.PREVIEWS / playback).exists())
            self.assertEqual(
                [artifact["kind"] for artifact in artifacts],
                ["preview", "thumb", "playback"],
            )
            playback_artifact = artifacts[-1]
            descriptor, _metadata = portal.DATA_STORAGE.open_regular_path(
                playback_artifact["source_path"]
            )
            try:
                self.assertEqual(os.read(descriptor, 100), b"descriptor-playback")
            finally:
                os.close(descriptor)
        finally:
            for artifact in artifacts:
                portal._unlink_staged_artifact(artifact["source_path"])

    def test_media_original_stays_on_pinned_nested_ancestor_during_swap(self):
        self.add_photo(
            "nested-media", owner_id="david@example.test", owner_name="David"
        )
        year = portal.ORIGINALS / "2026"
        month = year / "08"
        outside = Path(TEST_DATA.name) / "outside-tree"
        outside_month = outside / "08"
        for directory in (year, month, outside, outside_month):
            portal.ensure_restricted_directory(directory)
        saved = b"nested-saved"
        (month / "nested.jpg").write_bytes(saved)
        (outside_month / "nested.jpg").write_bytes(b"outside-leak")
        with portal.db() as connection:
            connection.execute(
                """UPDATE photos SET stored_path='2026/08/nested.jpg',byte_size=?
                   WHERE id='nested-media'""",
                (len(saved),),
            )
        parked = portal.ORIGINALS / "parked-2026"
        real_stat = os.stat
        swapped = False

        def stat_then_swap(path, *args, **kwargs):
            nonlocal swapped
            metadata = real_stat(path, *args, **kwargs)
            if path == "2026" and kwargs.get("dir_fd") is not None and not swapped:
                year.rename(parked)
                year.symlink_to(outside, target_is_directory=True)
                swapped = True
            return metadata

        try:
            with patch.object(
                secure_storage_module.os, "stat", side_effect=stat_then_swap
            ):
                response = self.client.get("/media/original/nested-media")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, saved)
        finally:
            if year.is_symlink():
                year.unlink()
            if parked.exists():
                parked.rename(year)

    def test_media_serving_routes_never_follow_final_entry_symlinks(self):
        self.add_photo(
            "symlink-video", owner_id="david@example.test", owner_name="David"
        )
        names = {
            portal.ORIGINALS / "symlink-video.jpg": b"original!!",
            portal.PREVIEWS / "symlink-video.jpg": b"preview",
            portal.THUMBS / "symlink-video.jpg": b"thumb",
            portal.PREVIEWS / "symlink-video.mp4": b"playback",
        }
        for path, content in names.items():
            path.write_bytes(content)
        with portal.db() as connection:
            connection.execute(
                """UPDATE photos SET content_type='video/mp4',playback_name=?,byte_size=?
                   WHERE id='symlink-video'""",
                ("symlink-video.mp4", len(names[portal.ORIGINALS / "symlink-video.jpg"])),
            )
        outside = Path(TEST_DATA.name) / "outside-route-media"
        outside.write_bytes(b"PRIVATE-OUTSIDE")
        cases = (
            (portal.THUMBS / "symlink-video.jpg", "/media/thumb/symlink-video"),
            (portal.PREVIEWS / "symlink-video.jpg", "/media/preview/symlink-video"),
            (portal.PREVIEWS / "symlink-video.jpg", "/media/view/symlink-video"),
            (portal.ORIGINALS / "symlink-video.jpg", "/media/original/symlink-video"),
            (portal.PREVIEWS / "symlink-video.mp4", "/media/play/symlink-video"),
        )
        try:
            for target, route in cases:
                parked = target.with_name(f"parked-{target.name}")
                target.rename(parked)
                target.symlink_to(outside)
                try:
                    response = self.client.get(route)
                    with self.subTest(route=route):
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn(outside.read_bytes(), response.data)
                finally:
                    target.unlink()
                    parked.rename(target)
            with portal.db() as connection:
                connection.execute(
                    """UPDATE photos SET deleted_at='2026-01-01T00:00:00+00:00'
                       WHERE id='symlink-video'"""
                )
            deleted_cases = (
                (portal.THUMBS / "symlink-video.jpg", "thumb"),
                (portal.PREVIEWS / "symlink-video.jpg", "preview"),
                (portal.PREVIEWS / "symlink-video.jpg", "view"),
                (portal.ORIGINALS / "symlink-video.jpg", "original"),
                (portal.PREVIEWS / "symlink-video.mp4", "play"),
            )
            for target, kind in deleted_cases:
                parked = target.with_name(f"parked-{target.name}")
                target.rename(parked)
                target.symlink_to(outside)
                try:
                    response = self.client.get(
                        f"/media/deleted/{kind}/symlink-video"
                    )
                    with self.subTest(deleted_kind=kind):
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn(outside.read_bytes(), response.data)
                finally:
                    target.unlink()
                    parked.rename(target)
        finally:
            outside.unlink(missing_ok=True)

    def test_android_unreadable_image_is_permanent_and_staging_is_removed(self):
        paired = self.pair_backup_device()
        content = b"17ded695-0a2e-4f24-b785-48fb045d"
        digest = __import__("hashlib").sha256(content).hexdigest()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:unreadable",
                "original_filename": "placeholder.png",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "image/png",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.get_json()["upload_id"]
        accepted = self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content,
            headers={**auth, "Upload-Offset": "0"},
        )
        self.assertEqual(accepted.status_code, 204)
        rejected = self.client.post(
            f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.get_json()["error"]["code"], "invalid_media")
        with portal.db() as connection:
            row = connection.execute(
                "SELECT state,error_code,part_path FROM device_uploads WHERE id=?",
                (upload_id,),
            ).fetchone()
        self.assertEqual(row["state"], "permanent_error")
        self.assertEqual(row["error_code"], "invalid_media")
        self.assertIsNone(row["part_path"])
        resumed = self.client.head(
            f"/api/v1/device-backup/uploads/{upload_id}", headers=auth
        )
        self.assertEqual(resumed.status_code, 422)
        self.assertEqual(resumed.headers["Upload-Error-Code"], "invalid_media")

    def test_android_unreadable_video_is_skipped_and_derivatives_are_cleaned(self):
        existing_previews = set(portal.PREVIEWS.glob("*"))
        existing_thumbs = set(portal.THUMBS.glob("*"))
        existing_incoming = set(portal.INCOMING.glob("*"))
        paired = self.pair_backup_device()
        content = b"not-a-decodable-video"
        digest = __import__("hashlib").sha256(content).hexdigest()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "video:unreadable",
                "original_filename": "broken.mp4",
                "byte_size": len(content),
                "sha256": digest,
                "mime_type": "video/mp4",
            },
            headers=auth,
        )
        upload_id = created.get_json()["upload_id"]
        self.client.patch(
            f"/api/v1/device-backup/uploads/{upload_id}",
            data=content,
            headers={**auth, "Upload-Offset": "0"},
        )

        poster = BytesIO()
        portal.Image.new("RGB", (8, 6), "purple").save(poster, "JPEG")

        def fail_after_poster(arguments):
            if arguments[0] == "ffmpeg":
                Path(arguments[-1]).write_bytes(poster.getvalue())
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise ValueError("Conversion failed!")

        with patch.object(portal, "run_media_command", side_effect=fail_after_poster):
            rejected = self.client.post(
                f"/api/v1/device-backup/uploads/{upload_id}/complete", headers=auth
            )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.get_json()["error"]["code"], "invalid_media")
        with portal.db() as connection:
            upload = connection.execute(
                "SELECT state,part_path FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()
        self.assertEqual(upload["state"], "permanent_error")
        self.assertIsNone(upload["part_path"])
        self.assertEqual(set(portal.PREVIEWS.glob("*")), existing_previews)
        self.assertEqual(set(portal.THUMBS.glob("*")), existing_thumbs)
        self.assertEqual(set(portal.INCOMING.glob("*")), existing_incoming)

    def test_device_manifest_is_scoped_and_credentials_are_upload_only(self):
        david = self.pair_backup_device()
        diana_client, diana_csrf = self.paired_client("diana", "Diana")
        diana_client.environ_base["HTTP_X_CSRF_TOKEN"] = diana_csrf
        diana = self.pair_backup_device(
            diana_client, "diana@example.test", "Diana", diana_csrf
        )
        diana_media_id = self.add_photo_returning(
            "diana-device-photo", "diana@example.test"
        )
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO device_media_records
                   (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                    content_sha256,byte_size,primary_verification_state,
                    secondary_verification_state,ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "diana-record", diana["device_id"], "private-client-item", diana_media_id,
                    "diana@example.test", "private.jpg", "a" * 64, 10,
                    "primary_verified", "secondary_pending", "2026-01-01T00:00:00+00:00",
                ),
            )
        manifest = self.client.get(
            "/api/v1/device-backup/manifest",
            headers={"Authorization": f"Bearer {david['device_credential']}"},
        ).get_json()["items"]
        self.assertEqual(manifest, [])
        self.assertIn(
            self.client.delete(
                "/api/photos/diana-device-photo",
                json={"version": 1},
                headers={"Authorization": f"Bearer {david['device_credential']}"},
            ).status_code,
            (403, 404, 405),
        )

    def add_photo_returning(self, photo_id, owner_id):
        self.add_photo(photo_id, owner_id=owner_id, owner_name=owner_id.split("@")[0], visibility="private")
        return photo_id

    def test_identical_bytes_across_users_keep_independent_logical_records(self):
        staged_a = Path(TEST_DATA.name) / "first.png"
        staged_b = Path(TEST_DATA.name) / "second.png"
        from PIL import Image
        Image.new("RGB", (8, 8), "green").save(staged_a, "PNG")
        staged_b.write_bytes(staged_a.read_bytes())
        first = portal.canonical_ingest_media(
            staged_path=staged_a, original_filename="same.png", mime_type="image/png",
            owner_user_id="david@example.test", owner_name="David",
        )
        second = portal.canonical_ingest_media(
            staged_path=staged_b, original_filename="same.png", mime_type="image/png",
            owner_user_id="diana@example.test", owner_name="Diana",
            source_device_id="phone-two", ingestion_source="android_backup",
        )
        with portal.db() as connection:
            rows = connection.execute(
                "SELECT id,owner_id,stored_path,content_sha256 FROM photos ORDER BY owner_id"
            ).fetchall()
            connection.execute(
                "UPDATE photos SET deleted_at=? WHERE id=?",
                ("2026-01-02T00:00:00+00:00", first["id"]),
            )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stored_path"], rows[1]["stored_path"])
        physical = portal.ORIGINALS / rows[0]["stored_path"]
        self.assertTrue(portal.destroy_photo(first["id"]))
        self.assertTrue(physical.exists())
        diana_client, _ = self.paired_client("diana", "Diana")
        self.assertEqual(diana_client.get(f"/media/original/{second['id']}").status_code, 200)

    def test_places_restaurant_review_and_sorting_flow(self):
        added = self.client.post(
            "/api/places/restaurants",
            json={
                "name": "Test Kitchen",
                "cuisines": ["Italian", "Mexican"],
                "notes": "Try the special.",
            },
        )
        self.assertEqual(added.status_code, 201)
        restaurant = added.get_json()["restaurant"]
        self.assertEqual(restaurant["status"], "want_to_go")
        self.assertEqual(restaurant["added_by_id"], "david@example.test")
        reviewed = self.client.post(
            f"/api/places/restaurants/{restaurant['id']}/review",
            json={"rating": 4.5, "review": "Would go again.", "visited_at": "2026-07-29", "version": restaurant["version"]},
        )
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.get_json()["restaurant"]["rating"], 4.5)
        self.assertEqual(
            self.client.get("/api/places/restaurants?view=want_to_go").get_json()["restaurants"],
            [],
        )
        result = self.client.get(
            "/api/places/restaurants?view=reviewed&sort=rating"
        ).get_json()["restaurants"]
        self.assertEqual([item["name"] for item in result], ["Test Kitchen"])

    def test_places_rating_requires_half_star_steps_and_chooser_filters(self):
        first = self.client.post(
            "/api/places/restaurants",
            json={"name": "Breakfast Spot", "cuisines": ["Breakfast"]},
        ).get_json()["restaurant"]
        self.client.post(
            "/api/places/restaurants",
            json={"name": "Dinner Spot", "cuisines": ["Italian"]},
        )
        invalid = self.client.post(
            f"/api/places/restaurants/{first['id']}/review",
            json={"rating": 4.2, "visited_at": "2026-07-29", "version": first["version"]},
        )
        self.assertEqual(invalid.status_code, 400)
        choice = self.client.post(
            "/api/places/choose",
            json={"pool": "unvisited", "cuisines": ["Breakfast"]},
        )
        self.assertEqual(choice.status_code, 200)
        self.assertEqual(choice.get_json()["restaurant"]["name"], "Breakfast Spot")

    def test_custom_cuisine_becomes_shared_persistent_option(self):
        added = self.client.post(
            "/api/places/restaurants",
            json={"name": "Fusion Test", "cuisines": ["Cajun Fusion"]},
        ).get_json()["restaurant"]
        listing = self.client.get(
            "/api/places/restaurants?view=want_to_go"
        ).get_json()
        self.assertIn("Cajun Fusion", listing["cuisines"])
        self.client.delete(
            f"/api/places/restaurants/{added['id']}", json={"version": added["version"]}
        )
        later = self.client.get(
            "/api/places/restaurants?view=want_to_go"
        ).get_json()
        self.assertIn("Cajun Fusion", later["cuisines"])

    def test_places_mutations_require_csrf(self):
        client = portal.app.test_client()
        response = client.post(
            "/api/places/restaurants", json={"name": "Should Fail"}
        )
        self.assertEqual(response.status_code, 403)

    def test_restaurant_photos_are_reencoded_listed_and_individually_removed(self):
        from PIL import Image
        first_image, second_image = BytesIO(), BytesIO()
        Image.new("RGBA", (40, 24), (20, 120, 220, 120)).save(first_image, "PNG")
        Image.new("RGB", (32, 32), (220, 80, 40)).save(second_image, "PNG")
        first_image.seek(0); second_image.seek(0)
        added = self.client.post(
            "/api/places/restaurants",
            data={
                "name": "Photo Cafe",
                "cuisines": "Cafe",
                "notes": "Window seat.",
                "images": [(first_image, "cafe.png"), (second_image, "lunch.png")],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(added.status_code, 201)
        restaurant = added.get_json()["restaurant"]
        self.assertTrue(restaurant["has_image"])
        self.assertEqual(len(restaurant["photos"]), 2)
        for photo in restaurant["photos"]:
            served = self.client.get(photo["url"])
            self.assertEqual(served.status_code, 200)
            self.assertEqual(served.mimetype, "image/jpeg")
        removed_url = restaurant["photos"][0]["url"]
        with places_module.connect(places_module.DB_PATH) as connection:
            legacy_image_name = connection.execute(
                "SELECT image_name FROM restaurant_photos WHERE id=?",
                (restaurant["photos"][0]["id"],),
            ).fetchone()[0]
            connection.execute(
                "UPDATE restaurants SET image_name=? WHERE id=?",
                (legacy_image_name, restaurant["id"]),
            )
        legacy_image_url = f"/api/places/restaurants/{restaurant['id']}/image"
        self.assertEqual(self.client.get(legacy_image_url).status_code, 200)
        removed = self.client.put(
            f"/api/places/restaurants/{restaurant['id']}",
            data={
                "name": "Photo Cafe",
                "cuisines": "Cafe",
                "notes": "Window seat.",
                "version": str(restaurant["version"]),
                "remove_photo_ids": restaurant["photos"][0]["id"],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(removed.status_code, 200)
        updated = removed.get_json()["restaurant"]
        self.assertTrue(updated["has_image"])
        self.assertEqual(len(updated["photos"]), 1)
        self.assertEqual(self.client.get(removed_url).status_code, 404)
        self.assertEqual(self.client.get(legacy_image_url).status_code, 404)
        self.assertEqual(self.client.get(updated["photos"][0]["url"]).status_code, 200)

    def test_chat_uses_verified_users_fixed_membership_and_encrypted_messages(self):
        diana, diana_csrf = self.paired_client("diana")
        self.assertEqual(diana.get("/chat").status_code, 200)
        self.assertEqual(self.client.get("/chat").status_code, 200)
        users = self.client.get("/api/chat/users").get_json()["users"]
        self.assertEqual([item["owner_id"] for item in users], ["diana@example.test"])
        created = self.client.post(
            "/api/chat/conversations", json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(created.status_code, 201)
        conversation_id = created.get_json()["id"]
        sent = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "A private household message", "client_message_id": "one"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(sent.status_code, 201)
        message_id = sent.get_json()["message"]["id"]
        with chat_module.connect(chat_module.DB_PATH) as connection:
            stored = connection.execute("SELECT body_cipher FROM messages WHERE id=?", (message_id,)).fetchone()[0]
            members = {row[0] for row in connection.execute(
                "SELECT owner_id FROM conversation_members WHERE conversation_id=?", (conversation_id,)
            )}
        self.assertNotIn(b"private household", bytes(stored))
        self.assertEqual(members, {"david@example.test", "diana@example.test"})
        listing = diana.get(f"/api/chat/conversations/{conversation_id}/messages").get_json()["messages"]
        self.assertEqual(listing[0]["body"], "A private household message")
        self.assertFalse(listing[0]["mine"])
        self.assertEqual(
            diana.patch(
                f"/api/chat/messages/{message_id}",
                json={"body": "changed", "version": listing[0]["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            404,
        )

    def test_chat_key_outage_is_json_fail_closed_and_preserves_encrypted_rows(self):
        def chat_table_snapshot():
            with chat_module.connect(chat_module.DB_PATH) as connection:
                names = [
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                        "ORDER BY name"
                    ).fetchall()
                ]
                return {
                    name: [
                        tuple(row)
                        for row in connection.execute(
                            f'SELECT * FROM "{name.replace(chr(34), chr(34) * 2)}" '
                            "ORDER BY rowid"
                        ).fetchall()
                    ]
                    for name in names
                }

        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        created = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        )
        conversation_id = created.get_json()["id"]
        sent = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "must remain encrypted", "client_message_id": "key-outage"},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(sent.status_code, 201)
        before = chat_table_snapshot()

        wrong_key = base64.b64encode(bytes(range(32))).decode("ascii")
        with patch.dict(os.environ, {"DAVID_PI_CHAT_KEY_B64": wrong_key}):
            unavailable = self.client.get("/api/chat/conversations")
            refused_create = self.client.post(
                "/api/chat/conversations",
                json={"member_ids": ["diana@example.test"]},
                headers={"X-CSRF-Token": self.csrf},
            )
            unknown = portal.app.test_client()
            unknown.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = (
                "unknown@example.test"
            )
            unknown.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Unknown"
            unauthorized = unknown.get("/api/chat/conversations")

        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.mimetype, "application/json")
        self.assertEqual(
            unavailable.get_json(),
            {
                "code": "chat_private_storage_unavailable",
                "error": (
                    "Chat's private storage is temporarily unavailable. "
                    "No messages were changed."
                ),
            },
        )
        self.assertNotIn("must remain encrypted", unavailable.get_data(as_text=True))
        self.assertNotIn(str(chat_module.KEY_FILE), unavailable.get_data(as_text=True))
        self.assertEqual(refused_create.status_code, 503)
        self.assertEqual(
            refused_create.get_json()["code"], "chat_private_storage_unavailable"
        )
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(chat_table_snapshot(), before)

        unreadable = Path(TEST_DATA.name) / "locked-chat-master.key"
        unreadable.write_bytes(b"u" * 32)
        unreadable.chmod(0)
        try:
            with patch.object(chat_module, "KEY_FILE", unreadable), patch.dict(
                os.environ, {"DAVID_PI_CHAT_KEY_B64": ""}
            ):
                unreadable_get = self.client.get("/api/chat/conversations")
                unreadable_post = self.client.post(
                    "/api/chat/conversations",
                    json={"member_ids": ["diana@example.test"]},
                    headers={"X-CSRF-Token": self.csrf},
                )
        finally:
            unreadable.chmod(0o600)

        for response in (unreadable_get, unreadable_post):
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.mimetype, "application/json")
            self.assertEqual(
                response.get_json()["code"], "chat_private_storage_unavailable"
            )
            self.assertNotIn(str(unreadable), response.get_data(as_text=True))
        self.assertEqual(chat_table_snapshot(), before)

    def test_readiness_reads_and_decodes_the_chat_key_instead_of_only_stating_it(self):
        status_path = Path(TEST_DATA.name) / "chat-key-ready-status.json"
        status_path.write_text(json.dumps({
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "state": "healthy",
            "subsystems": {},
            "databases": [],
            "privacy": {
                "contains_personal_filenames": False,
                "contains_domains": False,
                "contains_clients": False,
                "contains_secrets": False,
            },
        }), encoding="utf-8")
        unreadable = Path(TEST_DATA.name) / "unreadable-chat-master.key"
        unreadable.write_bytes(b"k" * 32)
        unreadable.chmod(0)
        try:
            with patch.object(portal, "SERVER_STATUS", status_path), \
                 patch.object(portal, "DATA_SENTINEL", DEVICE_SENTINEL), \
                 patch.object(chat_module, "KEY_FILE", unreadable), \
                 patch.dict(os.environ, {"DAVID_PI_CHAT_KEY_B64": ""}):
                response = self.client.get("/ready")
        finally:
            unreadable.chmod(0o600)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["reasons"], ["secret_unavailable"])
        self.assertNotIn(str(unreadable), response.get_data(as_text=True))

        raw_key = Path(TEST_DATA.name) / "raw-chat-master.key"
        raw_key.write_bytes(b"r" * 32)
        encoded_key = Path(TEST_DATA.name) / "encoded-chat-master.key"
        encoded_key.write_bytes(base64.b64encode(b"e" * 32))
        for candidate in (raw_key, encoded_key):
            with self.subTest(candidate=candidate.name), \
                 patch.object(portal, "SERVER_STATUS", status_path), \
                 patch.object(portal, "DATA_SENTINEL", DEVICE_SENTINEL), \
                 patch.object(chat_module, "KEY_FILE", candidate), \
                 patch.dict(os.environ, {"DAVID_PI_CHAT_KEY_B64": ""}):
                self.assertTrue(chat_module.chat_key_available())
                self.assertEqual(self.client.get("/ready").status_code, 200)

        malformed = Path(TEST_DATA.name) / "malformed-chat-master.key"
        malformed.write_text("not a private key", encoding="utf-8")
        with patch.object(portal, "SERVER_STATUS", status_path), \
             patch.object(portal, "DATA_SENTINEL", DEVICE_SENTINEL), \
             patch.object(chat_module, "KEY_FILE", malformed), \
             patch.dict(os.environ, {"DAVID_PI_CHAT_KEY_B64": ""}):
            self.assertFalse(chat_module.chat_key_available())
            malformed_response = self.client.get("/ready")
        self.assertEqual(malformed_response.status_code, 503)
        self.assertEqual(
            malformed_response.get_json()["reasons"], ["secret_unavailable"]
        )

    def test_deferred_gif_search_does_not_contact_provider(self):
        with patch("urllib.request.urlopen") as remote:
            for route in ("/api/chat/gifs/search?q=cats", "/api/chat/gifs/media/unused"):
                self.assertEqual(self.client.get(route).status_code,404)
            remote.assert_not_called()

    def test_chat_membership_uses_only_the_exact_household_allowlist(self):
        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        self.client.get("/chat")
        outsider_id = "legacy-user@tailnet.example"
        with chat_module.connect(chat_module.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO portal_users(owner_id,display_name,first_seen_at,last_seen_at) "
                "VALUES(?,?,?,?)",
                (outsider_id, "Legacy User", "2026-01-01", "2026-01-01"),
            )

        users = self.client.get("/api/chat/users").get_json()["users"]
        self.assertEqual([item["owner_id"] for item in users], ["diana@example.test"])
        rejected = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": [outsider_id]},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(rejected.status_code, 422)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                0,
            )

    def test_chat_attachment_is_authorized_and_save_to_media_uses_canonical_owner(self):
        from PIL import Image
        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations", json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        image = BytesIO(); Image.new("RGB", (40, 30), "coral").save(image, "PNG"); image.seek(0)
        sent = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "", "client_message_id": "photo", "attachments": (image, "meal.png")},
            content_type="multipart/form-data", headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(sent.status_code, 201)
        attachment = sent.get_json()["message"]["attachments"][0]
        self.assertEqual(diana.get(attachment["preview_url"]).status_code, 200)
        outsider = portal.app.test_client()
        outsider.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "claire@example.test"
        outsider.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Claire"
        outsider_csrf = "outsider-csrf-token-with-more-than-32-characters"
        outsider.set_cookie("david_pi_csrf", outsider_csrf, domain="localhost")
        self.assertEqual(outsider.get(attachment["preview_url"]).status_code, 403)
        saved = self.client.post(
            f"/api/chat/attachments/{attachment['id']}/save-to-media",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(saved.status_code, 200)
        with portal.db() as connection:
            media = connection.execute("SELECT owner_id,visibility,ingestion_source FROM photos WHERE id=?", (saved.get_json()["media_id"],)).fetchone()
        with chat_module.connect(chat_module.DB_PATH) as connection:
            media_audit = connection.execute(
                "SELECT actor_id,action,before_digest,after_digest FROM mutation_audit "
                "WHERE domain='chat_media_copy' AND object_id=? ORDER BY id DESC LIMIT 1",
                (saved.get_json()["media_id"],),
            ).fetchone()
        self.assertEqual(tuple(media), ("david@example.test", "private", "chat_save"))
        self.assertEqual(
            (media_audit["actor_id"], media_audit["action"]),
            ("david@example.test", "create"),
        )
        self.assertIsNone(media_audit["before_digest"])
        self.assertEqual(len(media_audit["after_digest"]), 64)
        self.assertEqual(
            outsider.post(f"/api/chat/attachments/{attachment['id']}/save-to-media", headers={"X-CSRF-Token": outsider_csrf}).status_code,
            403,
        )

    def test_chat_save_to_media_rechecks_the_conversation_policy(self):
        from PIL import Image

        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        image = BytesIO()
        Image.new("RGB", (18, 18), "coral").save(image, "PNG")
        image.seek(0)
        attachment_id = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={
                "client_message_id": "save-policy",
                "attachments": (image, "policy.png"),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["message"]["attachments"][0]["id"]
        with patch.object(
            chat_module, "_authorize", return_value=SimpleNamespace(allowed=False)
        ) as authorize:
            denied = self.client.post(
                f"/api/chat/attachments/{attachment_id}/save-to-media",
                headers={"X-CSRF-Token": self.csrf},
            )
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(authorize.call_args.args[0], "chat.attachment.copy_to_media")

        with patch.object(
            chat_module, "_audit_mutation", side_effect=RuntimeError("audit failed")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/api/chat/attachments/{attachment_id}/save-to-media",
                    headers={"X-CSRF-Token": self.csrf},
                )
        with portal.db() as connection:
            compensated = connection.execute(
                """SELECT deleted_at,deleted_by_id,purge_after,version
                   FROM photos WHERE ingestion_source='chat_save'"""
            ).fetchone()
            self.assertIsNotNone(compensated)
            self.assertIsNotNone(compensated["deleted_at"])
            self.assertEqual(compensated["deleted_by_id"], "david@example.test")
            self.assertIsNotNone(compensated["purge_after"])
            self.assertEqual(compensated["version"], 2)
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM mutation_audit
                       WHERE domain='media' AND action='ingest_compensation_trash'"""
                ).fetchone()[0],
                1,
            )

    def test_chat_attachment_files_roll_back_with_the_database_transaction(self):
        from PIL import Image

        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        directory = chat_module.CHAT_ROOT / conversation_id[:2] / conversation_id
        before = set(directory.iterdir()) if directory.exists() else set()
        image = BytesIO()
        Image.new("RGB", (22, 16), "navy").save(image, "PNG")
        image.seek(0)
        with patch.object(
            chat_module, "_audit_mutation", side_effect=RuntimeError("audit failed")
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"/api/chat/conversations/{conversation_id}/messages",
                    data={
                        "client_message_id": "rollback-files",
                        "attachments": (image, "rollback.png"),
                    },
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": self.csrf},
                )
        after = set(directory.iterdir()) if directory.exists() else set()
        self.assertEqual(after, before)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM chat_attachments WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()[0],
                0,
            )

    def test_chat_attachment_pair_failure_and_crash_journal_are_cleaned(self):
        from PIL import Image

        conversation_id = "crashjournal" + "a" * 20
        image_bytes = BytesIO()
        Image.new("RGB", (20, 20), "green").save(image_bytes, "PNG")
        payload = image_bytes.getvalue()
        upload = SimpleNamespace(
            filename="crash.png",
            mimetype="image/png",
            read=BytesIO(payload).read,
        )
        original_write = chat_module._write_private
        calls = 0

        def fail_preview(directory_fd, name, value):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("preview write failed")
            return original_write(directory_fd, name, value)

        with patch.object(chat_module, "_write_private", side_effect=fail_preview):
            with self.assertRaises(OSError):
                chat_module._store_attachment(upload, 1, conversation_id)
        directory = chat_module.CHAT_ROOT / conversation_id[:2] / conversation_id
        self.assertEqual(list(directory.iterdir()), [])

        crash_upload = SimpleNamespace(
            filename="crash.png",
            mimetype="image/png",
            read=BytesIO(payload).read,
        )
        attachment, handle = chat_module._store_attachment(
            crash_upload, 2, conversation_id
        )
        marker = directory / handle["pending_name"]
        object_path = chat_module.CHAT_ROOT / attachment["object_path"]
        preview_path = chat_module.CHAT_ROOT / attachment["preview_path"]
        chat_module._close_attachment_handle(handle)
        self.assertTrue(marker.is_file())
        self.assertTrue(object_path.is_file())
        self.assertTrue(preview_path.is_file())
        chat_module._reconcile_pending_attachments(
            now=__import__("time").time()
            + chat_module.PENDING_ATTACHMENT_GRACE_SECONDS
            + 1
        )
        self.assertFalse(marker.exists())
        self.assertFalse(object_path.exists())
        self.assertFalse(preview_path.exists())

    def test_chat_message_delete_is_versioned_and_soft_retains_encrypted_content(self):
        from PIL import Image
        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations", json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        image = BytesIO(); Image.new("RGB", (16, 16), "coral").save(image, "PNG"); image.seek(0)
        sent = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "retain me", "client_message_id": "delete-photo", "attachments": (image, "private.png")},
            content_type="multipart/form-data", headers={"X-CSRF-Token": self.csrf},
        ).get_json()["message"]
        attachment = sent["attachments"][0]
        with chat_module.connect(chat_module.DB_PATH) as connection:
            stored = connection.execute(
                "SELECT object_path,preview_path FROM chat_attachments WHERE id=?", (attachment["id"],)
            ).fetchone()
        paths = [chat_module.CHAT_ROOT / stored["object_path"], chat_module.CHAT_ROOT / stored["preview_path"]]
        self.assertTrue(all(path.is_file() for path in paths))
        deleted = self.client.delete(
            f"/api/chat/messages/{sent['id']}",
            json={"version": sent["version"]},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(diana.get(attachment["preview_url"]).status_code, 404)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            retained = connection.execute(
                "SELECT body_cipher,deleted_at,deleted_by,purge_after,version "
                "FROM messages WHERE id=?",
                (sent["id"],),
            ).fetchone()
            audit = connection.execute(
                "SELECT actor_id,action FROM mutation_audit "
                "WHERE domain='chat_message' AND object_id=? ORDER BY id DESC LIMIT 1",
                (str(sent["id"]),),
            ).fetchone()
        self.assertGreater(len(retained["body_cipher"]), 0)
        self.assertIsNotNone(retained["deleted_at"])
        self.assertEqual(retained["deleted_by"], "david@example.test")
        self.assertIsNotNone(retained["purge_after"])
        self.assertGreater(retained["version"], sent["version"])
        self.assertEqual(tuple(audit), ("david@example.test", "trash"))

    def test_chat_leave_is_personal_and_preserves_shared_history_and_objects(self):
        from PIL import Image
        diana, diana_csrf = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations", json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        image = BytesIO(); Image.new("RGB", (16, 16), "coral").save(image, "PNG"); image.seek(0)
        sent = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"client_message_id": "delete-chat", "attachments": (image, "private.png")},
            content_type="multipart/form-data", headers={"X-CSRF-Token": self.csrf},
        ).get_json()["message"]
        attachment = sent["attachments"][0]
        with chat_module.connect(chat_module.DB_PATH) as connection:
            stored = connection.execute(
                "SELECT object_path,preview_path FROM chat_attachments WHERE id=?", (attachment["id"],)
            ).fetchone()
        paths = [chat_module.CHAT_ROOT / stored["object_path"], chat_module.CHAT_ROOT / stored["preview_path"]]
        self.assertTrue(all(path.is_file() for path in paths))

        outsider = portal.app.test_client()
        outsider.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "claire@example.test"
        outsider.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Claire"
        outsider_csrf = "outsider-csrf-token-with-more-than-32-characters"
        outsider.set_cookie("david_pi_csrf", outsider_csrf, domain="localhost")
        self.assertEqual(outsider.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={"action": "leave", "confirmation": "LEAVE CHAT", "conversation_id": conversation_id, "version": 2},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code, 403)

        version = next(
            item["version"]
            for item in diana.get("/api/chat/conversations").get_json()["conversations"]
            if item["id"] == conversation_id
        )
        disabled = self.client.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={"action": "delete_for_all", "version": version},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(disabled.status_code, 503)
        self.assertTrue(all(path.is_file() for path in paths))

        stale = diana.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={
                "action": "leave",
                "confirmation": "LEAVE CHAT",
                "conversation_id": conversation_id,
                "version": version - 1,
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(stale.status_code, 409)

        left = diana.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={
                "action": "leave",
                "confirmation": "LEAVE CHAT",
                "conversation_id": conversation_id,
                "version": version,
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(left.status_code, 200)
        self.assertEqual(left.get_json()["action"], "leave")
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertEqual(
            diana.get(f"/api/chat/conversations/{conversation_id}/messages").status_code,
            404,
        )
        self.assertNotIn(
            conversation_id,
            {item["id"] for item in diana.get("/api/chat/conversations").get_json()["conversations"]},
        )
        self.assertEqual(
            self.client.get(f"/api/chat/conversations/{conversation_id}/messages").status_code,
            200,
        )
        self.assertEqual(self.client.get(attachment["preview_url"]).status_code, 200)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone())
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0], 1)
            membership = connection.execute(
                "SELECT left_at FROM conversation_members WHERE conversation_id=? AND owner_id=?",
                (conversation_id, "diana@example.test"),
            ).fetchone()
            audit = connection.execute(
                "SELECT actor_id,action FROM mutation_audit "
                "WHERE domain='chat_conversation' AND object_id=? ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
        self.assertIsNotNone(membership["left_at"])
        self.assertEqual(tuple(audit), ("diana@example.test", "leave"))

        david_version = next(
            item["version"]
            for item in self.client.get(
                "/api/chat/conversations"
            ).get_json()["conversations"]
            if item["id"] == conversation_id
        )
        last_left = self.client.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={
                "action": "leave",
                "confirmation": "LEAVE CHAT",
                "conversation_id": conversation_id,
                "version": david_version,
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(last_left.status_code, 200)
        self.assertIn("retained_until", last_left.get_json())
        self.assertTrue(all(path.is_file() for path in paths))
        with chat_module.connect(chat_module.DB_PATH) as connection:
            closed = connection.execute(
                "SELECT deleted_at,deleted_by,purge_after FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            active_members = connection.execute(
                "SELECT COUNT(*) FROM conversation_members "
                "WHERE conversation_id=? AND left_at IS NULL",
                (conversation_id,),
            ).fetchone()[0]
            retained_messages = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()[0]
        self.assertIsNotNone(closed["deleted_at"])
        self.assertEqual(closed["deleted_by"], "david@example.test")
        self.assertIsNotNone(closed["purge_after"])
        self.assertEqual(active_members, 0)
        self.assertEqual(retained_messages, 1)
        self.assertEqual(
            self.client.get(
                f"/api/chat/conversations/{conversation_id}/messages"
            ).status_code,
            404,
        )
        replacement = diana.post(
            "/api/chat/conversations",
            json={"member_ids": ["david@example.test"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(replacement.status_code, 201)
        self.assertNotEqual(replacement.get_json()["id"], conversation_id)

    def test_chat_message_edits_require_sender_and_current_version(self):
        diana, diana_csrf = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        message = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "first", "client_message_id": "versioned-edit"},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["message"]
        edited = self.client.patch(
            f"/api/chat/messages/{message['id']}",
            json={"body": "second", "version": message["version"]},
        )
        self.assertEqual(edited.status_code, 200)
        edited_message = edited.get_json()["message"]
        self.assertEqual(edited_message["body"], "second")
        self.assertGreater(edited_message["version"], message["version"])

        stale = self.client.patch(
            f"/api/chat/messages/{message['id']}",
            json={"body": "stale overwrite", "version": message["version"]},
        )
        self.assertEqual(stale.status_code, 409)
        denied = diana.patch(
            f"/api/chat/messages/{message['id']}",
            json={"body": "not mine", "version": edited_message["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(denied.status_code, 404)
        visible = self.client.get(
            f"/api/chat/conversations/{conversation_id}/messages"
        ).get_json()["messages"][0]
        self.assertEqual(visible["body"], "second")
        with chat_module.connect(chat_module.DB_PATH) as connection:
            audit = connection.execute(
                "SELECT actor_id,action,before_digest,after_digest FROM mutation_audit "
                "WHERE domain='chat_message' AND object_id=? AND action='update'",
                (str(message["id"]),),
            ).fetchone()
        self.assertEqual((audit["actor_id"], audit["action"]), ("david@example.test", "update"))
        self.assertEqual(len(audit["before_digest"]), 64)
        self.assertEqual(len(audit["after_digest"]), 64)

    def test_chat_unknown_identity_is_rejected_and_never_registered(self):
        unknown = portal.app.test_client()
        unknown.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "unknown@example.test"
        unknown.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Unknown"
        self.assertEqual(unknown.get("/chat").status_code, 403)
        self.assertEqual(unknown.get("/api/chat/conversations").status_code, 403)
        self.assertEqual(unknown.get("/api/chat/users").status_code, 403)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            registered = connection.execute(
                "SELECT 1 FROM portal_users WHERE owner_id=?",
                ("unknown@example.test",),
            ).fetchone()
        self.assertIsNone(registered)

    def test_chat_attachment_parent_symlink_is_never_followed(self):
        from PIL import Image

        diana, _ = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        image = BytesIO()
        Image.new("RGB", (20, 20), "navy").save(image, "PNG")
        image.seek(0)
        attachment = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"client_message_id": "symlink-parent", "attachments": (image, "safe.png")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["message"]["attachments"][0]
        with chat_module.connect(chat_module.DB_PATH) as connection:
            relative = connection.execute(
                "SELECT preview_path FROM chat_attachments WHERE id=?",
                (attachment["id"],),
            ).fetchone()["preview_path"]
        object_path = chat_module.CHAT_ROOT / relative
        conversation_directory = object_path.parent
        saved_directory = conversation_directory.with_name(
            f"{conversation_directory.name}-saved"
        )
        outside = Path(TEST_DATA.name) / "outside-chat-race"
        outside.mkdir(exist_ok=True)
        (outside / object_path.name).write_bytes(b"external bytes must never be served")
        conversation_directory.rename(saved_directory)
        conversation_directory.symlink_to(outside, target_is_directory=True)
        try:
            self.assertEqual(self.client.get(attachment["preview_url"]).status_code, 404)
        finally:
            conversation_directory.unlink(missing_ok=True)
            saved_directory.rename(conversation_directory)

    def test_browser_push_credentials_cannot_be_reassigned_and_native_push_is_closed(self):
        other, token=self.paired_client("diana")
        self.client.get("/chat");other.get("/chat")
        payload={"endpoint":"https://8.8.8.8/private-device","keys":{"p256dh":"key","auth":"auth"}}
        self.assertEqual(self.client.post("/api/chat/push/web",json=payload).status_code,200)
        rejected=other.post("/api/chat/push/web",json=payload,headers={"X-CSRF-Token":token})
        self.assertEqual(rejected.status_code,409)
        self.assertEqual(rejected.json["code"],"push_credential_conflict")
        self.assertEqual(self.client.post("/api/chat/push/android",json={"token":"a"*64}).status_code,404)
        payload["keys"]["auth"]="updated"
        self.assertEqual(self.client.post("/api/chat/push/web",json=payload).status_code,200)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            rows=connection.execute("SELECT platform,owner_id,version FROM push_subscriptions").fetchall()
            audits=connection.execute("SELECT actor_id,action,after_digest FROM mutation_audit WHERE domain='push_credential'").fetchall()
        self.assertEqual([tuple(row) for row in rows],[("web","david@example.test",2)])
        self.assertEqual([row["action"] for row in audits],["create","update"])
        self.assertTrue(all(len(row["after_digest"])==64 for row in audits))

    def test_chat_push_registration_rejects_internal_endpoint_without_persisting(self):
        rejected = self.client.post(
            "/api/chat/push/web",
            json={
                "endpoint": "https://127.0.0.1/internal",
                "keys": {"p256dh": "key", "auth": "auth"},
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.get_json()["error"], "Invalid push subscription.")
        with chat_module.connect(chat_module.DB_PATH) as connection:
            stored = connection.execute(
                "SELECT 1 FROM push_subscriptions WHERE platform='web'"
            ).fetchone()
        self.assertIsNone(stored)

    def test_chat_browser_push_policy_and_security_audit_are_transactional(self):
        payload={"endpoint":"https://8.8.8.8/policy-device","keys":{"p256dh":"key","auth":"auth"}}
        with patch.object(chat_module,"_authorize",return_value=SimpleNamespace(allowed=False)) as authorize:
            self.assertEqual(self.client.post("/api/chat/push/web",json=payload).status_code,403)
        self.assertEqual(authorize.call_args.args[0],"chat.push.web.update")
        with patch.object(chat_module,"_audit_mutation",side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.client.post("/api/chat/push/web",json=payload)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM push_subscriptions WHERE endpoint=?",(payload["endpoint"],)).fetchone())

    def test_chat_push_endpoint_cannot_change_owner_during_provider_wait(self):
        worker = importlib.import_module("modules.chat_notify_worker")
        diana, diana_csrf = self.paired_client("diana")
        self.client.get("/chat")
        diana.get("/chat")
        endpoint = "https://8.8.8.8/provider-wait"
        registration = self.client.post(
            "/api/chat/push/web",
            json={
                "endpoint": endpoint,
                "keys": {"p256dh": "david-key", "auth": "david-auth"},
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(registration.status_code, 200)
        conversation_id = self.client.post(
            "/api/chat/conversations",
            json={"member_ids": ["diana@example.test"]},
            headers={"X-CSRF-Token": self.csrf},
        ).get_json()["id"]
        sent = diana.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "hello", "client_message_id": "provider-wait"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(sent.status_code, 201)
        sent_message = sent.get_json()["message"]
        conversation_version = next(
            item["version"]
            for item in self.client.get(
                "/api/chat/conversations"
            ).get_json()["conversations"]
            if item["id"] == conversation_id
        )
        provider_started = threading.Event()
        release_provider = threading.Event()
        deliveries = []

        def wait_in_provider(subscription, delivered_conversation_id):
            provider_started.set()
            self.assertTrue(release_provider.wait(timeout=5))
            deliveries.append((subscription["endpoint"], delivered_conversation_id))

        with patch.object(worker, "_web", side_effect=wait_in_provider):
            delivery_thread = threading.Thread(target=worker.process_one)
            delivery_thread.start()
            self.assertTrue(provider_started.wait(timeout=5))
            reassignment = diana.post(
                "/api/chat/push/web",
                json={
                    "endpoint": endpoint,
                    "keys": {"p256dh": "diana-key", "auth": "diana-auth"},
                },
                headers={"X-CSRF-Token": diana_csrf},
            )
            owner_refresh = self.client.post(
                "/api/chat/push/web",
                json={
                    "endpoint": endpoint,
                    "keys": {"p256dh": "new-david-key", "auth": "new-david-auth"},
                },
                headers={"X-CSRF-Token": self.csrf},
            )
            leave_during_delivery = self.client.delete(
                f"/api/chat/conversations/{conversation_id}",
                json={
                    "action": "leave",
                    "confirmation": "LEAVE CHAT",
                    "conversation_id": conversation_id,
                    "version": conversation_version,
                },
                headers={"X-CSRF-Token": self.csrf},
            )
            delete_during_delivery = diana.delete(
                f"/api/chat/messages/{sent_message['id']}",
                json={"version": sent_message["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            )
            release_provider.set()
            delivery_thread.join(timeout=5)

        self.assertFalse(delivery_thread.is_alive())
        self.assertEqual(reassignment.status_code, 409)
        self.assertEqual(reassignment.get_json()["code"], "push_credential_conflict")
        self.assertEqual(owner_refresh.status_code, 409)
        self.assertEqual(
            owner_refresh.get_json()["code"], "notification_delivery_in_progress"
        )
        self.assertEqual(leave_during_delivery.status_code, 409)
        self.assertEqual(
            leave_during_delivery.get_json()["code"],
            "notification_delivery_in_progress",
        )
        self.assertEqual(delete_during_delivery.status_code, 409)
        self.assertEqual(
            delete_during_delivery.get_json()["code"],
            "notification_delivery_in_progress",
        )
        self.assertEqual(deliveries, [(endpoint, conversation_id)])
        with chat_module.connect(chat_module.DB_PATH) as connection:
            credential = connection.execute(
                "SELECT owner_id,version FROM push_subscriptions WHERE endpoint=?",
                (endpoint,),
            ).fetchone()
            job = connection.execute(
                "SELECT status,lease_token,lease_expires_at FROM notification_jobs"
            ).fetchone()
            active_member = connection.execute(
                "SELECT left_at FROM conversation_members "
                "WHERE conversation_id=? AND owner_id=?",
                (conversation_id, "david@example.test"),
            ).fetchone()
            retained_message = connection.execute(
                "SELECT deleted_at FROM messages WHERE id=?",
                (sent_message["id"],),
            ).fetchone()
        self.assertEqual(tuple(credential), ("david@example.test", 1))
        self.assertEqual(job["status"], "sent")
        self.assertIsNone(job["lease_token"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertIsNone(active_member["left_at"])
        self.assertIsNone(retained_message["deleted_at"])

    def test_chat_read_receipt_idempotency_and_generic_notification_queue(self):
        diana, diana_csrf = self.paired_client("diana")
        diana.get("/chat")
        conversation_id = self.client.post(
            "/api/chat/conversations", json={"member_ids": ["diana@example.test"]}, headers={"X-CSRF-Token": self.csrf}
        ).get_json()["id"]
        first = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "hello", "client_message_id": "same"}, headers={"X-CSRF-Token": self.csrf},
        )
        duplicate = self.client.post(
            f"/api/chat/conversations/{conversation_id}/messages",
            data={"body": "hello", "client_message_id": "same"}, headers={"X-CSRF-Token": self.csrf},
        )
        self.assertTrue(duplicate.get_json()["duplicate"])
        message_id = first.get_json()["message"]["id"]
        self.assertEqual(diana.post(
            f"/api/chat/conversations/{conversation_id}/read", json={"message_id": message_id},
            headers={"X-CSRF-Token": diana_csrf},
        ).status_code, 200)
        refreshed = self.client.get(f"/api/chat/conversations/{conversation_id}/messages").get_json()["messages"][0]
        self.assertEqual(refreshed["delivery"], "seen")
        worker_source = Path(chat_module.__file__).with_name("chat_notify_worker.py").read_text(encoding="utf-8")
        self.assertIn('"New household message"', worker_source)
        self.assertNotIn("sender_name", worker_source)
        self.assertNotIn("body_cipher", worker_source)

    def test_chat_mobile_controls_are_visible_cancellable_and_recover_notifications(self):
        base_dir = Path(__file__).resolve().parents[1]
        html = self.client.get("/chat").get_data(as_text=True)
        css = (base_dir / "static" / "chat.css").read_text(encoding="utf-8")
        javascript = (base_dir / "static" / "chat.js").read_text(encoding="utf-8")
        worker_source = (base_dir / "modules" / "chat_notify_worker.py").read_text(encoding="utf-8")
        android = (base_dir / "clients" / "android" / "app" / "src" / "main" / "java" / "com" / "davidpi" / "backup" / "PortalShell.kt").read_text(encoding="utf-8")
        self.assertIn('id="closeNewChat" type="button"', html)
        self.assertIn('id="cancelNewChat"', html)
        self.assertIn('id="createChat" class="primary" type="submit" value="default" disabled', html)
        self.assertIn('id="notificationDialog"', html)
        self.assertIn('id="composerStatus"', html)
        self.assertIn('id="jumpLatest"', html)
        self.assertIn('>Allow alerts</button>', html)
        self.assertIn('/static/chat.js?v=16', html)
        self.assertNotIn("color:transparent", css)
        self.assertIn("min-width: 92px", css)
        self.assertIn("$('closeNewChat').onclick = closeNewChat", javascript)
        self.assertIn("$('cancelNewChat').onclick = closeNewChat", javascript)
        self.assertIn("Choose at least one person, or tap Cancel to leave.", javascript)
        self.assertNotIn("alert('Notifications are blocked", javascript)
        self.assertIn("openNotificationSettings", javascript)
        self.assertIn("error.code = typeof data.code", javascript)
        self.assertIn("isPushCredentialConflict(error)", javascript)
        self.assertIn("await subscription.unsubscribe()", javascript)
        self.assertIn("replacement.endpoint === priorEndpoint", javascript)
        self.assertIn("await replacement.unsubscribe()", javascript)
        self.assertIn(r"^\/api\/chat\/gifs\/media\/", javascript)
        self.assertNotIn("/api/chat/gifs/fetch?url=", javascript)
        self.assertIn("window.DavidPiPush?.retireToken?.(token)", javascript)
        self.assertNotIn("FirebaseMessaging.getInstance()", android)
        self.assertIn("fun isConfigured(): Boolean = false", android)
        self.assertIn("[hidden] { display: none !important; }", css)
        self.assertIn("grid-template-columns: 41px 41px 41px minmax(0, 1fr) 58px", css)
        self.assertIn("flex: 1 1 0", css)
        self.assertIn("height: var(--chat-viewport-height)", css)
        self.assertIn(".message-pane {", css)
        self.assertIn("display: flex", css)
        self.assertIn("flex-direction: column", css)
        self.assertNotIn("--chat-system-bottom-inset: max(56px", css)
        self.assertIn("max(9px, var(--chat-system-bottom-inset))", css)
        self.assertIn("flex: 0 0 auto", css)
        self.assertIn("window.visualViewport?.addEventListener('resize', syncViewport", javascript)
        self.assertIn("function isInstalledWebApp()", javascript)
        self.assertIn("window.navigator.standalone === true", javascript)
        self.assertIn("classList.toggle('installed-web-app', isInstalledWebApp())", javascript)
        self.assertIn("new ResizeObserver", javascript)
        self.assertIn("state.pendingClientId ||= clientMessageId()", javascript)
        self.assertIn("Your draft was kept; tap Send to retry.", javascript)
        self.assertIn("$('jumpLatest').onclick", javascript)
        self.assertIn("data-plain-body", javascript)
        self.assertNotIn('id="deleteConversation"', html)
        self.assertIn('class="conversation-delete"', javascript)
        self.assertIn('id="deleteChatDialog"', html)
        self.assertIn("async function deleteConversation(event)", javascript)
        self.assertIn("confirmation: 'LEAVE CHAT'", javascript)
        self.assertIn("Other members and the encrypted shared history stay unchanged.", html)
        self.assertNotIn("permanently removes", javascript)
        self.assertIn("Notification.permission !== 'granted'", javascript)
        self.assertIn("registration.pushManager.getSubscription()", javascript)
        self.assertIn('DAVID_PI_VAPID_SUBJECT', worker_source)
        self.assertIn('public_url()', worker_source)
        self.assertNotIn('notifications@david-pi.local', worker_source)
        self.assertIn("else if (conversations.length === 1)", javascript)
        self.assertIn("await openThread(conversations[0].id)", javascript)
        self.assertIn("async function refreshChatLifecycle()", javascript)
        self.assertIn("window.addEventListener('pageshow'", javascript)
        self.assertIn("refreshChatLifecycle().catch", javascript)
        self.assertIn("grid-auto-rows: minmax(72px, auto)", css)
        self.assertIn("fun notificationsEnabled(): Boolean", android)
        self.assertIn("fun isConfigured(): Boolean = false", android)
        self.assertIn(".imePadding()", android)
        self.assertNotIn("webView.destroy()", android)
        self.assertNotIn("navigator.serviceWorker.getRegistrations().then", android)
        self.assertIn("settings.cacheMode = WebSettings.LOAD_DEFAULT", android)
        self.assertNotIn("clearCache(true)", android)
        self.assertIn("var navigationRequest by remember { mutableIntStateOf(0) }", android)
        self.assertIn("navigationRequest += 1", android)
        self.assertIn("appliedNavigationRequest != navigationRequest", android)
        self.assertNotIn("finally{location.reload();}", android)

    def test_margarita_month_safely_reencodes_image(self):
        from PIL import Image
        image = BytesIO()
        Image.new("RGBA", (24, 16), (255, 0, 0, 120)).save(image, "PNG")
        image.seek(0)
        version = self.client.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][0]["version"]
        response = self.client.put(
            "/api/places/margaritas/1",
            data={
                "name": "January Marg",
                "rating": "3.5",
                "review": "Tart.",
                "version": str(version),
                "image": (image, "month.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        listing = self.client.get("/api/places/margaritas").get_json()["margaritas"]
        self.assertTrue(listing[0]["has_image"])
        served = self.client.get(listing[0]["image_url"])
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/jpeg")

    def test_margarita_calendar_is_shared_versioned_and_retains_replaced_photos(self):
        from PIL import Image

        david, david_csrf = self.allowlisted_client("david@example.test", "David")
        diana, diana_csrf = self.allowlisted_client("diana@example.test", "Diana")

        first_photo = BytesIO()
        Image.new("RGB", (24, 18), "teal").save(first_photo, "PNG")
        first_photo.seek(0)
        initial = diana.get("/api/places/margaritas").get_json()["margaritas"][8]
        created = diana.put(
            "/api/places/margaritas/9",
            data={
                "name": "September Shared",
                "rating": "4.5",
                "review": "Diana's original notes",
                "version": initial["version"],
                "updated_by_id": "attacker@example.test",
                "image": (first_photo, "first.png"),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.get_json()["version"], 2)

        diana_view = diana.get("/api/places/margaritas").get_json()["margaritas"][8]
        david_view = david.get("/api/places/margaritas").get_json()["margaritas"][8]
        self.assertEqual(david_view, diana_view)
        self.assertEqual(david_view["updated_by_id"], "diana@example.test")
        self.assertEqual(david_view["owner_id"], "diana@example.test")
        self.assertEqual(david_view["version"], 2)
        self.assertEqual(
            david.get(david_view["image_url"]).data,
            diana.get(diana_view["image_url"]).data,
        )
        with places_module.connect(places_module.DB_PATH) as connection:
            first_image_name = connection.execute(
                "SELECT image_name FROM margaritas WHERE month=9"
            ).fetchone()["image_name"]

        replacement = BytesIO()
        Image.new("RGB", (24, 18), "tomato").save(replacement, "PNG")
        replacement.seek(0)
        changed = david.put(
            "/api/places/margaritas/9",
            data={
                "name": "September Shared — updated",
                "rating": "5",
                "review": "Joint favorite",
                "version": david_view["version"],
                "image": (replacement, "replacement.png"),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.get_json()["version"], 3)
        changed_for_diana = diana.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][8]
        self.assertEqual(changed_for_diana["name"], "September Shared — updated")
        self.assertEqual(changed_for_diana["updated_by_id"], "david@example.test")
        self.assertEqual(changed_for_diana["version"], 3)

        with places_module.connect(places_module.DB_PATH) as connection:
            replacement_image_name = connection.execute(
                "SELECT image_name FROM margaritas WHERE month=9"
            ).fetchone()["image_name"]
        removed = diana.put(
            "/api/places/margaritas/9",
            data={
                "name": "September Shared — updated",
                "rating": "5",
                "review": "Joint favorite",
                "version": changed_for_diana["version"],
                "remove_image": "true",
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.get_json()["version"], 4)

        stale = david.put(
            "/api/places/margaritas/9",
            data={"name": "Stale overwrite", "version": 3},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["current_version"], 4)
        outsider, outsider_csrf = self.allowlisted_client(
            "outsider@example.test", "Outsider"
        )
        self.assertEqual(
            outsider.put(
                "/api/places/margaritas/9",
                data={"name": "Denied", "version": 4},
                headers={"X-CSRF-Token": outsider_csrf},
            ).status_code,
            403,
        )

        final_for_david = david.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][8]
        final_for_diana = diana.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][8]
        self.assertEqual(final_for_david, final_for_diana)
        self.assertFalse(final_for_david["has_image"])
        self.assertEqual(final_for_david["name"], "September Shared — updated")
        with places_module.connect(places_module.DB_PATH) as connection:
            history = {
                row["image_name"]: row["owner_id"]
                for row in connection.execute(
                    "SELECT image_name,owner_id FROM margarita_image_history WHERE month=9"
                ).fetchall()
            }
            self.assertEqual(
                history,
                {
                    first_image_name: "david@example.test",
                    replacement_image_name: "diana@example.test",
                },
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM margarita_records WHERE month=9"
                ).fetchone()[0],
                0,
            )
            audit = connection.execute(
                """SELECT actor_id,action FROM mutation_audit
                   WHERE domain='place_margarita_record' AND object_id='9'
                   ORDER BY id DESC LIMIT 3"""
            ).fetchall()
        self.assertEqual(
            [(row["actor_id"], row["action"]) for row in audit],
            [
                ("diana@example.test", "update"),
                ("david@example.test", "update"),
                ("diana@example.test", "update"),
            ],
        )
        self.assertTrue((places_module.IMAGE_ROOT / first_image_name).is_file())
        self.assertTrue((places_module.IMAGE_ROOT / replacement_image_name).is_file())

    def test_margarita_migration_is_additive_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "places.db"
            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    """CREATE TABLE margaritas (
                       month INTEGER PRIMARY KEY,
                       name TEXT NOT NULL DEFAULT '', rating REAL,
                       review TEXT NOT NULL DEFAULT '', image_name TEXT,
                       updated_by_id TEXT, updated_by_name TEXT, updated_at TEXT)"""
                )
                places_module.initialize_places(connection)
                self.assertIn(
                    "version",
                    {
                        row["name"]
                        for row in connection.execute("PRAGMA table_info(margaritas)")
                    },
                )
                personal_rows = [
                    (1, "diana@example.test", "one", "Diana", "Solo", 4.5,
                     "Keep this", "solo.jpg", 4, "2026-09-01T01:02:03+00:00"),
                    (2, "diana@example.test", "two", "Diana", "Do not copy", 2.0,
                     "Personal", "personal.jpg", 2, "2026-09-02T01:02:03+00:00"),
                    (3, "david@example.test", "three-a", "David", "David copy", 3.0,
                     "First", "david.jpg", 1, "2026-09-03T01:02:03+00:00"),
                    (3, "diana@example.test", "three-b", "Diana", "Diana copy", 5.0,
                     "Second", "diana.jpg", 1, "2026-09-04T01:02:03+00:00"),
                ]
                connection.executemany(
                    """INSERT INTO margarita_records
                       (month,owner_id,record_id,owner_name,name,rating,review,
                        image_name,version,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    personal_rows,
                )
                connection.execute(
                    """UPDATE margaritas SET name='Canonical wins',rating=5,
                       review='Already shared',image_name='canonical.jpg',
                       updated_by_id='david@example.test',updated_by_name='David',
                       updated_at='2026-08-01T00:00:00+00:00',version=7
                       WHERE month=2"""
                )
                personals_before = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM margarita_records ORDER BY month,owner_id"
                    ).fetchall()
                ]
                with self.assertLogs(places_module.LOGGER.name, level="WARNING") as logs:
                    places_module.initialize_places(connection)
                promoted = connection.execute(
                    "SELECT * FROM margaritas WHERE month=1"
                ).fetchone()
                self.assertEqual(promoted["name"], "Solo")
                self.assertEqual(promoted["rating"], 4.5)
                self.assertEqual(promoted["review"], "Keep this")
                self.assertEqual(promoted["image_name"], "solo.jpg")
                self.assertEqual(promoted["updated_by_id"], "diana@example.test")
                self.assertEqual(promoted["updated_by_name"], "Diana")
                self.assertEqual(promoted["updated_at"], "2026-09-01T01:02:03+00:00")
                self.assertEqual(promoted["version"], 4)
                canonical = connection.execute(
                    "SELECT * FROM margaritas WHERE month=2"
                ).fetchone()
                self.assertEqual(canonical["name"], "Canonical wins")
                self.assertEqual(canonical["image_name"], "canonical.jpg")
                self.assertEqual(canonical["version"], 7)
                ambiguous = connection.execute(
                    "SELECT * FROM margaritas WHERE month=3"
                ).fetchone()
                self.assertFalse(places_module.margarita_row_populated(ambiguous))
                self.assertEqual(
                    places_module.margarita_migration_conflicts(connection), {3}
                )
                self.assertTrue(any(
                    "month=3 populated_personal_records=2" in entry
                    for entry in logs.output
                ))
                self.assertEqual(
                    [
                        tuple(row)
                        for row in connection.execute(
                            "SELECT * FROM margarita_records ORDER BY month,owner_id"
                        ).fetchall()
                    ],
                    personals_before,
                )
                canonical_after_first_run = tuple(
                    connection.execute(
                        "SELECT * FROM margaritas WHERE month=1"
                    ).fetchone()
                )
                with self.assertLogs(places_module.LOGGER.name, level="WARNING"):
                    places_module.initialize_places(connection)
                self.assertEqual(
                    tuple(connection.execute(
                        "SELECT * FROM margaritas WHERE month=1"
                    ).fetchone()),
                    canonical_after_first_run,
                )

    def test_restaurant_migration_normalizes_only_visibility_and_installs_exact_triggers(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "places.db"
            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    """CREATE TABLE restaurants (
                       id TEXT PRIMARY KEY, name TEXT NOT NULL,
                       cuisines_json TEXT NOT NULL DEFAULT '[]',
                       notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                       rating REAL, review TEXT NOT NULL DEFAULT '', visited_at TEXT,
                       added_by_id TEXT, added_by_name TEXT NOT NULL,
                       reviewed_by_id TEXT, reviewed_by_name TEXT,
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       image_name TEXT, owner_id TEXT, owner_name TEXT,
                       visibility TEXT NOT NULL DEFAULT 'shared',
                       version INTEGER NOT NULL DEFAULT 1,
                       deleted_at TEXT, deleted_by TEXT, purge_after TEXT)"""
                )
                connection.executemany(
                    """INSERT INTO restaurants
                       (id,name,cuisines_json,notes,status,rating,review,visited_at,
                        added_by_id,added_by_name,reviewed_by_id,reviewed_by_name,
                        created_at,updated_at,image_name,owner_id,owner_name,
                        visibility,version,deleted_at,deleted_by,purge_after)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        (
                            "private-active", "Private history", '["Cajun"]', "Keep",
                            "reviewed", 4.5, "Keep review", "2026-08-01",
                            "diana@example.test", "Diana", "david@example.test", "David",
                            "created", "updated", "one.jpg", "diana@example.test", "Diana",
                            "private", 7, None, None, None,
                        ),
                        (
                            "shared-trash", "Shared history", "[]", "Also keep",
                            "want_to_go", None, "", None,
                            "david@example.test", "David", None, None,
                            "created-2", "updated-2", None, "david@example.test", "David",
                            "shared", 4, "deleted", "diana@example.test", "purge-later",
                        ),
                    ),
                )
                before = {
                    row["id"]: tuple(
                        value for key, value in dict(row).items()
                        if key not in {"visibility", "version"}
                    )
                    for row in connection.execute(
                        "SELECT * FROM restaurants ORDER BY id"
                    ).fetchall()
                }
                places_module.initialize_places(connection)
                first = connection.execute(
                    "SELECT * FROM restaurants ORDER BY id"
                ).fetchall()
                self.assertEqual({row["visibility"] for row in first}, {"shared"})
                self.assertEqual(
                    {row["id"]: row["version"] for row in first},
                    {"private-active": 8, "shared-trash": 4},
                )
                self.assertEqual(
                    {
                        row["id"]: tuple(
                            value for key, value in dict(row).items()
                            if key not in {"visibility", "version"}
                        )
                        for row in first
                    },
                    before,
                )
                stale = connection.execute(
                    "UPDATE restaurants SET name='stale overwrite' "
                    "WHERE id='private-active' AND version=7"
                )
                self.assertEqual(stale.rowcount, 0)
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM restaurants WHERE id='private-active'"
                    ).fetchone()["name"],
                    "Private history",
                )
                trigger_sql = {
                    row["name"]: places_module.normalized_sql(row["sql"])
                    for row in connection.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE 'restaurants_household_shared_%'"
                    ).fetchall()
                }
                self.assertEqual(
                    trigger_sql,
                    {
                        name: places_module.normalized_sql(statement)
                        for name, statement in places_module.RESTAURANT_SHARED_TRIGGERS.items()
                    },
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "must remain household shared"
                ):
                    connection.execute(
                        "UPDATE restaurants SET visibility='private' WHERE id='private-active'"
                    )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "must remain household shared"
                ):
                    connection.execute(
                        """INSERT INTO restaurants
                           (id,name,added_by_name,created_at,updated_at,status,visibility)
                           VALUES ('future-private','No','David','now','now','want_to_go','private')"""
                    )
                stable = [tuple(row) for row in connection.execute(
                    "SELECT * FROM restaurants ORDER BY id"
                ).fetchall()]
                places_module.initialize_places(connection)
                self.assertEqual(
                    [tuple(row) for row in connection.execute(
                        "SELECT * FROM restaurants ORDER BY id"
                    ).fetchall()],
                    stable,
                )

    def test_margarita_unique_legacy_photo_promotes_and_ambiguous_month_blocks_put(self):
        from PIL import Image

        david, david_csrf = self.allowlisted_client("david@example.test", "David")
        diana, _diana_csrf = self.allowlisted_client("diana@example.test", "Diana")
        promoted_name = "legacy-promoted.jpg"
        Image.new("RGB", (16, 12), "purple").save(
            places_module.IMAGE_ROOT / promoted_name, "JPEG"
        )
        conflict_names = ("legacy-david.jpg", "legacy-diana.jpg")
        for name, color in zip(conflict_names, ("navy", "gold")):
            Image.new("RGB", (16, 12), color).save(
                places_module.IMAGE_ROOT / name, "JPEG"
            )
        with places_module.connect(places_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO margarita_records
                   (month,owner_id,record_id,owner_name,name,rating,review,
                    image_name,version,updated_at)
                   VALUES (11,'diana@example.test','promoted','Diana','November',
                           4.5,'Intact',?,3,'2026-09-01T00:00:00+00:00')""",
                (promoted_name,),
            )
            connection.executemany(
                """INSERT INTO margarita_records
                   (month,owner_id,record_id,owner_name,name,rating,review,
                    image_name,version,updated_at)
                   VALUES (10,?,?,?,?,?,?,?,1,'2026-09-01T00:00:00+00:00')""",
                (
                    ("david@example.test", "conflict-david", "David", "David older", 3.0, "A", conflict_names[0]),
                    ("diana@example.test", "conflict-diana", "Diana", "Diana older", 5.0, "B", conflict_names[1]),
                ),
            )
            personals_before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM margarita_records ORDER BY month,owner_id"
                ).fetchall()
            ]
            with self.assertLogs(places_module.LOGGER.name, level="WARNING"):
                places_module.initialize_places(connection)

        promoted_for_david = david.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][10]
        promoted_for_diana = diana.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][10]
        self.assertEqual(promoted_for_david, promoted_for_diana)
        self.assertEqual(promoted_for_david["name"], "November")
        self.assertEqual(promoted_for_david["version"], 3)
        self.assertEqual(david.get(promoted_for_david["image_url"]).status_code, 200)
        self.assertEqual(diana.get(promoted_for_diana["image_url"]).status_code, 200)
        cleared = david.put(
            "/api/places/margaritas/11",
            data={
                "name": "",
                "rating": "",
                "review": "",
                "remove_image": "true",
                "version": promoted_for_david["version"],
            },
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["version"], 4)
        with places_module.connect(places_module.DB_PATH) as connection:
            with self.assertLogs(places_module.LOGGER.name, level="WARNING"):
                places_module.initialize_places(connection)
        cleared_after_restart = diana.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][10]
        self.assertEqual(cleared_after_restart["name"], "")
        self.assertFalse(cleared_after_restart["has_image"])
        self.assertEqual(cleared_after_restart["version"], 4)
        self.assertFalse(cleared_after_restart["migration_conflict"])

        conflict = david.get(
            "/api/places/margaritas"
        ).get_json()["margaritas"][9]
        self.assertTrue(conflict["migration_conflict"])
        self.assertEqual(conflict["migration_status"], "review_required")
        self.assertFalse(conflict["has_image"])
        files_before = {path.name for path in places_module.IMAGE_ROOT.iterdir()}
        attempted_photo = BytesIO()
        Image.new("RGB", (16, 12), "red").save(attempted_photo, "PNG")
        attempted_photo.seek(0)
        denied = david.put(
            "/api/places/margaritas/10",
            data={
                "name": "Do not choose",
                "version": conflict["version"],
                "image": (attempted_photo, "attempt.png"),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(denied.status_code, 409)
        self.assertTrue(denied.get_json()["review_required"])
        self.assertEqual(
            {path.name for path in places_module.IMAGE_ROOT.iterdir()}, files_before
        )
        with places_module.connect(places_module.DB_PATH) as connection:
            canonical = connection.execute(
                "SELECT * FROM margaritas WHERE month=10"
            ).fetchone()
            self.assertFalse(places_module.margarita_row_populated(canonical))
            self.assertEqual(canonical["version"], 1)
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM margarita_records ORDER BY month,owner_id"
                    ).fetchall()
                ],
                personals_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM margarita_image_history WHERE month=10"
                ).fetchone()[0],
                0,
            )
        for name in (promoted_name, *conflict_names):
            self.assertTrue((places_module.IMAGE_ROOT / name).is_file())

    def test_household_modules_fail_closed_for_missing_and_unknown_identities(self):
        for login in (None, "outsider@example.test"):
            client = portal.app.test_client()
            if login:
                client.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = login
                client.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "Outsider"
            token = "outsider-csrf-token-with-more-than-32-characters"
            client.set_cookie("david_pi_csrf", token, domain="localhost")
            headers = {"X-CSRF-Token": token}
            for path in ("/recipes", "/movies", "/places"):
                self.assertEqual(client.get(path).status_code, 403, (login, path))
            for path in ("/api/recipes", "/api/movies", "/api/places/restaurants"):
                self.assertEqual(client.get(path).status_code, 403, (login, path))
            self.assertEqual(
                client.post("/api/recipes", json={"title": "Denied"}, headers=headers).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/places/restaurants",
                    json={"name": "Denied"},
                    headers=headers,
                ).status_code,
                403,
            )

    def test_household_locator_migrations_are_additive_and_leave_legacy_values(self):
        with tempfile.TemporaryDirectory() as directory:
            recipe_db = Path(directory) / "recipes.db"
            with sqlite3.connect(recipe_db) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    """CREATE TABLE recipes (
                       id TEXT PRIMARY KEY, title TEXT NOT NULL,
                       description TEXT NOT NULL DEFAULT '',
                       meal_type TEXT NOT NULL DEFAULT 'main',
                       tags_json TEXT NOT NULL DEFAULT '[]', total_minutes INTEGER,
                       servings TEXT, ingredients_json TEXT NOT NULL DEFAULT '[]',
                       instructions_json TEXT NOT NULL DEFAULT '[]', source_name TEXT,
                       source_url TEXT UNIQUE, image_url TEXT, content_hash TEXT NOT NULL,
                       favorite INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL,
                       created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                       imported_at TEXT, last_viewed_at TEXT, last_made_at TEXT,
                       deleted_at TEXT)"""
                )
                connection.execute(
                    """INSERT INTO recipes
                       (id,title,source_url,content_hash,created_by,created_at,updated_at)
                       VALUES ('legacy','Legacy','https://legacy.example/recipe','hash',
                               'legacy','then','then')"""
                )
                recipes_module.initialize_recipes(connection)
                recipe = connection.execute(
                    "SELECT source_url,catalog_source_url FROM recipes WHERE id='legacy'"
                ).fetchone()
                recipe_indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(recipes)")
                }
            self.assertEqual(
                (recipe["source_url"], recipe["catalog_source_url"]),
                ("https://legacy.example/recipe", None),
            )
            self.assertTrue({
                "recipes_shared_catalog_active_idx",
                "recipes_private_catalog_active_idx",
            }.issubset(recipe_indexes))

            movie_db = Path(directory) / "movies.db"
            with sqlite3.connect(movie_db) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute(
                    """CREATE TABLE movies (
                       id TEXT PRIMARY KEY, tmdb_id INTEGER UNIQUE, title TEXT NOT NULL,
                       release_year INTEGER, overview TEXT NOT NULL DEFAULT '', runtime INTEGER,
                       genres_json TEXT NOT NULL DEFAULT '[]', poster_url TEXT,
                       added_by TEXT NOT NULL, added_at TEXT NOT NULL, watched_at TEXT)"""
                )
                connection.execute(
                    """INSERT INTO movies
                       (id,tmdb_id,title,added_by,added_at)
                       VALUES ('legacy',12345,'Legacy','legacy','then')"""
                )
                movies_module.initialize_movies(connection)
                movie = connection.execute(
                    "SELECT tmdb_id,catalog_tmdb_id FROM movies WHERE id='legacy'"
                ).fetchone()
                movie_indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(movies)")
                }
            self.assertEqual((movie["tmdb_id"], movie["catalog_tmdb_id"]), (12345, None))
            self.assertTrue({
                "movies_shared_catalog_active_idx",
                "movies_private_catalog_active_idx",
            }.issubset(movie_indexes))

    def test_recipe_ownership_personal_state_stale_writes_and_restore(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        created = david.post(
            "/api/recipes",
            json={
                "title": "Shared soup", "meal_type": "main", "ingredients": ["Beans"],
                "instructions": ["Simmer"], "favorite": True, "visibility": "shared",
            },
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()["recipe"]
        recipe_id = created["id"]
        diana_copy = diana.get(f"/api/recipes/{recipe_id}").get_json()["recipe"]
        self.assertFalse(diana_copy["favorite"])
        denied_payload = {
            "title": "Hijacked soup", "meal_type": "main", "ingredients": ["Beans"],
            "instructions": ["Simmer"], "version": diana_copy["version"],
        }
        self.assertEqual(
            diana.put(
                f"/api/recipes/{recipe_id}", json=denied_payload,
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        self.assertEqual(
            diana.delete(
                f"/api/recipes/{recipe_id}", json={"version": diana_copy["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        personal = diana.post(
            f"/api/recipes/{recipe_id}/favorite",
            json={"favorite": True, "state_version": diana_copy["state_version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(personal.status_code, 200)
        self.assertTrue(personal.get_json()["favorite"])
        self.assertTrue(david.get(f"/api/recipes/{recipe_id}").get_json()["recipe"]["favorite"])
        david_personal = david.post(
            f"/api/recipes/{recipe_id}/favorite",
            json={"favorite": False, "state_version": created["state_version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_personal.status_code, 200)
        self.assertTrue(diana.get(f"/api/recipes/{recipe_id}").get_json()["recipe"]["favorite"])
        stale_state = diana.post(
            f"/api/recipes/{recipe_id}/favorite",
            json={"favorite": True, "state_version": personal.get_json()["state_version"] - 1},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(stale_state.status_code, 409)

        update_payload = {
            "title": "Shared bean soup", "meal_type": "main", "ingredients": ["Beans"],
            "instructions": ["Simmer"], "version": created["version"],
            "favorite": True, "state_version": david_personal.get_json()["state_version"],
        }
        updated = david.put(
            f"/api/recipes/{recipe_id}", json=update_payload,
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(updated.status_code, 200)
        stale = david.put(
            f"/api/recipes/{recipe_id}", json={**update_payload, "title": "Stale overwrite"},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(stale.status_code, 409)
        current = david.get(f"/api/recipes/{recipe_id}").get_json()["recipe"]
        self.assertEqual(current["title"], "Shared bean soup")

        trashed = david.delete(
            f"/api/recipes/{recipe_id}", json={"version": current["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        self.assertGreaterEqual(
            datetime.fromisoformat(trashed.get_json()["purge_after"]),
            datetime.now(timezone.utc) + timedelta(days=29),
        )
        self.assertEqual(david.get(f"/api/recipes/{recipe_id}").status_code, 404)
        trash = david.get("/api/recipes?view=trash").get_json()["recipes"]
        self.assertEqual([item["id"] for item in trash], [recipe_id])
        self.assertEqual(diana.get("/api/recipes?view=trash").get_json()["recipes"], [])
        restored = david.post(
            f"/api/recipes/{recipe_id}/restore", json={"version": trash[0]["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(david.get(f"/api/recipes/{recipe_id}").status_code, 200)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            actions = [row[0] for row in connection.execute(
                "SELECT action FROM mutation_audit WHERE domain='recipe' AND object_id=? ORDER BY id",
                (recipe_id,),
            )]
        self.assertEqual(actions[-4:], ["create", "update", "trash", "restore"])

    def test_private_recipe_stays_isolated_and_legacy_shared_recipe_is_collaborative(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        private_recipe = diana.post(
            "/api/recipes",
            json={
                "title": "Diana private", "meal_type": "main", "ingredients": ["Rice"],
                "instructions": ["Cook"], "visibility": "private",
            }, headers={"X-CSRF-Token": diana_csrf},
        ).get_json()["recipe"]
        self.assertEqual(david.get(f"/api/recipes/{private_recipe['id']}").status_code, 404)
        self.assertNotIn(
            private_recipe["id"],
            {item["id"] for item in david.get("/api/recipes").get_json()["recipes"]},
        )
        self.assertEqual(
            david.put(
                f"/api/recipes/{private_recipe['id']}",
                json={
                    "title": "Admin rewrite", "meal_type": "main", "ingredients": ["Rice"],
                    "instructions": ["Cook"], "version": private_recipe["version"],
                }, headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            404,
        )
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO recipes
                   (id,title,meal_type,ingredients_json,instructions_json,content_hash,
                    created_by,created_at,updated_at,owner_id,owner_name,visibility,version)
                   VALUES ('legacy-recipe','Legacy','main','[]','[]','legacy-hash',
                           'home','then','then',NULL,NULL,'shared',1)"""
            )
        legacy = david.get("/api/recipes/legacy-recipe").get_json()["recipe"]
        self.assertFalse(legacy["legacy_read_only"])
        self.assertTrue(legacy["can_edit"])
        david_update = david.put(
            "/api/recipes/legacy-recipe",
            json={
                "title": "David legacy edit", "meal_type": "main",
                "ingredients": [], "instructions": [], "version": legacy["version"],
            },
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_update.status_code, 200)
        diana_copy = diana.get("/api/recipes/legacy-recipe").get_json()["recipe"]
        self.assertTrue(diana_copy["can_edit"])
        diana_update = diana.put(
            "/api/recipes/legacy-recipe",
            json={
                "title": "Diana legacy edit", "meal_type": "main",
                "ingredients": [], "instructions": [], "version": diana_copy["version"],
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(diana_update.status_code, 200)
        self.assertEqual(diana_update.get_json()["recipe"]["title"], "Diana legacy edit")
        self.assertEqual(
            diana.put(
                "/api/recipes/legacy-recipe",
                json={
                    "title": "Private legacy", "meal_type": "main",
                    "ingredients": [], "instructions": [], "visibility": "private",
                    "version": diana_update.get_json()["recipe"]["version"],
                },
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            400,
        )
        outsider, outsider_csrf = self.allowlisted_client(
            "outsider@example.test", "Outsider"
        )
        self.assertEqual(
            outsider.put(
                "/api/recipes/legacy-recipe",
                json={
                    "title": "Outsider edit", "meal_type": "main",
                    "ingredients": [], "instructions": [],
                    "version": diana_update.get_json()["recipe"]["version"],
                },
                headers={"X-CSRF-Token": outsider_csrf},
            ).status_code,
            403,
        )
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            stored = connection.execute(
                "SELECT title,owner_id,visibility FROM recipes WHERE id='legacy-recipe'"
            ).fetchone()
        self.assertEqual(
            (stored["title"], stored["owner_id"], stored["visibility"]),
            ("Diana legacy edit", None, "shared"),
        )

    def test_movie_ownership_personal_state_and_restore_are_independent(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        movie = david.post(
            "/api/movies", json={"title": "Shared film", "visibility": "shared"},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()["movie"]
        movie_id = movie["id"]
        self.assertEqual(
            diana.delete(
                f"/api/movies/{movie_id}", json={"version": movie["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            403,
        )
        watched = diana.post(
            f"/api/movies/{movie_id}/watched",
            json={"watched": True, "state_version": None},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(watched.status_code, 200)
        self.assertEqual(diana.get("/api/movies?view=all").get_json()["movies"], [])
        self.assertEqual(
            [item["id"] for item in diana.get("/api/movies?view=watched").get_json()["movies"]],
            [movie_id],
        )
        self.assertEqual(
            [item["id"] for item in david.get("/api/movies?view=all").get_json()["movies"]],
            [movie_id],
        )
        stale = diana.post(
            f"/api/movies/{movie_id}/watched",
            json={"watched": False, "state_version": watched.get_json()["state_version"] - 1},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(stale.status_code, 409)
        unwatched = diana.post(
            f"/api/movies/{movie_id}/watched",
            json={"watched": False, "state_version": watched.get_json()["state_version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(unwatched.status_code, 200)

        self.assertEqual(
            david.delete(
                f"/api/movies/{movie_id}", json={"version": movie["version"] + 50},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            409,
        )

        david_services = david.put(
            "/api/movies/subscriptions", json={"provider_ids": [8], "version": None},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_services.status_code, 200)
        diana_services = diana.put(
            "/api/movies/subscriptions", json={"provider_ids": [15], "version": None},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(diana_services.status_code, 200)
        david_enabled = {
            item["provider_id"] for item in david.get("/api/movies/subscriptions").get_json()["subscriptions"]
            if item["enabled"]
        }
        diana_enabled = {
            item["provider_id"] for item in diana.get("/api/movies/subscriptions").get_json()["subscriptions"]
            if item["enabled"]
        }
        self.assertEqual(david_enabled, {8})
        self.assertEqual(diana_enabled, {15})

        trashed = david.delete(
            f"/api/movies/{movie_id}", json={"version": movie["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        trash = david.get("/api/movies?view=trash").get_json()["movies"]
        self.assertEqual([item["id"] for item in trash], [movie_id])
        self.assertEqual(diana.get("/api/movies?view=trash").get_json()["movies"], [])
        self.assertEqual(
            david.post(
                f"/api/movies/{movie_id}/restore", json={"version": trash[0]["version"]},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            200,
        )

    def test_places_are_one_shared_household_module_with_retained_photos(self):
        from PIL import Image
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        image = BytesIO()
        Image.new("RGB", (20, 20), "teal").save(image, "PNG")
        image.seek(0)
        restaurant = david.post(
            "/api/places/restaurants",
            data={
                "name": "Shared cafe", "cuisines": "Cafe",
                "owner_id": "attacker@example.test",
                "added_by_id": "attacker@example.test",
                "images": (image, "cafe.png"),
            },
            content_type="multipart/form-data", headers={"X-CSRF-Token": david_csrf},
        ).get_json()["restaurant"]
        restaurant_id = restaurant["id"]
        self.assertEqual(restaurant["visibility"], "shared")
        self.assertEqual(restaurant["added_by_id"], "david@example.test")
        david_list = david.get(
            "/api/places/restaurants?view=all"
        ).get_json()
        diana_list = diana.get(
            "/api/places/restaurants?view=all"
        ).get_json()
        self.assertEqual(david_list, diana_list)
        self.assertEqual(
            diana.get(restaurant["photos"][0]["url"]).data,
            david.get(restaurant["photos"][0]["url"]).data,
        )
        diana_photo = BytesIO()
        Image.new("RGB", (20, 20), "gold").save(diana_photo, "PNG")
        diana_photo.seek(0)
        edited = diana.put(
            f"/api/places/restaurants/{restaurant_id}",
            data={
                "name": "Shared cafe updated", "notes": "Joint list",
                "cuisines": ["Cafe", "Brunch"],
                "visibility": "private", "version": restaurant["version"],
                "images": (diana_photo, "diana.png"),
            },
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(edited.status_code, 200)
        edited_item = edited.get_json()["restaurant"]
        self.assertEqual(edited_item["visibility"], "shared")
        self.assertEqual(edited_item["owner_id"], "david@example.test")
        self.assertEqual(edited_item["added_by_id"], "david@example.test")
        self.assertEqual(len(edited_item["photos"]), 2)
        self.assertEqual(
            david.get("/api/places/restaurants?view=all").get_json(),
            diana.get("/api/places/restaurants?view=all").get_json(),
        )
        reviewed = diana.post(
            f"/api/places/restaurants/{restaurant_id}/review",
            json={"rating": 4.5, "review": "Lovely", "visited_at": "2026-08-01", "version": edited_item["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(reviewed.status_code, 200)
        reviewed_item = reviewed.get_json()["restaurant"]
        self.assertEqual(reviewed_item["reviewed_by_id"], "diana@example.test")
        rereviewed = david.post(
            f"/api/places/restaurants/{restaurant_id}/review",
            json={
                "rating": 5, "review": "Joint favorite",
                "visited_at": "2026-08-02", "version": reviewed_item["version"],
                "reviewed_by_id": "attacker@example.test",
            },
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(rereviewed.status_code, 200)
        reviewed_item = rereviewed.get_json()["restaurant"]
        self.assertEqual(reviewed_item["reviewed_by_id"], "david@example.test")
        david_choice = david.post(
            "/api/places/choose",
            json={"pool": "highly_rated", "cuisines": ["Brunch"]},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()
        diana_choice = diana.post(
            "/api/places/choose",
            json={"pool": "highly_rated", "cuisines": ["Brunch"]},
            headers={"X-CSRF-Token": diana_csrf},
        ).get_json()
        self.assertEqual(david_choice["pool_size"], diana_choice["pool_size"])
        self.assertEqual(david_choice["restaurant"]["id"], restaurant_id)
        self.assertEqual(diana_choice["restaurant"]["id"], restaurant_id)
        self.assertEqual(
            david.post(
                f"/api/places/restaurants/{restaurant_id}/review",
                json={
                    "rating": 2, "review": "Stale", "visited_at": "2026-08-02",
                    "version": restaurant["version"],
                },
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            409,
        )
        with places_module.connect(places_module.DB_PATH) as connection:
            image_name = connection.execute(
                "SELECT image_name FROM restaurant_photos WHERE restaurant_id=?",
                (restaurant_id,),
            ).fetchone()[0]
        stored_image = places_module.RESTAURANT_IMAGE_ROOT / image_name
        self.assertTrue(stored_image.is_file())
        trashed = diana.delete(
            f"/api/places/restaurants/{restaurant_id}",
            json={"version": reviewed_item["version"]}, headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        self.assertTrue(stored_image.is_file())
        david_trash = david.get("/api/places/restaurants?view=trash").get_json()
        diana_trash = diana.get("/api/places/restaurants?view=trash").get_json()
        self.assertEqual(david_trash, diana_trash)
        trash = david_trash["restaurants"]
        self.assertEqual([item["id"] for item in trash], [restaurant_id])
        self.assertEqual(trash[0]["photos"], [])
        self.assertEqual(
            david.post(
                f"/api/places/restaurants/{restaurant_id}/restore",
                json={"version": trash[0]["version"]}, headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            200,
        )
        restored = david.get("/api/places/restaurants?view=reviewed").get_json()["restaurants"][0]
        self.assertEqual(restored["photos"][0]["id"], restaurant["photos"][0]["id"])
        self.assertEqual(
            david.get("/api/places/restaurants?view=reviewed").get_json(),
            diana.get("/api/places/restaurants?view=reviewed").get_json(),
        )

        initial = david.get("/api/places/margaritas").get_json()["margaritas"][1]
        david_saved = david.put(
            "/api/places/margaritas/2",
            data={"name": "David marg", "version": initial["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_saved.status_code, 200)
        diana_view = diana.get("/api/places/margaritas").get_json()["margaritas"][1]
        self.assertEqual(diana_view["name"], "David marg")
        diana_saved = diana.put(
            "/api/places/margaritas/2",
            data={"name": "Diana marg", "version": diana_view["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(diana_saved.status_code, 200)
        david_view = david.get("/api/places/margaritas").get_json()["margaritas"][1]
        self.assertEqual(david_view["name"], "Diana marg")
        self.assertEqual(
            david_view,
            diana.get("/api/places/margaritas").get_json()["margaritas"][1],
        )

    def test_private_movies_stay_isolated_while_cached_private_places_become_shared(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        private_movie = diana.post(
            "/api/movies", json={"title": "Diana film", "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        ).get_json()["movie"]
        david_movie_ids = {
            item["id"] for item in david.get("/api/movies?view=all").get_json()["movies"]
        }
        self.assertNotIn(private_movie["id"], david_movie_ids)
        self.assertEqual(
            david.post(
                f"/api/movies/{private_movie['id']}/watched",
                json={"watched": True, "state_version": None},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            404,
        )
        self.assertEqual(
            david.delete(
                f"/api/movies/{private_movie['id']}",
                json={"version": private_movie["version"]},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            404,
        )
        with movies_module.connect(movies_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO movies
                   (id,title,overview,genres_json,added_by,added_at,watched_at,
                    media_type,owner_id,owner_name,visibility,version)
                   VALUES ('legacy-movie','Legacy film','','[]','Legacy','then','then',
                           'movie',NULL,NULL,'shared',1)"""
            )
        self.assertIn(
            "legacy-movie",
            {item["id"] for item in david.get("/api/movies?view=watched").get_json()["movies"]},
        )
        legacy_unwatched = david.post(
            "/api/movies/legacy-movie/watched",
            json={"watched": False, "state_version": None},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(legacy_unwatched.status_code, 200)
        legacy_item = next(
            item for item in david.get("/api/movies?view=all").get_json()["movies"]
            if item["id"] == "legacy-movie"
        )
        self.assertFalse(legacy_item["watched"])
        self.assertFalse(legacy_item["legacy_read_only"])
        self.assertTrue(legacy_item["can_edit"])
        legacy_movie_trashed = david.delete(
            "/api/movies/legacy-movie", json={"version": 1},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(legacy_movie_trashed.status_code, 200)
        diana_movie_trash = diana.get("/api/movies?view=trash").get_json()["movies"]
        self.assertEqual([item["id"] for item in diana_movie_trash], ["legacy-movie"])
        self.assertTrue(diana_movie_trash[0]["can_restore"])
        self.assertEqual(
            diana.post(
                "/api/movies/legacy-movie/restore",
                json={"version": diana_movie_trash[0]["version"]},
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code,
            200,
        )

        private_place = diana.post(
            "/api/places/restaurants",
            json={"name": "Diana cafe", "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        ).get_json()["restaurant"]
        self.assertEqual(private_place["visibility"], "shared")
        david_place_ids = {
            item["id"] for item in david.get("/api/places/restaurants?view=all").get_json()["restaurants"]
        }
        self.assertIn(private_place["id"], david_place_ids)
        david_update = david.put(
            f"/api/places/restaurants/{private_place['id']}",
            json={
                "name": "Household rewrite", "visibility": "private",
                "version": private_place["version"],
            },
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_update.status_code, 200)
        self.assertEqual(david_update.get_json()["restaurant"]["visibility"], "shared")
        with places_module.connect(places_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO restaurants
                   (id,name,cuisines_json,notes,status,review,added_by_name,
                    created_at,updated_at,owner_id,owner_name,visibility,version)
                   VALUES ('legacy-place','Legacy cafe','[]','','want_to_go','',
                           'Legacy','then','then',NULL,NULL,'shared',1)"""
            )
        legacy_place = next(
            item for item in david.get("/api/places/restaurants?view=all").get_json()["restaurants"]
            if item["id"] == "legacy-place"
        )
        self.assertFalse(legacy_place["legacy_read_only"])
        self.assertTrue(legacy_place["can_edit"])
        self.assertTrue(legacy_place["can_review"])
        david_place_update = david.put(
            "/api/places/restaurants/legacy-place",
            json={"name": "David legacy cafe", "version": 1},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(david_place_update.status_code, 200)
        diana_place_review = diana.post(
            "/api/places/restaurants/legacy-place/review",
            json={
                "rating": 4.5, "review": "Still shared",
                "visited_at": "2026-08-03",
                "version": david_place_update.get_json()["restaurant"]["version"],
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(diana_place_review.status_code, 200)
        legacy_place_trashed = diana.delete(
            "/api/places/restaurants/legacy-place",
            json={"version": diana_place_review.get_json()["restaurant"]["version"]},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(legacy_place_trashed.status_code, 200)
        david_place_trash = david.get(
            "/api/places/restaurants?view=trash"
        ).get_json()["restaurants"]
        self.assertEqual([item["id"] for item in david_place_trash], ["legacy-place"])
        self.assertEqual(
            david.post(
                "/api/places/restaurants/legacy-place/restore",
                json={"version": david_place_trash[0]["version"]},
                headers={"X-CSRF-Token": david_csrf},
            ).status_code,
            200,
        )

    def test_private_recipe_and_movie_catalog_locators_do_not_cross_users(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        source_url = "https://recipes.example.test/household-secret"
        with patch.object(recipes_module, "public_url", side_effect=lambda value: value):
            diana_recipe = diana.post(
                "/api/recipes",
                json={
                    "title": "Diana source", "meal_type": "main",
                    "ingredients": ["Diana"], "instructions": ["Cook"],
                    "source_url": source_url, "visibility": "private",
                },
                headers={"X-CSRF-Token": diana_csrf},
            )
            david_recipe = david.post(
                "/api/recipes",
                json={
                    "title": "David source", "meal_type": "main",
                    "ingredients": ["David"], "instructions": ["Cook"],
                    "source_url": source_url, "visibility": "private",
                },
                headers={"X-CSRF-Token": david_csrf},
            )
            same_owner_duplicate = david.post(
                "/api/recipes",
                json={
                    "title": "David duplicate", "meal_type": "main",
                    "ingredients": ["Different"], "instructions": ["Cook"],
                    "source_url": source_url, "visibility": "private",
                },
                headers={"X-CSRF-Token": david_csrf},
            )
        self.assertEqual(diana_recipe.status_code, 201)
        self.assertEqual(david_recipe.status_code, 201)
        self.assertEqual(same_owner_duplicate.status_code, 409)
        self.assertEqual(david_recipe.get_json()["recipe"]["source_url"], source_url)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            recipe_locators = connection.execute(
                "SELECT source_url,catalog_source_url FROM recipes ORDER BY owner_id"
            ).fetchall()
        self.assertEqual(
            [(row["source_url"], row["catalog_source_url"]) for row in recipe_locators],
            [(None, source_url), (None, source_url)],
        )
        shared_source = "https://recipes.example.test/shared"
        with patch.object(recipes_module, "public_url", side_effect=lambda value: value):
            self.assertEqual(diana.post(
                "/api/recipes",
                json={
                    "title": "Shared source", "meal_type": "main",
                    "ingredients": ["Shared"], "instructions": ["Cook"],
                    "source_url": shared_source, "visibility": "shared",
                },
                headers={"X-CSRF-Token": diana_csrf},
            ).status_code, 201)
            self.assertEqual(david.post(
                "/api/recipes",
                json={
                    "title": "Visible duplicate", "meal_type": "main",
                    "ingredients": ["Other"], "instructions": ["Cook"],
                    "source_url": shared_source, "visibility": "private",
                },
                headers={"X-CSRF-Token": david_csrf},
            ).status_code, 409)

        diana_movie = diana.post(
            "/api/movies",
            json={"title": "Diana title", "tmdb_id": 424242, "visibility": "private"},
            headers={"X-CSRF-Token": diana_csrf},
        )
        david_movie = david.post(
            "/api/movies",
            json={"title": "David title", "tmdb_id": 424242, "visibility": "private"},
            headers={"X-CSRF-Token": david_csrf},
        )
        movie_duplicate = david.post(
            "/api/movies",
            json={"title": "David duplicate", "tmdb_id": 424242, "visibility": "private"},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(diana_movie.status_code, 201)
        self.assertEqual(david_movie.status_code, 201)
        self.assertEqual(movie_duplicate.status_code, 409)
        self.assertEqual(david_movie.get_json()["movie"]["tmdb_id"], 424242)
        with movies_module.connect(movies_module.DB_PATH) as connection:
            movie_locators = connection.execute(
                "SELECT tmdb_id,catalog_tmdb_id FROM movies ORDER BY owner_id"
            ).fetchall()
        self.assertEqual(
            [(row["tmdb_id"], row["catalog_tmdb_id"]) for row in movie_locators],
            [(None, 424242), (None, 424242)],
        )
        self.assertEqual(diana.post(
            "/api/movies",
            json={"title": "Shared TMDB", "tmdb_id": 515151, "visibility": "shared"},
            headers={"X-CSRF-Token": diana_csrf},
        ).status_code, 201)
        self.assertEqual(david.post(
            "/api/movies",
            json={"title": "Visible TMDB duplicate", "tmdb_id": 515151, "visibility": "private"},
            headers={"X-CSRF-Token": david_csrf},
        ).status_code, 409)

    def test_private_modules_preserve_visibility_but_date_night_forces_household_shared(self):
        david, csrf = self.paired_client("home", "David")
        recipe = david.post(
            "/api/recipes",
            json={
                "title": "Private soup", "meal_type": "main",
                "ingredients": ["Beans"], "instructions": ["Cook"],
                "visibility": "private",
            },
            headers={"X-CSRF-Token": csrf},
        ).get_json()["recipe"]
        updated_recipe = david.put(
            f"/api/recipes/{recipe['id']}",
            json={
                "title": "Private bean soup", "meal_type": "main",
                "ingredients": ["Beans"], "instructions": ["Cook"],
                "version": recipe["version"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(updated_recipe.status_code, 200)
        current_recipe = updated_recipe.get_json()["recipe"]
        self.assertEqual(current_recipe["visibility"], "private")
        invalid_visibility = david.put(
            f"/api/recipes/{recipe['id']}",
            json={
                "title": "Published by typo", "meal_type": "main",
                "ingredients": ["Beans"], "instructions": ["Cook"],
                "version": current_recipe["version"], "visibility": "household",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(invalid_visibility.status_code, 400)
        invalid_boolean = david.put(
            f"/api/recipes/{recipe['id']}",
            json={
                "title": "Private bean soup", "meal_type": "main",
                "ingredients": ["Beans"], "instructions": ["Cook"],
                "version": current_recipe["version"], "favorite": "false",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(invalid_boolean.status_code, 400)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            stored_recipe = connection.execute(
                "SELECT visibility,version FROM recipes WHERE id=?", (recipe["id"],)
            ).fetchone()
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM recipe_activity WHERE recipe_id=?", (recipe["id"],)
            ).fetchone())
        self.assertEqual(
            (stored_recipe["visibility"], stored_recipe["version"]),
            ("private", current_recipe["version"]),
        )
        trashed = david.delete(
            f"/api/recipes/{recipe['id']}",
            json={"version": current_recipe["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(trashed.status_code, 200)
        deleted_recipe = david.get("/api/recipes?view=trash").get_json()["recipes"][0]
        self.assertFalse(deleted_recipe["can_edit"])
        self.assertTrue(deleted_recipe["can_restore"])

        place = david.post(
            "/api/places/restaurants",
            json={"name": "Private cafe", "cuisines": ["Cafe"], "visibility": "private"},
            headers={"X-CSRF-Token": csrf},
        ).get_json()["restaurant"]
        self.assertEqual(place["visibility"], "shared")
        updated_place = david.put(
            f"/api/places/restaurants/{place['id']}",
            json={"name": "Private cafe updated", "cuisines": ["Cafe"], "version": place["version"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(updated_place.status_code, 200)
        current_place = updated_place.get_json()["restaurant"]
        self.assertEqual(current_place["visibility"], "shared")
        invalid_place = david.put(
            f"/api/places/restaurants/{place['id']}",
            json={
                "name": "Private cafe updated", "cuisines": ["Cafe"],
                "version": current_place["version"], "visibility": "",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(invalid_place.status_code, 200)
        current_place = invalid_place.get_json()["restaurant"]
        self.assertEqual(current_place["visibility"], "shared")
        with places_module.connect(places_module.DB_PATH) as connection:
            stored_place = connection.execute(
                "SELECT visibility,version FROM restaurants WHERE id=?", (place["id"],)
            ).fetchone()
        self.assertEqual(
            (stored_place["visibility"], stored_place["version"]),
            ("shared", current_place["version"]),
        )

    def test_date_night_cuisines_and_legacy_reviews_are_household_shared(self):
        david, david_csrf = self.paired_client("home", "David")
        diana, diana_csrf = self.paired_client("diana", "Diana")
        private_cuisine = "Diana Moon Cuisine"
        private_place = diana.post(
            "/api/places/restaurants",
            json={
                "name": "Private moon cafe", "cuisines": [private_cuisine],
                "visibility": "private",
            },
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(private_place.status_code, 201)
        with places_module.connect(places_module.DB_PATH) as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM cuisine_categories WHERE normalized_name=?",
                (private_cuisine.casefold(),),
            ).fetchone())
        self.assertIn(
            private_cuisine,
            diana.get("/api/places/restaurants?view=all").get_json()["cuisines"],
        )
        self.assertIn(
            private_cuisine,
            david.get("/api/places/restaurants?view=all").get_json()["cuisines"],
        )
        self.assertIn(private_cuisine, diana.get("/places").get_data(as_text=True))
        self.assertIn(private_cuisine, david.get("/places").get_data(as_text=True))
        with places_module.connect(places_module.DB_PATH) as connection:
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM cuisine_categories WHERE normalized_name=?",
                (private_cuisine.casefold(),),
            ).fetchone())
            connection.execute(
                """INSERT INTO restaurants
                   (id,name,cuisines_json,notes,status,rating,review,added_by_name,
                    reviewed_by_id,reviewed_by_name,created_at,updated_at,
                    owner_id,owner_name,visibility,version)
                   VALUES ('reviewed-legacy','Legacy reviewed cafe','[]','',
                           'reviewed',4,'Original','Legacy','david@example.test',
                           'David','then','then',NULL,NULL,'shared',1)"""
            )
        updated = david.post(
            "/api/places/restaurants/reviewed-legacy/review",
            json={"rating": 1, "review": "Rewritten", "visited_at": "2026-08-03", "version": 1},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertFalse(updated.get_json()["restaurant"]["legacy_read_only"])
        with places_module.connect(places_module.DB_PATH) as connection:
            legacy = connection.execute(
                "SELECT rating,review,version FROM restaurants WHERE id='reviewed-legacy'"
            ).fetchone()
        self.assertEqual((legacy["rating"], legacy["review"], legacy["version"]), (1, "Rewritten", 2))

    def test_recipe_recommendation_history_and_audit_commit_atomically(self):
        david, csrf = self.paired_client("home", "David")
        recipe = david.post(
            "/api/recipes",
            json={
                "title": "Audited soup", "meal_type": "main",
                "ingredients": ["Beans"], "instructions": ["Cook"],
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(recipe.status_code, 201)
        normal = david.post(
            "/api/recipes/recommend", json={"meal": "main"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(normal.status_code, 200)
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            recommendation_count = connection.execute(
                "SELECT COUNT(*) FROM recipe_recommendations"
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM mutation_audit WHERE domain='recipe_recommendation'"
            ).fetchone()[0]
        self.assertEqual(recommendation_count, 1)
        self.assertGreaterEqual(audit_count, 1)
        with patch.object(
            recipes_module, "audit_mutation", side_effect=RuntimeError("audit failed")
        ):
            with self.assertRaises(RuntimeError):
                david.post(
                    "/api/recipes/recommend", json={"meal": "main"},
                    headers={"X-CSRF-Token": csrf},
                )
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM recipe_recommendations").fetchone()[0],
                recommendation_count,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM mutation_audit WHERE domain='recipe_recommendation'"
                ).fetchone()[0],
                audit_count,
            )

    def test_production_principal_filter_invalid_tmdb_and_restore_conflicts(self):
        production = portal.app.test_client()
        production.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "david@example.test"
        production.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = "David Smith"
        csrf = "production-test-csrf-token-with-more-than-32-characters"
        production.set_cookie("david_pi_csrf", csrf, domain="localhost")
        created = production.post(
            "/api/movies", json={"title": "Production-owned"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        david_view = production.get("/api/movies?view=owner:david@example.test").get_json()["movies"]
        self.assertEqual([item["id"] for item in david_view], [created.get_json()["movie"]["id"]])
        self.assertEqual(production.get("/api/movies?view=owner:diana@example.test").get_json()["movies"], [])
        for invalid in (True, -1, "not-a-number", 12.5, 9_223_372_036_854_775_808):
            response = production.post(
                "/api/movies", json={"title": "Invalid TMDB", "tmdb_id": invalid},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(response.status_code, 400, invalid)

        david, david_csrf = self.paired_client("home", "David")
        with patch.object(recipes_module, "public_url", side_effect=lambda value: value):
            first_recipe = david.post(
                "/api/recipes",
                json={
                    "title": "Restore recipe", "meal_type": "main",
                    "ingredients": ["One"], "instructions": ["Cook"],
                    "source_url": "https://recipes.example.test/restore",
                },
                headers={"X-CSRF-Token": david_csrf},
            ).get_json()["recipe"]
            trashed_recipe = david.delete(
                f"/api/recipes/{first_recipe['id']}",
                json={"version": first_recipe["version"]},
                headers={"X-CSRF-Token": david_csrf},
            ).get_json()
            replacement_recipe = david.post(
                "/api/recipes",
                json={
                    "title": "Restore replacement", "meal_type": "main",
                    "ingredients": ["Two"], "instructions": ["Cook"],
                    "source_url": "https://recipes.example.test/restore",
                },
                headers={"X-CSRF-Token": david_csrf},
            )
        self.assertEqual(replacement_recipe.status_code, 201)
        recipe_restore = david.post(
            f"/api/recipes/{first_recipe['id']}/restore",
            json={"version": trashed_recipe["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(recipe_restore.status_code, 409)
        self.assertTrue(recipe_restore.get_json()["duplicate"])

        first_movie = david.post(
            "/api/movies", json={"title": "Restore movie", "tmdb_id": 987654},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()["movie"]
        trashed_movie = david.delete(
            f"/api/movies/{first_movie['id']}", json={"version": first_movie["version"]},
            headers={"X-CSRF-Token": david_csrf},
        ).get_json()
        replacement_movie = david.post(
            "/api/movies", json={"title": "Restore replacement", "tmdb_id": 987654},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(replacement_movie.status_code, 201)
        movie_restore = david.post(
            f"/api/movies/{first_movie['id']}/restore",
            json={"version": trashed_movie["version"]},
            headers={"X-CSRF-Token": david_csrf},
        )
        self.assertEqual(movie_restore.status_code, 409)
        self.assertTrue(movie_restore.get_json()["duplicate"])

    def test_restaurant_photo_symlink_is_neither_served_nor_followed_on_cleanup(self):
        target_directory = Path(tempfile.mkdtemp(prefix="david-pi-place-target-"))
        target = target_directory / "outside.jpg"
        target.write_bytes(b"private target bytes")
        link = places_module.RESTAURANT_IMAGE_ROOT / "linked.jpg"
        link.symlink_to(target)
        try:
            with places_module.connect(places_module.DB_PATH) as connection:
                connection.execute(
                    """INSERT INTO restaurants
                       (id,name,cuisines_json,notes,status,review,added_by_name,
                        created_at,updated_at,owner_id,owner_name,visibility,version)
                       VALUES ('linked-place','Linked cafe','[]','','want_to_go','',
                               'David','then','then','david@example.test','David','shared',1)"""
                )
                connection.execute(
                    """INSERT INTO restaurant_photos
                       (id,restaurant_id,image_name,sort_order,created_at,version)
                       VALUES ('linked-photo','linked-place','linked.jpg',0,'then',1)"""
                )
            served = self.client.get(
                "/api/places/restaurants/linked-place/photos/linked-photo"
            )
            self.assertEqual(served.status_code, 404)
            self.assertTrue(places_module.unlink_managed_image(
                places_module.RESTAURANT_IMAGE_ROOT, "linked.jpg"
            ))
            self.assertFalse(link.exists())
            self.assertEqual(target.read_bytes(), b"private target bytes")
        finally:
            link.unlink(missing_ok=True)
            shutil.rmtree(target_directory)

    def test_restaurant_file_publication_rolls_back_cleanly_when_audit_fails(self):
        from PIL import Image
        image = BytesIO()
        Image.new("RGB", (16, 16), "plum").save(image, "PNG")
        image.seek(0)
        before_files = {path.name for path in places_module.RESTAURANT_IMAGE_ROOT.iterdir()}
        with patch.object(places_module, "audit_mutation", side_effect=RuntimeError("audit failed")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    "/api/places/restaurants",
                    data={"name": "Rollback cafe", "images": (image, "rollback.png")},
                    content_type="multipart/form-data",
                )
        with places_module.connect(places_module.DB_PATH) as connection:
            self.assertIsNone(
                connection.execute("SELECT 1 FROM restaurants WHERE name='Rollback cafe'").fetchone()
            )
        self.assertEqual(
            {path.name for path in places_module.RESTAURANT_IMAGE_ROOT.iterdir()}, before_files
        )

    def test_device_reconciliation_requires_a_strict_complete_integrity_receipt(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        self.add_device_media_record(
            paired["device_id"], "image:visible", "strict-reconcile-photo"
        )
        with portal.db() as connection:
            contact_before = connection.execute(
                "SELECT last_contact_at FROM backup_devices WHERE id=?",
                (paired["device_id"],),
            ).fetchone()[0]
            photo_before = dict(connection.execute(
                "SELECT * FROM photos WHERE id=?", ("strict-reconcile-photo",)
            ).fetchone())

        scan_id = "01890f3a-1234-7abc-8def-000000000010"
        incomplete = self.reconciliation_payload(scan_id, [], complete=False)
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/reconcile", json=incomplete, headers=auth
            ).status_code,
            422,
        )
        malformed = self.reconciliation_payload(scan_id, ["image:visible"])
        malformed["ids_sha256"] = "0" * 64
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/reconcile", json=malformed, headers=auth
            ).status_code,
            422,
        )
        extra = self.reconciliation_payload(scan_id, ["image:visible"])
        extra["owner_id"] = "diana@example.test"
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/reconcile", json=extra, headers=auth
            ).status_code,
            422,
        )
        future = self.reconciliation_payload(
            "ffffffff-ffff-7abc-8def-000000000010", ["image:visible"]
        )
        with patch.object(
            device_backup_module.time, "time", return_value=1_700_000_000.0
        ):
            future_response = self.client.post(
                "/api/v1/device-backup/reconcile", json=future, headers=auth
            )
        self.assertEqual(future_response.status_code, 422)
        self.assertEqual(
            future_response.get_json()["error"],
            {
                "code": "scan_clock_skew",
                "message": "The phone clock is too far ahead of David-Pi.",
                "scan_id": future["scan_id"],
                "server_time_ms": 1_700_000_000_000,
                "max_future_skew_ms": 5 * 60 * 1000,
            },
        )
        oversized_items = self.reconciliation_payload(
            scan_id, ["image:visible", "image:second", "image:third"]
        )
        with patch.object(device_backup_module, "RECONCILIATION_MAX_ITEMS", 2):
            self.assertEqual(
                self.client.post(
                    "/api/v1/device-backup/reconcile",
                    json=oversized_items,
                    headers=auth,
                ).status_code,
                413,
            )
        with patch.object(device_backup_module, "RECONCILIATION_MAX_BODY_BYTES", 64):
            bounded = self.client.post(
                "/api/v1/device-backup/reconcile",
                data=b"{" + b" " * 128 + b"}",
                content_type="application/json",
                headers=auth,
            )
            unauthenticated_bounded = self.client.post(
                "/api/v1/device-backup/reconcile",
                data=b"{" + b" " * 128 + b"}",
                content_type="application/json",
                headers={"Authorization": "Bearer " + "x" * 48},
            )
        self.assertEqual(bounded.status_code, 413)
        self.assertEqual(unauthenticated_bounded.status_code, 401)
        with portal.db() as connection:
            record = connection.execute(
                "SELECT local_source_visible FROM device_media_records WHERE device_id=?",
                (paired["device_id"],),
            ).fetchone()[0]
            contact_after = connection.execute(
                "SELECT last_contact_at FROM backup_devices WHERE id=?",
                (paired["device_id"],),
            ).fetchone()[0]
            receipts = connection.execute(
                "SELECT COUNT(*) FROM device_reconciliation_receipts"
            ).fetchone()[0]
            photo_after = dict(connection.execute(
                "SELECT * FROM photos WHERE id=?", ("strict-reconcile-photo",)
            ).fetchone())
        self.assertEqual(record, 1)
        self.assertEqual(contact_after, contact_before)
        self.assertEqual(receipts, 0)
        self.assertEqual(photo_after, photo_before)



    def test_complete_scan_retires_only_same_device_absent_incomplete_sessions(self):
        david = self.pair_backup_device()
        david_auth = {"Authorization": f"Bearer {david['device_credential']}"}
        diana_client, diana_csrf = self.paired_client(profile="diana")
        diana = self.pair_backup_device(
            client=diana_client,
            owner="diana@example.test",
            name="Diana",
            csrf=diana_csrf,
        )
        diana_auth = {"Authorization": f"Bearer {diana['device_credential']}"}
        with patch.dict(os.environ, {"DAVID_PI_BACKUP_GLOBAL_UPLOADS": "2"}):
            david_session = self.client.post(
                "/api/v1/device-backup/uploads",
                json={
                    "client_item_id": "image:old-source",
                    "original_filename": "old.jpg",
                    "byte_size": 101,
                    "sha256": "e" * 64,
                    "mime_type": "image/jpeg",
                },
                headers=david_auth,
            )
            diana_session = diana_client.post(
                "/api/v1/device-backup/uploads",
                json={
                    "client_item_id": "image:diana-current",
                    "original_filename": "diana.jpg",
                    "byte_size": 102,
                    "sha256": "f" * 64,
                    "mime_type": "image/jpeg",
                },
                headers=diana_auth,
            )
        self.assertEqual(david_session.status_code, 201)
        self.assertEqual(diana_session.status_code, 201)
        david_upload_id = david_session.get_json()["upload_id"]
        diana_upload_id = diana_session.get_json()["upload_id"]
        with portal.db() as connection:
            paths = {
                row["id"]: Path(row["part_path"])
                for row in connection.execute(
                    "SELECT id,part_path FROM device_uploads WHERE id IN (?,?)",
                    (david_upload_id, diana_upload_id),
                )
            }

        payload = self.reconciliation_payload(
            "01890f3a-1234-7abc-8def-000000000012",
            ["image:new-source"],
        )
        accepted = self.client.post(
            "/api/v1/device-backup/reconcile", json=payload, headers=david_auth
        )

        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertFalse(paths[david_upload_id].exists())
        self.assertTrue(paths[diana_upload_id].is_file())
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE id=?", (david_upload_id,)
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE id=?", (diana_upload_id,)
                ).fetchone()[0],
                1,
            )
        replay = self.client.post(
            "/api/v1/device-backup/reconcile", json=payload, headers=david_auth
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["state"], "replayed")

    def test_complete_scan_cleanup_failure_rolls_back_receipt_and_session(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:ambiguous-create",
                "original_filename": "ambiguous.jpg",
                "byte_size": 103,
                "sha256": "1" * 64,
                "mime_type": "image/jpeg",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.get_json()["upload_id"]
        with portal.db() as connection:
            part = Path(connection.execute(
                "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
            ).fetchone()["part_path"])
            contact_before = connection.execute(
                "SELECT last_contact_at FROM backup_devices WHERE id=?",
                (paired["device_id"],),
            ).fetchone()[0]
        payload = self.reconciliation_payload(
            "01890f3a-1234-7abc-8def-000000000013",
            ["image:replacement-source"],
        )
        with patch.object(
            device_backup_module.os,
            "replace",
            side_effect=OSError("simulated staging failure"),
        ):
            failed = self.client.post(
                "/api/v1/device-backup/reconcile", json=payload, headers=auth
            )

        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.get_json()["error"]["code"], "upload_cleanup_failed")
        self.assertTrue(part.is_file())
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_reconciliation_receipts WHERE device_id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT last_contact_at FROM backup_devices WHERE id=?",
                    (paired["device_id"],),
                ).fetchone()[0],
                contact_before,
            )

    def test_complete_scan_replay_adopts_exact_same_scan_retirement_after_crash(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        created = self.client.post(
            "/api/v1/device-backup/uploads",
            json={
                "client_item_id": "image:retirement-crash",
                "original_filename": "retirement.jpg",
                "byte_size": 17,
                "sha256": "7" * 64,
                "mime_type": "image/jpeg",
            },
            headers=auth,
        )
        self.assertEqual(created.status_code, 201)
        upload_id = created.get_json()["upload_id"]
        scan_id = "01890f3a-1234-7abc-8def-000000000014"
        with portal.db() as connection:
            part = Path(
                connection.execute(
                    "SELECT part_path FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()["part_path"]
            )
        retiring = part.with_name(f"{part.name}.retiring-{scan_id}")
        os.replace(part, retiring)

        accepted = self.client.post(
            "/api/v1/device-backup/reconcile",
            json=self.reconciliation_payload(scan_id, []),
            headers=auth,
        )

        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertFalse(retiring.exists())
        with portal.db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM device_uploads WHERE id=?", (upload_id,)
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_reconciliation_receipts WHERE scan_id=?",
                    (scan_id,),
                ).fetchone()[0],
                1,
            )

    def test_device_reconciliation_is_atomic_idempotent_and_observation_only(self):
        paired = self.pair_backup_device()
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        self.add_device_media_record(
            paired["device_id"], "image:visible", "reconcile-visible-photo"
        )
        self.add_device_media_record(
            paired["device_id"], "video:missing", "reconcile-missing-photo"
        )
        protected_file = files_module.OBJECTS / "reconciliation-control.txt"
        protected_file.write_bytes(b"canonical-file-must-not-change")
        with files_module.connect(files_module.DB_PATH) as connection:
            connection.execute(
                """INSERT INTO stored_files
                   (id,name,folder_id,stored_name,content_type,byte_size,sha256,
                    created_at,updated_at,uploaded_by,deleted_at,owner_id,owner_name,
                    visibility,version)
                   VALUES ('reconciliation-control','control.txt',NULL,
                           'reconciliation-control.txt','text/plain',30,?,?,?,
                           'David',NULL,'david@example.test','David','private',1)""",
                (
                    hashlib.sha256(b"canonical-file-must-not-change").hexdigest(),
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            file_before = tuple(connection.execute(
                "SELECT * FROM stored_files WHERE id='reconciliation-control'"
            ).fetchone())
        scan_id = "01890f3a-1234-7abc-8def-000000000020"
        payload = self.reconciliation_payload(
            scan_id, ["image:visible", "image:visible", "unmatched:local"]
        )
        with portal.db() as connection:
            photos_before = [tuple(row) for row in connection.execute(
                "SELECT * FROM photos ORDER BY id"
            )]
        accepted = self.client.post(
            "/api/v1/device-backup/reconcile", json=payload, headers=auth
        )
        self.assertEqual(accepted.status_code, 200, accepted.get_data(as_text=True))
        self.assertEqual(accepted.get_json()["state"], "accepted")
        self.assertEqual(accepted.get_json()["item_count"], 2)
        replay = self.client.post(
            "/api/v1/device-backup/reconcile", json=payload, headers=auth
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()["state"], "replayed")

        conflict = self.reconciliation_payload(scan_id, ["video:missing"])
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/reconcile", json=conflict, headers=auth
            ).status_code,
            409,
        )
        stale = self.reconciliation_payload(
            "01890f3a-1233-7abc-8def-000000000020", ["image:visible"]
        )
        stale_response = self.client.post(
            "/api/v1/device-backup/reconcile", json=stale, headers=auth
        )
        self.assertEqual(stale_response.status_code, 409)
        self.assertEqual(stale_response.get_json()["error"]["code"], "stale_scan")
        throttled = self.reconciliation_payload(
            "01890f3a-1235-7abc-8def-000000000020", ["image:visible"]
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/device-backup/reconcile", json=throttled, headers=auth
            ).status_code,
            429,
        )
        with portal.db() as connection:
            visibility = dict(connection.execute(
                "SELECT client_item_id,local_source_visible FROM device_media_records"
            ))
            photos_after = [tuple(row) for row in connection.execute(
                "SELECT * FROM photos ORDER BY id"
            )]
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_reconciliation_receipts"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_backup_events "
                    "WHERE event_code='reconciliation_accepted'"
                ).fetchone()[0],
                1,
            )
        with files_module.connect(files_module.DB_PATH) as connection:
            file_after = tuple(connection.execute(
                "SELECT * FROM stored_files WHERE id='reconciliation-control'"
            ).fetchone())
        self.assertEqual(visibility, {"image:visible": 1, "video:missing": 0})
        self.assertEqual(photos_after, photos_before)
        self.assertEqual(file_after, file_before)
        self.assertEqual(protected_file.read_bytes(), b"canonical-file-must-not-change")

    def test_device_reconciliation_is_device_and_owner_bound_even_when_ids_overlap(self):
        david = self.pair_backup_device()
        diana_token = "diana-device-credential-with-more-than-32-characters"
        diana_device = "diana-reconciliation-device"
        with portal.db() as connection:
            connection.execute(
                """INSERT INTO backup_devices
                   (id,credential_hash,owner_user_id,owner_name,display_name,platform,
                    created_at,last_contact_at)
                   VALUES (?,?,?,?,?,'android',?,?)""",
                (
                    diana_device, device_backup_module.token_hash(diana_token),
                    "diana@example.test", "Diana", "Diana's phone",
                    "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                ),
            )
        self.add_device_media_record(
            david["device_id"], "shared-local-id", "david-reconcile-photo"
        )
        self.add_device_media_record(
            diana_device, "shared-local-id", "diana-reconcile-photo",
            owner_id="diana@example.test",
        )
        # Even a pre-existing inconsistent provenance row cannot cross the
        # structural owner predicate in reconciliation.
        self.add_device_media_record(
            david["device_id"], "misbound-local-id", "misbound-reconcile-photo",
            owner_id="diana@example.test",
        )
        david_payload = self.reconciliation_payload(
            "01890f3a-1234-7abc-8def-000000000030", []
        )
        accepted = self.client.post(
            "/api/v1/device-backup/reconcile",
            json=david_payload,
            headers={"Authorization": f"Bearer {david['device_credential']}"},
        )
        self.assertEqual(accepted.status_code, 200)
        with portal.db() as connection:
            visible = {
                (row["device_id"], row["client_item_id"]): row["local_source_visible"]
                for row in connection.execute(
                    "SELECT device_id,client_item_id,local_source_visible "
                    "FROM device_media_records"
                )
            }
            connection.execute(
                "UPDATE backup_devices SET revoked_at=? WHERE id=?",
                ("2026-01-02T00:00:00+00:00", diana_device),
            )
        self.assertEqual(
            visible,
            {
                (david["device_id"], "shared-local-id"): 0,
                (david["device_id"], "misbound-local-id"): 1,
                (diana_device, "shared-local-id"): 1,
            },
        )
        denied = self.client.post(
            "/api/v1/device-backup/reconcile",
            json=self.reconciliation_payload(
                "01890f3a-1234-7abc-8def-000000000031", []
            ),
            headers={"Authorization": f"Bearer {diana_token}"},
        )
        self.assertEqual(denied.status_code, 401)
        with portal.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT local_source_visible FROM device_media_records "
                    "WHERE device_id=?", (diana_device,)
                ).fetchone()[0],
                1,
            )

    def test_device_reconciliation_supports_more_than_5000_without_truncation(self):
        paired = self.pair_backup_device()
        visible_ids = [f"image:{index}" for index in range(5001)]
        response = self.client.post(
            "/api/v1/device-backup/reconcile",
            json=self.reconciliation_payload(
                "01890f3a-1234-7abc-8def-000000000040", visible_ids
            ),
            headers={"Authorization": f"Bearer {paired['device_credential']}"},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["item_count"], 5001)
        with portal.db() as connection:
            receipt = connection.execute(
                "SELECT item_count,ids_sha256 FROM device_reconciliation_receipts"
            ).fetchone()
        self.assertEqual(receipt["item_count"], 5001)
        self.assertEqual(
            receipt["ids_sha256"],
            device_backup_module.reconciliation_ids_sha256(sorted(visible_ids)),
        )

    @staticmethod
    def _record_initialize_error(errors):
        try:
            portal.initialize()
        except Exception as error:
            errors.append(error)

    @staticmethod
    def _record_migration_error(errors, path, migration):
        try:
            notes_module.migrate(path, migration)
        except Exception as error:
            errors.append(error)


class RecipePlannerTest(unittest.TestCase):
    def test_bulk_item_changes_are_atomic_and_versioned(self):
        self.week = '2026-10-05'
        rid = self.recipe()
        plan = self.change(0, action='add_recipe', recipe_id=rid).json['plan']
        ids = [i['id'] for i in plan['items']]
        self.assertEqual(self.change(1, action='edit_items', item_ids=ids+['missing'], checked=True).status_code, 409)
        self.assertEqual(self.change(1, action='edit_items', item_ids=ids, checked='yes').status_code, 400)
        plan = self.change(1, action='edit_items', item_ids=ids, checked=True).json['plan']
        self.assertTrue(all(i['checked'] for i in plan['items']))
        self.assertEqual(self.change(1, action='edit_items', item_ids=ids, removed=True).status_code, 409)
        plan = self.change(2, action='edit_items', item_ids=ids, removed=True).json['plan']
        self.assertTrue(all(i['removed'] for i in plan['items']))
        plan = self.change(3, action='edit_items', item_ids=ids, removed=False).json['plan']
        self.assertFalse(any(i['removed'] for i in plan['items']))

    def test_planner_has_its_own_page_and_library_stays_uncluttered(self):
        library = self.client.get('/recipes')
        self.assertEqual(library.status_code, 200)
        self.assertIn(b'href="/recipes/weekly-plan"', library.data)
        self.assertNotIn(b'id="weeklyPlanner"', library.data)
        self.assertNotIn(b'/static/recipe-planner.js', library.data)
        planner = self.client.get('/recipes/weekly-plan')
        self.assertEqual(planner.status_code, 200)
        self.assertIn(b'id="weeklyPlanner"', planner.data)
        self.assertIn(b'href="/recipes"', planner.data)
        self.assertNotIn(b'/static/recipes.js', planner.data)

    def setUp(self):
        PortalTestCase.setUp(self)
        self.week = '2026-09-07' if 'shared_generation' in self._testMethodName else '2026-09-14' if 'private_recipe' in self._testMethodName else '2026-09-21'

    def recipe(self, title='Dinner', visibility='shared'):
        response = self.client.post('/api/recipes', json={'title':title, 'ingredients':['1 cup rice','2 onions'], 'instructions':['Cook.'], 'visibility':visibility})
        self.assertEqual(response.status_code, 201, response.json)
        return response.json['recipe']['id']

    def change(self, version, **action):
        return self.client.post('/api/recipes/weekly-plan', json={'week':self.week,'version':version,**action})

    def test_shared_generation_edits_survive_and_conflicts_do_not_overwrite(self):
        rid=self.recipe()
        p=self.change(0,action='add_recipe',recipe_id=rid)
        self.assertEqual(p.status_code,200,p.json)
        p=p.json['plan']; self.assertEqual(p['week'],'2026-09-07')
        self.assertEqual([i['text'] for i in p['items']],['1 cup rice','2 onions'])
        duplicate=self.change(1,action='add_recipe',recipe_id=rid).json['plan']
        self.assertEqual(duplicate,p)
        p=self.change(1,action='edit_item',item_id=p['items'][0]['id'],text='2 cups rice',removed=True).json['plan']
        self.assertEqual(self.change(1,action='add_item',text='stale').status_code,409)
        other=self.recipe('Other dinner')
        p=self.change(2,action='add_recipe',recipe_id=other).json['plan']
        self.assertEqual(p['items'][0]['text'],'2 cups rice');self.assertTrue(p['items'][0]['removed'])
        original=self.client.get('/api/recipes/'+rid).json['recipe']
        self.assertEqual(original['ingredients'],['1 cup rice','2 onions'])
        self.client.environ_base['HTTP_X_TEST_TAILSCALE_LOGIN']='diana@example.test'
        read=self.client.get('/api/recipes/weekly-plan?week=2026-09-07')
        self.assertEqual(read.status_code,200,read.json)
        self.assertEqual(read.json['plan'],p)
        p=self.change(3,action='add_item',text='Milk').json['plan']
        self.assertEqual(p['items'][-1]['text'],'Milk')

    def test_private_recipe_never_copied_or_returned_after_visibility_change(self):
        private=self.recipe('Private dish','private')
        self.assertEqual(self.change(0,action='add_recipe',recipe_id=private).status_code,404)
        shared=self.recipe()
        self.change(0,action='add_recipe',recipe_id=shared)
        with recipes_module.connect(recipes_module.DB_PATH) as c:
            c.execute("UPDATE recipes SET visibility='private' WHERE id=?",(shared,))
        p=self.client.get('/api/recipes/weekly-plan?week='+self.week).json['plan']
        self.assertEqual(p['recipes'],[]);self.assertEqual(p['items'],[])

    def test_validation_csrf_and_remove_restore(self):
        self.assertEqual(self.change(0,action='add_item',text='').status_code,400)
        self.assertEqual(self.client.post('/api/recipes/weekly-plan',json=[],headers={'X-CSRF-Token':self.csrf}).status_code,400)
        p=self.change(0,action='add_item',text='Milk').json['plan']; iid=p['items'][0]['id']
        self.assertEqual(self.change(1,action='edit_item',item_id=iid,checked='yes').status_code,400)
        p=self.change(1,action='edit_item',item_id=iid,removed=True).json['plan']
        self.assertTrue(p['items'][0]['removed'])
        p=self.change(2,action='edit_item',item_id=iid,removed=False,checked=True).json['plan']
        self.assertFalse(p['items'][0]['removed']);self.assertTrue(p['items'][0]['checked'])
        self.assertEqual(self.client.post('/api/recipes/weekly-plan',json={'action':'add_item'},headers={'X-CSRF-Token':'bad'}).status_code,403)
        self.assertEqual(self.client.post('/api/recipes/weekly-plan',json={'text':'x'*17000}).status_code,413)


if __name__ == "__main__":
    unittest.main()
