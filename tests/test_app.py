import importlib
import os
import sys
import tempfile
import threading
import json
import unittest
import zipfile
import base64
import shutil
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
places_module = importlib.import_module("modules.places")
audiobooks_module = importlib.import_module("modules.audiobooks")
audiobook_streaming_module = importlib.import_module("modules.audiobook_streaming")
games_module = importlib.import_module("modules.games")
device_backup_module = importlib.import_module("modules.device_backup")
chat_module = importlib.import_module("modules.chat")
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
            connection.execute("DELETE FROM collection_photos")
            connection.execute("DELETE FROM collections")
            connection.execute("DELETE FROM photos")
        with notes_module.connect(notes_module.DB_PATH) as connection:
            connection.execute("DELETE FROM notes")
        with movies_module.connect(movies_module.DB_PATH) as connection:
            connection.execute("DELETE FROM availability")
            connection.execute("DELETE FROM movies")
            connection.execute("UPDATE subscriptions SET enabled = 0")
            connection.execute("DELETE FROM movie_settings")
        with recipes_module.connect(recipes_module.DB_PATH) as connection:
            connection.execute("DELETE FROM recipe_recommendations")
            connection.execute("DELETE FROM recipes")
        with files_module.connect(files_module.DB_PATH) as connection:
            connection.execute("DELETE FROM stored_files")
            connection.execute("DELETE FROM file_folders")
        with places_module.connect(places_module.DB_PATH) as connection:
            connection.execute("DELETE FROM restaurants")
            connection.execute(
                "DELETE FROM cuisine_categories WHERE normalized_name NOT IN (%s)"
                % ",".join("?" for _ in places_module.CUISINES),
                tuple(name.casefold() for name in places_module.CUISINES),
            )
            connection.execute(
                """UPDATE margaritas SET name='', rating=NULL, review='',
                   image_name=NULL, updated_by_id=NULL, updated_by_name=NULL,
                   updated_at=NULL"""
            )
        with audiobooks_module.connect(audiobooks_module.DB_PATH) as connection:
            connection.execute("DELETE FROM audiobook_progress")
            connection.execute("DELETE FROM audiobooks")
        with audiobook_streaming_module.queue_connection() as connection:
            connection.execute("DELETE FROM audiobook_playback_jobs")
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
        for path in files_module.OBJECTS.glob("*"):
            path.unlink()
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

    def add_photo(self, photo_id, deleted_at=None, owner_id=None, owner_name=None, visibility="shared"):
        with portal.db() as connection:
            connection.execute(
                "INSERT INTO photos (id, original_name, stored_path, preview_name, thumb_name, content_type, "
                "byte_size, sha256, taken_at, capture_timestamp, uploaded_at, uploaded_by, deleted_at, owner_id, owner_name, visibility) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (photo_id, f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.jpg", "image/jpeg", 10,
                 f"hash-{photo_id}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
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

    def memberships(self):
        with portal.db() as connection:
            return {tuple(row) for row in connection.execute(
                "SELECT collection_id, photo_id FROM collection_photos ORDER BY collection_id, photo_id"
            ).fetchall()}

    def test_pages_render(self):
        for path in ("/", "/photos", "/assistant", "/chat", "/games", "/status", "/files", "/device-backup", "/places", "/audiobooks"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
        self.assertIn(b"David-Pi", self.client.get("/assistant").data)
        icon = self.client.get("/david-pi-icon-192.png")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.mimetype, "image/png")
        self.assertGreater(len(icon.data), 1000)
        files_page = self.client.get("/files").data
        self.assertIn(b"files.js?v=12", files_page)
        self.assertIn(b"file-drop-plus", files_page)
        self.assertNotIn(b"<iframe", files_page)
        self.assertIn(b"pinchStartZoom", self.client.get("/static/files.js").data)
        audiobooks_page = self.client.get("/audiobooks").data
        self.assertIn(b"audiobooks.js?v=15", audiobooks_page)
        self.assertIn(b"hls.min.js?v=1.6.16", audiobooks_page)
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
        self.assertIn("const renderWidth=2000", files_script)

    def test_audiobook_upload_progress_range_and_privacy(self):
        with patch.object(audiobooks_module,"probe",return_value=({"title":"A Safe Book","artist":"An Author"},3600.0,[{"title":"Chapter 1","start":0,"end":120}])), patch.object(audiobooks_module,"cover",return_value=None):
            uploaded=self.client.post("/api/audiobooks/upload",data={"visibility":"private","books":(BytesIO(b"ID3"+b"a"*2048),"safe.mp3")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(uploaded.status_code,201,uploaded.get_data(as_text=True))
        mine=self.client.get("/api/audiobooks?owner=mine").get_json()["books"]
        self.assertEqual(mine[0]["title"],"A Safe Book"); self.assertEqual(mine[0]["chapters"][0]["title"],"Chapter 1")
        book_id=mine[0]["id"]
        self.assertEqual(self.client.put(f"/api/audiobooks/{book_id}/progress",json={"position_seconds":125.5},headers={"X-CSRF-Token":self.csrf}).status_code,200)
        self.assertEqual(self.client.get("/api/audiobooks?owner=mine").get_json()["books"][0]["position_seconds"],125.5)
        ranged=self.client.get(f"/api/audiobooks/{book_id}/stream",headers={"Range":"bytes=0-2"}); self.assertEqual(ranged.status_code,206); self.assertEqual(ranged.data,b"ID3")
        diana,_=self.paired_client("diana","Diana"); self.assertEqual(diana.get("/api/audiobooks").get_json()["books"],[])

    def test_audiobook_rejects_protected_aax(self):
        response=self.client.post("/api/audiobooks/upload",data={"visibility":"shared","books":(BytesIO(b"protected"),"book.aax")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(response.status_code,422); self.assertIn("DRM-free",response.get_data(as_text=True))

    def test_audiobook_segmented_playback_is_private_and_range_capable(self):
        with patch.object(audiobooks_module,"probe",return_value=({"title":"Segmented"},120.0,[])), patch.object(audiobooks_module,"cover",return_value=None):
            response=self.client.post("/api/audiobooks/upload",data={"visibility":"private","books":(BytesIO(b"ID3"+b"x"*4096),"segmented.mp3")},content_type="multipart/form-data",headers={"X-CSRF-Token":self.csrf})
        self.assertEqual(response.status_code,201)
        pending=self.client.get("/api/audiobooks?owner=mine").get_json()["books"][0]
        self.assertEqual(pending["playback_mode"],"range")
        self.assertEqual(pending["playback_state"],"pending")
        self.assertIsNone(pending["hls_url"])
        target=audiobook_streaming_module.STREAMING/pending["id"]/"v1"; target.mkdir(parents=True)
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
        self.assertIn("private",media.headers["Cache-Control"])
        diana,_=self.paired_client("diana","Diana")
        self.assertEqual(diana.get(ready["hls_url"]).status_code,404)

    def test_audiobook_derivative_validation_and_atomic_original_preservation(self):
        book_id="a"*32;source=audiobook_streaming_module.ORIGINALS/f"{book_id}.m4b";original=b"immutable-original"*1024;source.write_bytes(original)
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size)
        job=audiobook_streaming_module.claim_next_job()
        def fake_run(command,**_kwargs):
            if command[0]=="ffprobe":return SimpleNamespace(stdout='{"streams":[{"codec_name":"aac","channels":2}]}')
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

    def test_audiobook_worker_pauses_for_storage_temperature_or_load(self):
        with patch.object(audiobook_streaming_module.shutil,"disk_usage",return_value=SimpleNamespace(free=1)):
            self.assertEqual(audiobook_streaming_module.system_pause_reason(),"low_storage")
        with patch.object(audiobook_streaming_module.shutil,"disk_usage",return_value=SimpleNamespace(free=200*1024**3)), patch.object(audiobook_streaming_module.os,"getloadavg",return_value=(99,1,1)):
            self.assertEqual(audiobook_streaming_module.system_pause_reason(),"load_high")

    def test_audiobook_worker_restart_requeues_interrupted_job_and_clears_staging(self):
        book_id="b"*32
        source=audiobook_streaming_module.ORIGINALS/f"{book_id}.m4b"
        source.write_bytes(b"original")
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size)
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
        audiobook_streaming_module.enqueue(book_id,source.name,source.stat().st_size)
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

    def test_audiobook_player_prefers_hls_with_bounded_buffer_and_range_fallback(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/"static"/"audiobooks.js").read_text(encoding="utf-8")
        self.assertIn("audio.canPlayType('application/vnd.apple.mpegurl')",script)
        self.assertIn("window.Hls.isSupported()",script)
        self.assertIn("maxBufferLength:900",script)
        self.assertIn("maxBufferSize:134217728",script)
        self.assertIn("backBufferLength:120",script)
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

    def test_existing_videos_are_automatically_added_to_videos_collection(self):
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
            self.assertIsNotNone(collection)
            membership = connection.execute(
                "SELECT 1 FROM collection_photos WHERE collection_id=? AND photo_id=?",
                (collection["id"], "auto-video"),
            ).fetchone()
            self.assertIsNotNone(membership)

    def test_videos_collection_is_useful_in_added_by_me_without_leaking_owners(self):
        self.add_photo("david-private-video", owner_id="david@example.test", owner_name="David", visibility="private")
        self.add_photo("diana-private-video", owner_id="diana@example.test", owner_name="Diana", visibility="private")
        with portal.db() as connection:
            connection.execute("UPDATE photos SET content_type='video/mp4'")
        portal.initialize_once()
        listing = self.client.get("/api/collections?view=mine").get_json()["collections"]
        videos = next(item for item in listing if item["name"].casefold() == "videos")
        self.assertEqual(videos["photo_count"], 1)
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
        self.assertIn('addJavascriptInterface(it, "DavidPiMedia")', shell)
        self.assertIn("WebSettings.LOAD_NO_CACHE", shell)
        self.assertIn("mediaPlaybackRequiresUserGesture = false", shell)
        self.assertIn("clearCache(true)", shell)
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
        self.assertIn("audio.addEventListener('canplay',resume,{once:true})", audiobooks)
        self.assertIn("seekAudiobookChapter", audiobooks)

    def test_gallery_has_continuous_loading_drill_in_and_viewport_organizer(self):
        root = Path(__file__).resolve().parents[1]
        gallery = (root / "static" / "gallery.js").read_text(encoding="utf-8")
        self.assertIn("openTimelinePeriod", gallery)
        self.assertIn("window.addEventListener('scroll'", gallery)
        self.assertIn("document.body.append(organizeSheet)", gallery)
        self.assertIn("collectionChecks.style.setProperty('max-height'", gallery)
        self.assertIn("(viewport?.height || window.innerHeight) * 0.4", gallery)
        self.assertIn("captureGalleryPosition", gallery)

    def test_mobile_media_repairs_and_iphone_now_playing_contracts(self):
        root = Path(__file__).resolve().parents[1]
        gallery = (root / "static" / "gallery.js").read_text(encoding="utf-8")
        dialog_host = (root / "static" / "mobile-dialog-host.js").read_text(encoding="utf-8")
        audiobooks = (root / "static" / "audiobooks.js").read_text(encoding="utf-8")
        audiobook_page = (root / "templates" / "audiobooks.html").read_text(encoding="utf-8")
        self.assertIn("collection-card:not(.dynamic)", gallery)
        self.assertIn("api('/api/collections?view=mine')", gallery)
        self.assertIn("gallery-month-zoom", gallery)
        self.assertIn("setGalleryDensity('compact')", gallery)
        self.assertIn("data-gallery-density", gallery)
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
        self.assertNotIn("if (viewerImage.hidden) return;", gallery)
        self.assertIn("{passive:false, capture:true}", gallery)
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
            self.assertIn("immutable", response.headers["Cache-Control"])
            self.assertTrue(derivative.is_file())
            with portal.Image.open(derivative) as image:
                self.assertLessEqual(max(image.size), 1600)
            page = self.client.get("/photos").get_data(as_text=True)
            self.assertIn("app.css?v=31", page)
            self.assertIn("gallery.js?v=37", page)
            self.assertIn("mobile-dialog-host.js?v=8", page)
        finally:
            preview.unlink(missing_ok=True)
            derivative.unlink(missing_ok=True)

    def test_service_worker_never_caches_private_pages_or_media(self):
        root = Path(__file__).resolve().parents[1]
        worker = (root / "static" / "sw.js").read_text(encoding="utf-8")
        page = self.client.get("/photos").get_data(as_text=True)
        self.assertNotIn("const SHELL = ['/']", worker)
        self.assertIn("event.request.mode === 'navigate'", worker)
        self.assertIn("url.pathname.startsWith('/api/')", worker)
        self.assertIn("url.pathname.startsWith('/media/')", worker)
        self.assertIn('data-density="compact"', page)

    def test_home_links_to_games_without_redundant_add_media_card(self):
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn('href="/games"', html)
        self.assertNotIn('href="/photos?upload=1"', html)

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
        self.assertIn("/static/games.js?v=8", html)
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

    def test_status_summary_is_sanitized_and_history_is_allowlisted(self):
        status_path = Path(TEST_DATA.name) / "server-status.json"
        payload = {
            "schema_version": 1,
            "generated_at": "2026-07-25T12:00:00+00:00",
            "state": "healthy",
            "subsystems": {
                "portal": {
                    "state": "healthy", "summary": "Healthy", "updated_at": "2026-07-25T12:00:00+00:00",
                    "details": {}, "recommended_action": "", "evidence_code": "PORTAL_HEALTHY",
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
        self.assertEqual(self.client.get("/api/status/history?metric=secret&range=24h").status_code, 400)
        self.assertEqual(self.client.get("/api/status/history?metric=temperature&range=forever").status_code, 400)

    def test_status_page_has_ten_health_cards_and_mobile_controls(self):
        html = self.client.get("/status").get_data(as_text=True)
        self.assertIn('id="healthCards"', html)
        self.assertIn('id="cpuMetric"', html)
        self.assertIn("Everything at a glance.", html)
        self.assertIn('id="historyMetric"', html)
        self.assertIn('id="historyRange"', html)
        self.assertIn("/static/status.js?v=6", html)
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
            "David-Pi Rules", "Windows Codex", "Ubuntu Local AI",
        ])
        self.assertFalse(providers[1]["available"])

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

    def test_assistant_routes_only_unsupported_questions_to_windows(self):
        with patch(
            "modules.assistant.WindowsCodexProvider.submit",
            return_value={"answer": "Twenty-five.", "model": "test"},
        ) as submit:
            remote = self.client.post(
                "/api/assistant", json={"question": "What is five times five?"}
            ).get_json()
            self.assertEqual(remote["intent"], "windows_general")
            self.assertEqual(remote["provider"], "Windows Codex")
            submit.assert_called_once()

            deterministic = self.client.post(
                "/api/assistant", json={"question": "How many photos do we have?"}
            ).get_json()
            self.assertEqual(deterministic["intent"], "photos")
            submit.assert_called_once()

    def test_assistant_has_no_pi_generative_provider(self):
        providers = self.client.get("/api/assistant/providers").get_json()["providers"]
        names = {provider["name"] for provider in providers}
        self.assertIn("David-Pi Rules", names)
        self.assertNotIn("Local Pi", names)

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
            200,
        )

    def test_active_media_cannot_be_permanently_purged(self):
        self.add_photo("active")
        response = self.client.post("/api/photos/purge", json={"ids": ["active"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 0)
        with portal.db() as connection:
            self.assertIsNotNone(
                connection.execute("SELECT 1 FROM photos WHERE id='active'").fetchone()
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
        self.add_photo("p1")
        self.add_photo("p2")
        self.add_collection("c1", "Mixed")
        self.add_collection("c2", "Empty")
        with portal.db() as connection:
            connection.execute("INSERT INTO collection_photos VALUES (?, ?, ?)", ("c1", "p1", "2026-01-01"))
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
        with portal.db() as connection:
            connection.execute(
                "INSERT INTO collection_photos VALUES (?, ?, ?)",
                ("private-collection", "shared-photo", "2026-01-01"),
            )
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
        self.add_photo("shared-photo")
        self.add_collection("shared-collection", "Shared")
        with portal.db() as connection:
            connection.execute(
                "INSERT INTO collection_photos VALUES (?, ?, ?)",
                ("shared-collection", "shared-photo", "2026-01-01"),
            )
        response = self.client.patch(
            "/api/collections/shared-collection",
            json={"name": "Private collection", "visibility": "private"},
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

    def test_membership_changes_apply_together(self):
        self.add_photo("p1")
        self.add_photo("p2")
        self.add_collection("c1", "Mixed")
        self.add_collection("c2", "Full")
        with portal.db() as connection:
            connection.executemany(
                "INSERT INTO collection_photos VALUES (?, ?, ?)",
                [("c1", "p1", "2026-01-01"), ("c2", "p1", "2026-01-01"), ("c2", "p2", "2026-01-01")],
            )
        response = self.client.post("/api/collections/membership", json={
            "ids": ["p1", "p2"],
            "changes": [{"collection_id": "c1", "action": "add"}, {"collection_id": "c2", "action": "remove"}],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["added"], 1)
        self.assertEqual(response.get_json()["removed"], 2)
        self.assertEqual(self.memberships(), {("c1", "p1"), ("c1", "p2")})

    def test_invalid_membership_batch_rolls_back(self):
        self.add_photo("p1")
        self.add_collection("c1", "Keep")
        before = self.memberships()
        response = self.client.post("/api/collections/membership", json={
            "ids": ["p1"],
            "changes": [{"collection_id": "c1", "action": "add"}, {"collection_id": "missing", "action": "add"}],
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.memberships(), before)

    def test_restore_all_only_restores_deleted_photos(self):
        self.add_photo("active")
        self.add_photo("deleted-1", "2026-01-02T00:00:00+00:00")
        self.add_photo("deleted-2", "2026-01-03T00:00:00+00:00")
        response = self.client.post("/api/photos/restore-all")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["count"], 2)
        with portal.db() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM photos WHERE deleted_at IS NOT NULL"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 3)

    def test_purge_all_requires_explicit_confirmation_and_only_purges_deleted(self):
        self.add_photo("active")
        self.add_photo("deleted", "2026-01-02T00:00:00+00:00")
        denied = self.client.post("/api/photos/purge-all", json={})
        self.assertEqual(denied.status_code, 400)
        with portal.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)

        allowed = self.client.post(
            "/api/photos/purge-all", json={"confirmation": "empty-recently-deleted"}
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["count"], 1)
        with portal.db() as connection:
            rows = connection.execute("SELECT id FROM photos").fetchall()
        self.assertEqual([row[0] for row in rows], ["active"])

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
        self.add_photo("p1")
        self.add_photo("p2")
        self.add_collection("c1", "Vacation")
        with portal.db() as connection:
            connection.executemany(
                "INSERT INTO collection_photos VALUES (?, ?, ?)",
                [("c1", "p1", "2026-01-01"), ("c1", "p2", "2026-01-01")],
            )
        options = self.client.get("/api/slideshows/options").get_json()["collections"]
        self.assertEqual(options[0]["image_count"], 2)
        with patch.object(threading.Thread, "start"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "c1", "duration_seconds": 20,
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
        self.assertEqual(parameters["media_ids"], ["p1", "p2"])
        self.assertEqual(parameters["transition"], "mixed")
        self.assertEqual(parameters["layout"], "fill")
        self.assertEqual(parameters["music_id"], "jrpg-piano")
        self.assertTrue(parameters["loop_playback"])

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
        self.add_photo("p1")
        self.add_collection("c1", "Memories")
        with portal.db() as connection:
            connection.execute("INSERT INTO collection_photos VALUES (?, ?, ?)", ("c1", "p1", "2026-01-01"))
        with patch.object(threading.Thread, "start"):
            response = self.client.post("/api/slideshows", json={
                "collection_id": "c1", "duration_seconds": 1200,
                "transition": "fade", "layout": "fit",
            })
        self.assertEqual(response.status_code, 202)
        options = self.client.get("/api/slideshows/options").get_json()
        self.assertGreaterEqual(len(options["music"]), 4)
        preview = self.client.get(options["music"][0]["preview_url"])
        self.assertEqual(preview.status_code, 200)

    def test_video_upload_preserves_original_and_returns_playback_metadata(self):
        def fake_prepare(path, photo_id, extension):
            (portal.PREVIEWS / f"{photo_id}.jpg").write_bytes(b"poster")
            (portal.THUMBS / f"{photo_id}.jpg").write_bytes(b"thumb")
            (portal.PREVIEWS / f"{photo_id}.mp4").write_bytes(b"playback")
            return f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.mp4"

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
        diana, _ = self.paired_client("diana")
        self.assertEqual(diana.get(f"/api/notes/{private['id']}").status_code, 404)
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
        self.assertIn("/static/notes.js?v=8", notes_page)

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
        self.assertEqual(
            david.post(f"/api/notes/{note['id']}/state", json={"action": "trash"}, headers={"X-CSRF-Token": csrf}).status_code,
            200,
        )
        self.assertEqual(len(david.get("/api/notes?view=deleted").get_json()["notes"]), 1)
        self.assertEqual(
            david.post(f"/api/notes/{note['id']}/state", json={"action": "restore"}, headers={"X-CSRF-Token": csrf}).status_code,
            200,
        )
        self.assertEqual(david.delete(f"/api/notes/{note['id']}", headers={"X-CSRF-Token": csrf}).status_code, 400)

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
        self.assertIn('/static/movies.js?v=6', html)

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

    def test_recipe_manual_creation_recommendation_and_ssrf_guard(self):
        david, csrf = self.paired_client()
        recipe = david.post(
            "/api/recipes",
            json={"title": "Tomato Soup", "meal_type": "main", "total_minutes": 25, "ingredients": ["Tomatoes", "Fresh basil"], "instructions": ["Simmer"], "tags": ["Quick"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(recipe.status_code, 201)
        recommendations = david.get("/api/recipes/recommend?meal=main&quick=1").get_json()["recipes"]
        self.assertEqual(recommendations[0]["title"], "Tomato Soup")
        multi_ingredient = david.get("/api/recipes/recommend?meal=main&ingredient=tomatoes,basil").get_json()["recipes"]
        self.assertEqual(multi_ingredient[0]["title"], "Tomato Soup")
        with self.assertRaises(ValueError):
            recipes_module.public_url("http://127.0.0.1/private")
        with self.assertRaises(ValueError):
            recipes_module.public_url("file:///etc/passwd")
        duplicate = david.post(
            "/api/recipes",
            json={"title": "Tomato Soup", "meal_type": "main", "ingredients": ["Tomatoes", "Fresh basil"], "instructions": ["Simmer"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_recipe_sections_are_simple_and_legacy_clients_remain_compatible(self):
        self.assertEqual(recipes_module.classify_legacy_recipe("Banana Pancakes", ["Dessert"]), "breakfast")
        self.assertEqual(recipes_module.classify_legacy_recipe("Apple Pie", ["Dessert"]), "dessert")
        self.assertEqual(recipes_module.classify_legacy_recipe("Tomato Soup", ["Starter"]), "main")
        self.assertEqual(recipes_module.normalize_recipe_section("lunch"), "main")
        self.assertEqual(recipes_module.normalize_recipe_section("dinner"), "main")

        david, csrf = self.paired_client()
        created = david.post(
            "/api/recipes",
            json={"title": "Legacy Dinner Client", "meal_type": "dinner", "ingredients": ["Rice"], "instructions": ["Cook"]},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["recipe"]["meal_type"], "main")
        self.assertEqual(len(david.get("/api/recipes?meal=main").get_json()["recipes"]), 1)
        self.assertEqual(len(david.get("/api/recipes?meal=dinner").get_json()["recipes"]), 1)

        page = david.get("/recipes")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Breakfast", page.data)
        self.assertIn(b"Lunch &amp; dinner", page.data)
        self.assertIn(b"Desserts", page.data)
        self.assertIn(b"recipes.js?v=4", page.data)

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

    def test_recipe_image_proxy_returns_image_data(self):
        david, csrf = self.paired_client()
        recipe = david.post(
            "/api/recipes",
            json={
                "title": "Picture Recipe", "meal_type": "main",
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
            self.assertEqual(david.get("/api/recipes").get_json()["recipes"], [])
            imported = david.post(
                "/api/recipes/import-mealdb",
                json={"mealdb_id": "52772", "meal_type": "main"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(imported.status_code, 201)
            self.assertEqual(imported.get_json()["recipe"]["ingredients"], ["500 g chicken"])
            duplicate = david.post(
                "/api/recipes/import-mealdb",
                json={"mealdb_id": "52772", "meal_type": "main"},
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(duplicate.status_code, 409)

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
        file_id = listing["files"][0]["id"]
        self.assertEqual(client.get(f"/api/files/{file_id}/text").get_json()["text"], "hello David-Pi")
        self.assertEqual(client.get(f"/api/files/{file_id}/content").data, b"hello David-Pi")
        self.assertEqual(
            client.delete(f"/api/files/{file_id}", headers={"X-CSRF-Token": csrf}).status_code, 200
        )
        self.assertEqual(len(client.get("/api/files?view=deleted").get_json()["files"]), 1)
        self.assertEqual(
            client.post(f"/api/files/{file_id}/restore", headers={"X-CSRF-Token": csrf}).status_code, 200
        )
        client.delete(f"/api/files/{file_id}", headers={"X-CSRF-Token": csrf})
        denied = client.post(
            f"/api/files/{file_id}/purge", json={"confirm": "yes"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(denied.status_code, 400)
        purged = client.post(
            f"/api/files/{file_id}/purge", json={"confirm": "permanently delete"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(purged.status_code, 200)
        self.assertEqual(list(files_module.OBJECTS.glob("*")), [])

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
        diana, _ = self.paired_client("diana")
        self.assertEqual(diana.get("/api/files").get_json()["files"], [])
        self.assertEqual(diana.get(f"/api/files/{file_id}/content").status_code, 404)
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

    def test_new_tailscale_member_gets_an_automatic_personal_view(self):
        guest, csrf = self.paired_client("guest", name="Alex")
        guest.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = "alex@example.test"
        created = guest.post(
            "/api/notes",
            json={"visibility": "private"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(created.status_code, 201)
        note = created.get_json()["note"]
        self.assertEqual(note["owner_display"], "Alex")
        self.assertEqual(guest.get("/api/notes").get_json()["notes"], [])
        self.assertEqual(
            [item["id"] for item in guest.get("/api/notes?view=mine").get_json()["notes"]],
            [note["id"]],
        )
        david, _ = self.paired_client("david")
        self.assertEqual(david.get(f"/api/notes/{note['id']}").status_code, 404)

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

    def test_ios_shortcut_upload_uses_canonical_owner_and_checkpoint(self):
        paired = self.pair_backup_device(endpoint="/api/v1/ios-backup/pair")
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        image = BytesIO()
        portal.Image.new("RGB", (10, 8), "coral").save(image, "PNG")
        image.seek(0)
        uploaded = self.client.post(
            "/api/v1/ios-backup/upload",
            data={
                "file": (image, "iphone-photo.png"),
                "client_item_id": "ios-asset-1",
                "capture_timestamp": "2026-07-30T12:00:00+00:00",
            },
            headers=auth,
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        media_id = uploaded.get_json()["media_id"]
        with portal.db() as connection:
            photo = connection.execute(
                "SELECT owner_id,ingestion_source,source_device_id FROM photos WHERE id=?",
                (media_id,),
            ).fetchone()
        self.assertEqual(photo["owner_id"], "david@example.test")
        self.assertEqual(photo["ingestion_source"], "ios_shortcut_backup")
        self.assertEqual(photo["source_device_id"], paired["device_id"])

        checkpoint = self.client.post(
            "/api/v1/ios-backup/checkpoint",
            json={"capture_cursor": "2026-07-30T12:00:00+00:00"}, headers=auth,
        )
        self.assertEqual(checkpoint.status_code, 200)
        status = self.client.get("/api/v1/ios-backup/status", headers=auth).get_json()
        self.assertEqual(status["incremental_cursor"], "2026-07-30T12:00:00+00:00")

    def test_ios_raw_file_upload_is_resumable_and_self_checkpoints(self):
        paired = self.pair_backup_device(endpoint="/api/v1/ios-backup/pair")
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        initial = self.client.get("/api/v1/ios-backup/status", headers=auth)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.get_json()["capture_after"], "1970-01-01T00:00:00+00:00")
        self.assertEqual(initial.get_json()["recommended_batch_size"], 200)

        image = BytesIO()
        portal.Image.new("RGB", (11, 9), "peachpuff").save(image, "PNG")
        body = image.getvalue()
        raw_headers = {
            **auth,
            "X-David-Pi-Filename": "IMG_0123.png",
            "X-David-Pi-Captured-At": "2026-07-31T14:15:16Z",
            "X-David-Pi-Item-Id": "ios-library-item-123",
        }
        uploaded = self.client.post(
            "/api/v1/ios-backup/upload-file", data=body,
            headers=raw_headers, content_type="image/png",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        self.assertEqual(
            uploaded.get_json()["capture_cursor"], "2026-07-31T14:15:16+00:00"
        )

        status = self.client.get("/api/v1/ios-backup/status", headers=auth).get_json()
        self.assertEqual(status["incremental_cursor"], "2026-07-31T14:15:16+00:00")
        retried = self.client.post(
            "/api/v1/ios-backup/upload-file", data=body,
            headers=raw_headers, content_type="image/png",
        )
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.get_json()["state"], "duplicate")

    def test_ios_credential_download_pairs_current_tailscale_owner(self):
        response = self.client.post("/api/ios-backup/credential-file")
        self.assertEqual(response.status_code, 200)
        credential = response.get_data(as_text=True).strip()
        self.assertGreaterEqual(len(credential), 48)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("DavidPiBackupCredential.txt", response.headers["Content-Disposition"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        with portal.db() as connection:
            row = connection.execute(
                """SELECT owner_user_id,platform,credential_hash FROM backup_devices
                   WHERE id=?""",
                (response.headers["X-David-Pi-Device-Id"],),
            ).fetchone()
        self.assertEqual(row["owner_user_id"], "david@example.test")
        self.assertEqual(row["platform"], "ios_shortcut")
        self.assertEqual(row["credential_hash"], device_backup_module.token_hash(credential))

    def test_unsigned_ios_shortcut_download_is_retired_without_creating_device(self):
        with portal.db() as connection:
            before = connection.execute("SELECT COUNT(*) FROM backup_devices").fetchone()[0]
        response = self.client.post("/api/ios-backup/shortcut")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.get_json()["error"]["code"], "unsigned_shortcut_retired")
        with portal.db() as connection:
            after = connection.execute("SELECT COUNT(*) FROM backup_devices").fetchone()[0]
        self.assertEqual(after, before)

    def test_iphone_setup_accepts_only_apple_icloud_shortcut_links(self):
        good = "https://www.icloud.com/shortcuts/AbCdEf012345"
        with patch.dict(os.environ, {"IOS_SHORTCUT_ICLOUD_URL": good}):
            page = self.client.get("/device-backup")
        self.assertEqual(page.status_code, 200)
        self.assertIn(good.encode(), page.data)

        with patch.dict(os.environ, {"IOS_SHORTCUT_ICLOUD_URL": "https://evil.test/shortcuts/nope"}):
            rejected = self.client.get("/device-backup")
        self.assertEqual(rejected.status_code, 200)
        self.assertNotIn(b"evil.test", rejected.data)
        self.assertIn(b"Apple share link pending", rejected.data)

    def test_ios_raw_file_upload_rejects_client_ownership(self):
        paired = self.pair_backup_device(endpoint="/api/v1/ios-backup/pair")
        response = self.client.post(
            "/api/v1/ios-backup/upload-file", data=b"not parsed",
            headers={
                "Authorization": f"Bearer {paired['device_credential']}",
                "X-David-Pi-Owner-Id": "someone-else",
            },
            content_type="application/octet-stream",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["code"], "server_controls_ownership")

    def test_ios_shortcut_cannot_claim_owner_and_deduplicates_retry(self):
        paired = self.pair_backup_device(endpoint="/api/v1/ios-backup/pair")
        auth = {"Authorization": f"Bearer {paired['device_credential']}"}
        rejected = self.client.post(
            "/api/v1/ios-backup/upload",
            data={"owner_user_id": "diana@example.test", "file": (BytesIO(b"x"), "x.jpg")},
            headers=auth, content_type="multipart/form-data",
        )
        self.assertEqual(rejected.status_code, 422)

        def image_file():
            stream = BytesIO()
            portal.Image.new("RGB", (7, 7), "green").save(stream, "PNG")
            stream.seek(0)
            return stream

        first = self.client.post(
            "/api/v1/ios-backup/upload",
            data={"client_item_id": "stable-asset", "file": (image_file(), "same.png")},
            headers=auth, content_type="multipart/form-data",
        )
        second = self.client.post(
            "/api/v1/ios-backup/upload",
            data={"client_item_id": "stable-asset", "file": (image_file(), "same.png")},
            headers=auth, content_type="multipart/form-data",
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["state"], "duplicate")

    def test_ios_shortcut_authenticates_before_parsing_upload_or_checkpoint(self):
        upload = self.client.post("/api/v1/ios-backup/upload", data={})
        self.assertEqual(upload.status_code, 401)
        self.assertEqual(upload.get_json()["error"]["code"], "device_unauthorized")
        checkpoint = self.client.post("/api/v1/ios-backup/checkpoint", json={})
        self.assertEqual(checkpoint.status_code, 401)
        self.assertEqual(checkpoint.get_json()["error"]["code"], "device_unauthorized")

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
            (portal.PREVIEWS / f"{photo_id}.jpg").write_bytes(b"poster")
            (portal.THUMBS / f"{photo_id}.jpg").write_bytes(b"thumb")
            (portal.PREVIEWS / f"{photo_id}.mp4").write_bytes(b"playable")
            return f"{photo_id}.jpg", f"{photo_id}.jpg", f"{photo_id}.mp4"

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

        def fail_after_poster(path, photo_id, extension):
            (portal.PREVIEWS / f"{photo_id}.jpg").write_bytes(b"partial")
            raise ValueError("Conversion failed!")

        with patch.object(portal, "prepare_video", side_effect=fail_after_poster):
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
            json={"rating": 4.5, "review": "Would go again.", "visited_at": "2026-07-29"},
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
            json={"rating": 4.2, "visited_at": "2026-07-29"},
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
        self.client.delete(f"/api/places/restaurants/{added['id']}")
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
        removed = self.client.put(
            f"/api/places/restaurants/{restaurant['id']}",
            data={
                "name": "Photo Cafe",
                "cuisines": "Cafe",
                "notes": "Window seat.",
                "remove_photo_ids": restaurant["photos"][0]["id"],
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(removed.status_code, 200)
        updated = removed.get_json()["restaurant"]
        self.assertTrue(updated["has_image"])
        self.assertEqual(len(updated["photos"]), 1)
        self.assertEqual(self.client.get(removed_url).status_code, 404)
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
            diana.patch(f"/api/chat/messages/{message_id}", json={"body": "changed"}, headers={"X-CSRF-Token": diana_csrf}).status_code,
            404,
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
        self.assertEqual(outsider.get(attachment["preview_url"]).status_code, 404)
        saved = self.client.post(
            f"/api/chat/attachments/{attachment['id']}/save-to-media",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(saved.status_code, 200)
        with portal.db() as connection:
            media = connection.execute("SELECT owner_id,visibility,ingestion_source FROM photos WHERE id=?", (saved.get_json()["media_id"],)).fetchone()
        self.assertEqual(tuple(media), ("david@example.test", "private", "chat_save"))
        self.assertEqual(
            outsider.post(f"/api/chat/attachments/{attachment['id']}/save-to-media", headers={"X-CSRF-Token": outsider_csrf}).status_code,
            404,
        )

    def test_chat_delete_removes_encrypted_attachment_objects(self):
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
            data={"client_message_id": "delete-photo", "attachments": (image, "private.png")},
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
            f"/api/chat/messages/{sent['id']}", headers={"X-CSRF-Token": self.csrf}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(diana.get(attachment["preview_url"]).status_code, 404)

    def test_chat_conversation_delete_removes_shared_history_and_encrypted_objects(self):
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
            json={"confirmation": "DELETE CHAT", "conversation_id": conversation_id},
            headers={"X-CSRF-Token": outsider_csrf},
        ).status_code, 404)

        refused = diana.delete(
            f"/api/chat/conversations/{conversation_id}", headers={"X-CSRF-Token": diana_csrf}
        )
        self.assertEqual(refused.status_code, 422)
        self.assertTrue(all(path.is_file() for path in paths))
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone())

        deleted = diana.delete(
            f"/api/chat/conversations/{conversation_id}",
            json={"confirmation": "DELETE CHAT", "conversation_id": conversation_id},
            headers={"X-CSRF-Token": diana_csrf},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.get_json()["cleanup_pending"], 0)
        self.assertTrue(all(not path.exists() for path in paths))
        with chat_module.connect(chat_module.DB_PATH) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone())
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE conversation_id=?", (conversation_id,)
            ).fetchone()[0], 0)

    def test_chat_push_endpoint_is_reassigned_not_shared_between_users(self):
        diana, diana_csrf = self.paired_client("diana")
        self.client.get("/chat")
        diana.get("/chat")
        token = "a" * 64
        endpoint = "https://push.example.test/private-device"
        self.assertEqual(self.client.post(
            "/api/chat/push/android", json={"token": token}, headers={"X-CSRF-Token": self.csrf}
        ).status_code, 200)
        self.assertEqual(self.client.post(
            "/api/chat/push/web", json={"endpoint": endpoint, "keys": {"p256dh": "key", "auth": "auth"}},
            headers={"X-CSRF-Token": self.csrf},
        ).status_code, 200)
        self.assertEqual(diana.post(
            "/api/chat/push/android", json={"token": token}, headers={"X-CSRF-Token": diana_csrf}
        ).status_code, 200)
        self.assertEqual(diana.post(
            "/api/chat/push/web", json={"endpoint": endpoint, "keys": {"p256dh": "new-key", "auth": "new-auth"}},
            headers={"X-CSRF-Token": diana_csrf},
        ).status_code, 200)
        with chat_module.connect(chat_module.DB_PATH) as connection:
            owners = connection.execute(
                "SELECT platform,owner_id FROM push_subscriptions ORDER BY platform"
            ).fetchall()
        self.assertEqual([(row["platform"], row["owner_id"]) for row in owners], [
            ("android", "diana@example.test"), ("web", "diana@example.test"),
        ])

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
        self.assertIn('"New David-Pi message"', worker_source)
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
        self.assertNotIn("color:transparent", css)
        self.assertIn("min-width: 92px", css)
        self.assertIn("$('closeNewChat').onclick = closeNewChat", javascript)
        self.assertIn("$('cancelNewChat').onclick = closeNewChat", javascript)
        self.assertIn("Choose at least one person, or tap Cancel to leave.", javascript)
        self.assertNotIn("alert('Notifications are blocked", javascript)
        self.assertIn("openNotificationSettings", javascript)
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
        self.assertIn("confirmation: 'DELETE CHAT'", javascript)
        self.assertIn("Leaving Chat never performs this action.", html)
        self.assertIn("Notification.permission !== 'granted'", javascript)
        self.assertIn("registration.pushManager.getSubscription()", javascript)
        self.assertIn('DAVID_PI_VAPID_SUBJECT', worker_source)
        self.assertIn('https://localhost', worker_source)
        self.assertNotIn('notifications@david-pi.local', worker_source)
        self.assertIn("else if (conversations.length === 1)", javascript)
        self.assertIn("await openThread(conversations[0].id)", javascript)
        self.assertIn("async function refreshChatLifecycle()", javascript)
        self.assertIn("window.addEventListener('pageshow'", javascript)
        self.assertIn("refreshChatLifecycle().catch", javascript)
        self.assertIn("grid-auto-rows: minmax(72px, auto)", css)
        self.assertIn("fun notificationsEnabled(): Boolean", android)
        self.assertIn("Settings.ACTION_APP_NOTIFICATION_SETTINGS", android)
        self.assertIn(".imePadding()", android)
        self.assertNotIn("webView.destroy()", android)
        self.assertNotIn("navigator.serviceWorker.getRegistrations().then", android)
        self.assertIn("settings.cacheMode = WebSettings.LOAD_NO_CACHE", android)
        self.assertIn("clearCache(true)", android)
        self.assertIn("var navigationRequest by remember { mutableIntStateOf(0) }", android)
        self.assertIn("navigationRequest += 1", android)
        self.assertIn("appliedNavigationRequest != navigationRequest", android)
        self.assertNotIn("finally{location.reload();}", android)

    def test_margarita_month_safely_reencodes_image(self):
        from PIL import Image
        image = BytesIO()
        Image.new("RGBA", (24, 16), (255, 0, 0, 120)).save(image, "PNG")
        image.seek(0)
        response = self.client.put(
            "/api/places/margaritas/1",
            data={
                "name": "January Marg",
                "rating": "3.5",
                "review": "Tart.",
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


if __name__ == "__main__":
    unittest.main()
