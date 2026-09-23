import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_authoritative_media_link_privacy_unlink_and_delete_gate():
    script = r'''
import hashlib
from pathlib import Path
import app

app.app.config["TESTING"] = True
media_id = "a" * 32
payload = b"owned-video-bytes"
stored_name = f"{media_id}.mp4"
(app.ORIGINALS / stored_name).write_bytes(payload)
digest = hashlib.sha256(payload).hexdigest()
with app.db() as connection:
    connection.execute(
        """INSERT INTO photos
        (id,original_name,stored_path,preview_name,thumb_name,content_type,byte_size,sha256,
         content_sha256,taken_at,capture_timestamp,uploaded_at,uploaded_by,owner_id,owner_name,visibility)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (media_id,"Family.mp4",stored_name,"preview.jpg","thumb.jpg","video/mp4",len(payload),digest,
         digest,"2026-01-01","2026-01-01","2026-01-01","David","david@example.test","David","shared"),
    )

def client(login, name):
    result = app.app.test_client()
    result.environ_base["HTTP_X_TEST_TAILSCALE_LOGIN"] = login
    result.environ_base["HTTP_X_TEST_TAILSCALE_NAME"] = name
    token = f"mytube-link-{name.lower()}-csrf-token-long-enough"
    result.set_cookie("david_pi_csrf", token, domain="localhost")
    result.environ_base["HTTP_X_CSRF_TOKEN"] = token
    return result

david = client("david@example.test", "David")
diana = client("diana@example.test", "Diana")
created = david.post("/api/mytube/media-links", json={"media_id": media_id})
assert created.status_code == 201, created.get_data(as_text=True)
assert david.get(f"/api/mytube/media-links/{media_id}").get_json()["linked"] is True
assert len(diana.get("/api/mytube/videos").get_json()["videos"]) == 1
from modules import mytube
with mytube.connect(mytube.DB_PATH) as connection:
    connection.execute("DELETE FROM mytube_videos WHERE media_id=?", (media_id,))
assert len(diana.get("/api/mytube/videos").get_json()["videos"]) == 1

privacy = david.patch("/api/photos/visibility", json={"items":[{"id":media_id,"version":1}],"visibility":"private"})
assert privacy.status_code == 200, privacy.get_data(as_text=True)
assert diana.get("/api/mytube/videos").get_json()["videos"] == []

blocked = david.post("/api/photos/trash", json={"items":[{"id":media_id,"version":2}]})
assert blocked.status_code == 409
removed = david.delete(f"/api/mytube/media-links/{media_id}")
assert removed.status_code == 200
with app.db() as connection:
    link_actions = [
        row[0] for row in connection.execute(
            "SELECT action FROM mutation_audit WHERE domain='mytube_media_link' ORDER BY id"
        )
    ]
assert link_actions == ["create", "delete"]
trashed = david.post("/api/photos/trash", json={"items":[{"id":media_id,"version":2}]})
assert trashed.status_code == 200, trashed.get_data(as_text=True)
assert (app.ORIGINALS / stored_name).read_bytes() == payload
print("MYTUBE_MEDIA_LINK_OK")
'''
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = os.environ.copy()
        environment.update({
            "PHOTO_DATA": str(root),
            "DAVID_PI_PLATFORM_DATA": str(root / "platform"),
            "DAVID_PI_MYTUBE_DATA": str(root / "mytube"),
            "DAVID_PI_FILES_DATA": str(root / "files"),
            "DAVID_PI_CHAT_DATA": str(root / "chat"),
            "DAVID_PI_AUDIOBOOKS_DATA": str(root / "audiobooks"),
            "DAVID_PI_DISABLE_METRICS": "1",
            "DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING": "1",
            "DAVID_PI_CHAT_KEY_B64": base64.b64encode(b"mytube-media-link-test-key-32bytes").decode(),
        })
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=environment,
            check=True, capture_output=True, text=True, timeout=60,
        )
    assert "MYTUBE_MEDIA_LINK_OK" in result.stdout
