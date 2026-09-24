from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from modules import mytube
from modules import mytube_prepare_worker as worker
from modules.mytube import MediaReference
from modules.platform import connect, migrate


DAVID = {"X-Test-Tailscale-Login": "david@example.test", "X-Test-Tailscale-Name": "David"}
DIANA = {"X-Test-Tailscale-Login": "diana@example.test", "X-Test-Tailscale-Name": "Diana"}
UNKNOWN = {"X-Test-Tailscale-Login": "mallory@example.test", "X-Test-Tailscale-Name": "Mallory"}


@pytest.fixture()
def mytube_app(tmp_path, monkeypatch):
    root = tmp_path / "mytube"
    values = {
        "DB_PATH": tmp_path / "platform" / "mytube.db",
        "ROOT": root,
        "ORIGINALS": root / "originals",
        "INCOMING": root / "incoming",
        "POSTERS": root / "posters",
        "STREAMING": root / "streaming",
        "MIN_FREE_BYTES": 0,
        "MAX_CHUNK_BYTES": 8,
        "MAX_UPLOAD_BYTES": 1024,
    }
    for name, value in values.items():
        monkeypatch.setattr(mytube, name, value)
    for name in ("DB_PATH", "POSTERS", "STREAMING"):
        monkeypatch.setattr(worker, name, values[name])
    monkeypatch.setattr(worker, "LOCK_PATH", tmp_path / "platform" / "mytube-worker.lock")
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parents[1] / "templates"),
        static_folder=str(Path(__file__).parents[1] / "static"),
    )
    app.testing = True
    app.secret_key = "mytube-test"
    app.config["MYTUBE_MEDIA_AUTHORIZER"] = lambda _media_id, _actor: True
    from modules.portal_configuration import init_portal_configuration
    init_portal_configuration(app)
    mytube.init_mytube(app)
    return app


def reserve(client, *, headers=DAVID, key="upload-key-1", visibility="shared", size=8, sha256=None):
    payload = {"filename": "family-video.mp4", "size": size, "title": "Family video", "visibility": visibility}
    payload["sha256"] = sha256 or hashlib.sha256(b"video123").hexdigest()
    return client.post("/api/mytube/uploads", json=payload, headers={**headers, "Idempotency-Key": key})


def insert_video(*, owner="david@example.test", visibility="shared", content=b"0123456789", video_id="a" * 32):
    stored_name = f"{video_id}.mp4"
    (mytube.ORIGINALS / stored_name).write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with connect(mytube.DB_PATH) as connection:
        connection.execute(
            """INSERT INTO mytube_videos(id,title,source_kind,stored_name,content_type,byte_size,sha256,
            duration_seconds,owner_id,owner_name,visibility,created_at,updated_at)
            VALUES(?,?,'upload',?,'video/mp4',?,?,10,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
            (video_id, "Test video", stored_name, len(content), digest, owner, owner.split("@", 1)[0], visibility),
        )
    return video_id, digest


def test_schema_is_additive_and_separate(mytube_app):
    assert mytube.DB_PATH.name == "mytube.db"
    with sqlite3.connect(mytube.DB_PATH) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "mytube_videos", "mytube_uploads", "mytube_progress", "mytube_prepare_jobs",
        "mytube_collections", "mytube_collection_members",
    }.issubset(tables)


def test_unapproved_identity_is_denied(mytube_app):
    response = mytube_app.test_client().get("/api/mytube/videos", headers=UNKNOWN)
    assert response.status_code == 403


def test_professional_pages_are_private_responsive_and_search_free(mytube_app):
    client = mytube_app.test_client()
    library = client.get("/mytube", headers=DAVID)
    assert library.status_code == 200
    assert b"viewport-fit=auto" in library.data
    assert b"viewport-fit=cover" not in library.data
    assert b'type="search"' not in library.data
    video_id, _digest = insert_video()
    watch = client.get(f"/mytube/watch/{video_id}", headers=DAVID)
    assert watch.status_code == 200
    assert b"playsinline" in watch.data and b"Up next" in watch.data
    assert client.get("/mytube", headers=UNKNOWN).status_code == 403


def test_compose_worker_is_profile_gated_and_storage_isolated():
    root = Path(__file__).parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    service = compose.split("  mytube-preparer:\n", 1)[1].split(
        "  david-pi-maintenance:\n", 1
    )[0]
    assert 'profiles: ["mytube-prepare"]' in service
    assert "DAVID_PI_WORKER_MODE: mytube" in service
    assert "source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}/photos.db" in service
    assert "target: /data/photos.db" in service
    assert "source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}/.david-pi-storage" in service
    assert "source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}\n" not in service
    entrypoint = (root / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert 'DAVID_PI_WORKER_MODE:-}" = "mytube"' in entrypoint
    assert "mount_is_read_only /data/photos.db" in entrypoint


def test_reservation_is_idempotent_and_bound_to_metadata(mytube_app):
    client = mytube_app.test_client()
    first = reserve(client)
    assert first.status_code == 201
    replay = reserve(client)
    assert replay.status_code == 200
    assert replay.json["upload_id"] == first.json["upload_id"]
    conflict = reserve(client, visibility="private")
    assert conflict.status_code == 409


def test_reservation_requires_content_hash_and_direct_play_container(mytube_app):
    client = mytube_app.test_client()
    missing_hash = client.post(
        "/api/mytube/uploads",
        json={"filename": "video.mp4", "size": 8, "title": "Video", "visibility": "shared"},
        headers={**DAVID, "Idempotency-Key": "missing-hash-key"},
    )
    assert missing_hash.status_code == 422
    unsupported = client.post(
        "/api/mytube/uploads",
        json={"filename": "video.mkv", "size": 8, "sha256": "a" * 64, "title": "Video", "visibility": "shared"},
        headers={**DAVID, "Idempotency-Key": "unsupported-key"},
    )
    assert unsupported.status_code == 422


def test_global_storage_reservations_prevent_overcommit(mytube_app, monkeypatch):
    monkeypatch.setattr(mytube, "_free_bytes", lambda: 12)
    assert reserve(mytube_app.test_client(), key="reserve-first", size=8).status_code == 201
    assert reserve(mytube_app.test_client(), key="reserve-second", size=8).status_code == 507


def test_exact_offset_and_bounded_chunks(mytube_app):
    client = mytube_app.test_client()
    upload_id = reserve(client).json["upload_id"]
    mismatch = client.patch(
        f"/api/mytube/uploads/{upload_id}", data=b"1234", headers={**DAVID, "Upload-Offset": "2"},
    )
    assert mismatch.status_code == 409
    assert mismatch.headers["Upload-Offset"] == "0"
    first = client.patch(
        f"/api/mytube/uploads/{upload_id}", data=b"1234", headers={**DAVID, "Upload-Offset": "0"},
    )
    assert first.status_code == 200 and first.json["offset"] == 4
    second = client.patch(
        f"/api/mytube/uploads/{upload_id}", data=b"5678", headers={**DAVID, "Upload-Offset": "4"},
    )
    assert second.status_code == 200 and second.json["complete"] is True


def test_uploads_are_owned_and_chunk_limit_is_enforced(mytube_app):
    client = mytube_app.test_client(); upload_id = reserve(client).json["upload_id"]
    assert client.patch(
        f"/api/mytube/uploads/{upload_id}", data=b"1234", headers={**DIANA, "Upload-Offset": "0"},
    ).status_code == 404
    oversized = reserve(client, key="upload-key-2", size=9).json["upload_id"]
    assert client.patch(
        f"/api/mytube/uploads/{oversized}", data=b"123456789", headers={**DAVID, "Upload-Offset": "0"},
    ).status_code == 422


def test_finalize_hashes_probes_and_publishes_atomically(mytube_app, monkeypatch):
    content = b"video123"
    client = mytube_app.test_client()
    response = reserve(client, size=len(content), sha256=hashlib.sha256(content).hexdigest())
    upload_id = response.json["upload_id"]
    client.patch(f"/api/mytube/uploads/{upload_id}", data=content, headers={**DAVID, "Upload-Offset": "0"})
    monkeypatch.setattr(mytube, "_probe", lambda _path: {"duration_seconds": 45.0, "width": 1280, "height": 720, "video_codec": "h264"})
    finalized = client.post(f"/api/mytube/uploads/{upload_id}/finalize", headers=DAVID)
    assert finalized.status_code == 201
    video_id = finalized.json["video_id"]
    assert (mytube.ORIGINALS / f"{video_id}.mp4").read_bytes() == content
    assert not (mytube.INCOMING / f"{upload_id}.part").exists()
    with connect(mytube.DB_PATH) as connection:
        row = connection.execute("SELECT * FROM mytube_videos WHERE id=?", (video_id,)).fetchone()
        job = connection.execute("SELECT * FROM mytube_prepare_jobs WHERE video_id=?", (video_id,)).fetchone()
    assert row["sha256"] == hashlib.sha256(content).hexdigest()
    assert row["duration_seconds"] == 45.0 and row["playback_state"] == "direct" and job is None


def test_bad_checksum_fails_without_publishing(mytube_app, monkeypatch):
    content = b"video123"; client = mytube_app.test_client()
    upload_id = reserve(client, size=len(content), sha256="0" * 64).json["upload_id"]
    client.patch(f"/api/mytube/uploads/{upload_id}", data=content, headers={**DAVID, "Upload-Offset": "0"})
    result = client.post(f"/api/mytube/uploads/{upload_id}/finalize", headers=DAVID)
    assert result.status_code == 422
    assert not list(mytube.ORIGINALS.iterdir())


def test_journaled_finalization_recovers_without_deleting_bytes(mytube_app, monkeypatch):
    content = b"recover1"; client = mytube_app.test_client()
    upload_id = reserve(client, key="recover-key", size=len(content), sha256=hashlib.sha256(content).hexdigest()).json["upload_id"]
    client.patch(f"/api/mytube/uploads/{upload_id}", data=content, headers={**DAVID, "Upload-Offset": "0"})
    video_id = "d" * 32
    with connect(mytube.DB_PATH) as connection:
        connection.execute("UPDATE mytube_uploads SET state='finalizing',video_id=? WHERE id=?", (video_id, upload_id))
    monkeypatch.setattr(mytube, "_probe", lambda _path: {"duration_seconds": 8.0, "width": 640, "height": 360, "video_codec": "h264"})
    mytube.recover_incomplete_uploads()
    assert (mytube.ORIGINALS / f"{video_id}.mp4").read_bytes() == content
    with connect(mytube.DB_PATH) as connection:
        assert connection.execute("SELECT state FROM mytube_uploads WHERE id=?", (upload_id,)).fetchone()[0] == "complete"
        assert connection.execute("SELECT COUNT(*) FROM mytube_videos WHERE id=?", (video_id,)).fetchone()[0] == 1


def test_upload_storage_recovery_removes_only_orphans_and_cancelled_staging(mytube_app):
    client = mytube_app.test_client()
    open_id = reserve(client, key="open-recovery").json["upload_id"]
    cancelled_id = reserve(client, key="cancel-recovery").json["upload_id"]
    orphan = mytube.INCOMING / ("f" * 32 + ".part")
    orphan.write_bytes(b"unpublished")
    with connect(mytube.DB_PATH) as connection:
        connection.execute("UPDATE mytube_uploads SET state='cancelled' WHERE id=?", (cancelled_id,))
    mytube.reconcile_upload_storage()
    assert (mytube.INCOMING / f"{open_id}.part").is_file()
    assert not (mytube.INCOMING / f"{cancelled_id}.part").exists()
    assert not orphan.exists()


def test_media_projection_reconciliation_repairs_missing_and_removes_stale(mytube_app):
    keep = MediaReference(
        video_id="b" * 32, media_id="c" * 32, title="Keep", owner_id="david@example.test",
        owner_name="David", visibility="shared", content_type="video/mp4", byte_size=8,
        sha256="d" * 64,
    )
    stale = MediaReference(
        video_id="e" * 32, media_id="f" * 32, title="Stale", owner_id="david@example.test",
        owner_name="David", visibility="shared", content_type="video/mp4", byte_size=8,
        sha256="a" * 64,
    )
    mytube.register_media_reference(stale)
    mytube.reconcile_media_references([keep])
    with connect(mytube.DB_PATH) as connection:
        assert connection.execute("SELECT visibility FROM mytube_videos WHERE id=?", (keep.video_id,)).fetchone()[0] == "shared"
        assert connection.execute("SELECT 1 FROM mytube_videos WHERE id=?", (stale.video_id,)).fetchone() is None


def test_upload_ui_hashes_actual_content_and_describes_direct_play(mytube_app):
    page = mytube_app.test_client().get("/mytube", headers=DAVID).get_data(as_text=True)
    javascript = (Path(__file__).parents[1] / "static" / "mytube.js").read_text(encoding="utf-8")
    assert "sha256-stream.js" in page and "H.264 video with AAC audio" in page
    assert "new Worker('/static/mytube-upload-worker.js?v=1')" in javascript and "sha256:contentSha256" in javascript
    worker_source=(Path(__file__).parents[1] / "static/mytube-upload-worker.js").read_text()
    assert "DavidPiSha256.create()" in worker_source and "file.slice(offset, offset + chunkSize).arrayBuffer()" in worker_source and "hash.hex()" in worker_source


def test_private_visibility_applies_to_metadata_and_streaming(mytube_app):
    video_id, _digest = insert_video(visibility="private")
    david = mytube_app.test_client().get(f"/api/mytube/videos/{video_id}", headers=DAVID)
    diana = mytube_app.test_client().get(f"/api/mytube/videos/{video_id}", headers=DIANA)
    diana_stream = mytube_app.test_client().get(f"/api/mytube/videos/{video_id}/stream", headers=DIANA)
    assert david.status_code == 200
    assert diana.status_code == 404 and diana_stream.status_code == 404


def test_stream_supports_exact_open_suffix_head_and_unsatisfied_ranges(mytube_app):
    video_id, digest = insert_video()
    client = mytube_app.test_client()
    partial = client.get(f"/api/mytube/videos/{video_id}/stream", headers={**DIANA, "Range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.data == b"2345"
    assert partial.headers["Content-Range"] == "bytes 2-5/10"
    assert partial.headers["ETag"] == f'"{digest}"'
    suffix = client.get(f"/api/mytube/videos/{video_id}/stream", headers={**DAVID, "Range": "bytes=-3"})
    assert suffix.data == b"789"
    head = client.head(f"/api/mytube/videos/{video_id}/stream", headers=DAVID)
    assert head.status_code == 200 and head.headers["Content-Length"] == "10"
    invalid = client.get(f"/api/mytube/videos/{video_id}/stream", headers={**DAVID, "Range": "bytes=50-60"})
    assert invalid.status_code == 416 and invalid.headers["Content-Range"] == "bytes */10"


def test_progress_is_per_identity_and_soft_delete_is_owner_only(mytube_app):
    video_id, _digest = insert_video()
    client = mytube_app.test_client()
    assert client.put(
        f"/api/mytube/videos/{video_id}/progress", json={"position_seconds": 7}, headers=DIANA,
    ).status_code == 200
    assert client.delete(f"/api/mytube/videos/{video_id}", headers=DIANA).status_code == 404
    assert client.delete(f"/api/mytube/videos/{video_id}", headers=DAVID).status_code == 200
    assert client.get(f"/api/mytube/videos/{video_id}", headers=DAVID).status_code == 404
    assert client.post(f"/api/mytube/videos/{video_id}/restore", headers=DAVID).status_code == 200
    with connect(mytube.DB_PATH) as connection:
        actions = {
            row[0]
            for row in connection.execute(
                "SELECT action FROM mutation_audit WHERE domain IN ('mytube_progress','mytube_video')"
            )
        }
    assert {"update", "trash", "restore"}.issubset(actions)


def test_media_projection_is_non_destructive_and_uses_runtime_resolver(mytube_app):
    media_id = "b" * 32; video_id = "c" * 32; content = b"media-video"
    media_path = mytube.ROOT.parent / "media-source.mp4"; media_path.write_bytes(content)
    reference = MediaReference(
        video_id=video_id, media_id=media_id, title="Existing media", owner_id="david@example.test",
        owner_name="David", visibility="shared", content_type="video/mp4", byte_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(), duration_seconds=20,
    )
    assert mytube.register_media_reference(reference) == video_id
    mytube_app.config["MYTUBE_MEDIA_RESOLVER"] = lambda requested: media_path if requested == media_id else None
    response = mytube_app.test_client().get(f"/api/mytube/videos/{video_id}/stream", headers=DIANA)
    assert response.status_code == 200 and response.data == content
    assert media_path.exists() and not list(mytube.ORIGINALS.iterdir())


def test_hls_rejects_traversal_and_enforces_visibility(mytube_app):
    video_id, _digest = insert_video(visibility="private")
    generation = mytube.STREAMING / video_id / "g00000001"; generation.mkdir(parents=True)
    (generation / "master.m3u8").write_text("#EXTM3U\n")
    with connect(mytube.DB_PATH) as connection:
        connection.execute("UPDATE mytube_videos SET playback_mode='hls',hls_generation='g00000001' WHERE id=?", (video_id,))
    client = mytube_app.test_client()
    assert client.get(f"/api/mytube/videos/{video_id}/hls/master.m3u8", headers=DAVID).status_code == 200
    assert client.get(f"/api/mytube/videos/{video_id}/hls/master.m3u8", headers=DIANA).status_code == 404
    assert client.get(f"/api/mytube/videos/{video_id}/hls/..%2Fsecret", headers=DAVID).status_code == 404


def test_worker_claim_is_single_lease_and_direct_prepare_is_atomic(mytube_app, monkeypatch):
    video_id, _digest = insert_video()
    with connect(mytube.DB_PATH) as connection:
        connection.execute(
            "INSERT INTO mytube_prepare_jobs(video_id,state,available_at,updated_at) VALUES(?,'pending',?,?)",
            (video_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    claimed = worker.claim_job()
    assert claimed is not None and worker.claim_job() is None
    row, token = claimed
    monkeypatch.setattr(worker, "_probe", lambda _path: {
        "format": "mov,mp4", "duration": 10.0, "video_codec": "h264", "audio_codec": "aac",
        "width": 1280, "height": 720,
    })
    monkeypatch.setattr(worker, "_poster", lambda _source, _video_id, _staging: f"{video_id}.webp")
    worker.prepare(row, token)
    with connect(mytube.DB_PATH) as connection:
        video = connection.execute("SELECT * FROM mytube_videos WHERE id=?", (video_id,)).fetchone()
        job = connection.execute("SELECT * FROM mytube_prepare_jobs WHERE video_id=?", (video_id,)).fetchone()
    assert job["state"] == "ready"
    assert video["playback_state"] == "ready" and video["playback_mode"] == "direct"


def test_worker_resource_gate_fails_closed_on_memory(mytube_app, monkeypatch):
    monkeypatch.setattr(worker, "resource_limits_enforced", lambda: True)
    monkeypatch.setattr(worker, "_temperature", lambda: 50.0)
    monkeypatch.setattr(worker, "_available_memory", lambda: worker.ABORT_AVAILABLE_BYTES - 1)
    monkeypatch.setattr(worker.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _path: type("Usage", (), {"free": worker.MIN_STORAGE_BYTES + 1})())
    gate = worker.resource_gate()
    assert gate.allowed is False and gate.abort is True and gate.reason == "resource_abort"


def test_worker_requires_kernel_enforced_cpu_and_memory_limits(tmp_path, monkeypatch):
    unified = tmp_path / "unified"
    unified.mkdir()
    (unified / "memory.max").write_text("1073741824\n")
    (unified / "cpu.max").write_text("150000 100000\n")
    assert worker.resource_limits_enforced(unified) is True
    (unified / "memory.max").write_text("max\n")
    assert worker.resource_limits_enforced(unified) is False

    monkeypatch.setattr(worker, "resource_limits_enforced", lambda: False)
    monkeypatch.setattr(worker, "_temperature", lambda: 50.0)
    monkeypatch.setattr(worker, "_available_memory", lambda: worker.MIN_AVAILABLE_BYTES + 1)
    monkeypatch.setattr(worker.os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(
        worker.shutil, "disk_usage",
        lambda _path: type("Usage", (), {"free": worker.MIN_STORAGE_BYTES + 1})(),
    )
    gate = worker.resource_gate()
    assert gate.allowed is False and gate.abort is True and gate.reason == "limits_unenforced"


def test_collection_visibility_prevents_private_leak(mytube_app):
    video_id, _digest = insert_video(visibility="private")
    client = mytube_app.test_client()
    created = client.post("/api/mytube/collections", json={"name": "Favorites", "visibility": "shared"}, headers=DAVID)
    assert created.status_code == 201
    blocked = client.put(f"/api/mytube/collections/{created.json['id']}/videos/{video_id}", headers=DAVID)
    assert blocked.status_code == 409
    private_collection = client.post(
        "/api/mytube/collections", json={"name": "Private", "visibility": "private"}, headers=DAVID,
    ).json
    assert client.put(f"/api/mytube/collections/{private_collection['id']}/videos/{video_id}", headers=DAVID).status_code == 200
    listed = client.get("/api/mytube/collections", headers=DIANA).json["collections"]
    assert [item["name"] for item in listed] == ["Favorites"]
