import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.secure_storage import PinnedStorageRoot
from modules import slideshow_worker as worker


SCHEMA = """
CREATE TABLE slideshow_jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    progress INTEGER NOT NULL,
    message TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    result_photo_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    owner_id TEXT,
    owner_name TEXT,
    visibility TEXT NOT NULL DEFAULT 'shared',
    version INTEGER NOT NULL DEFAULT 1,
    generation INTEGER NOT NULL DEFAULT 1,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    target_photo_id TEXT,
    target_name TEXT,
    target_dev INTEGER,
    target_ino INTEGER,
    target_size INTEGER,
    target_sha256 TEXT,
    started_at TEXT,
    finished_at TEXT,
    source_snapshot_sha256 TEXT,
    publish_intent_id TEXT,
    publish_state TEXT NOT NULL DEFAULT 'none',
    failure_code TEXT
);
CREATE TABLE media_publish_intents (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    preview_name TEXT NOT NULL,
    thumb_name TEXT NOT NULL,
    playback_name TEXT,
    content_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    legacy_sha256 TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    capture_timestamp TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source_device_id TEXT,
    ingestion_source TEXT NOT NULL,
    loop_playback INTEGER NOT NULL,
    collection_id TEXT,
    collection_version INTEGER,
    collection_visibility TEXT,
    source_path TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    requires_live_validator INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT
);
"""


def test_worker_app_import_does_not_bootstrap_unrelated_domain_databases(tmp_path):
    for name in (
        "originals",
        "previews",
        "viewer-previews",
        "thumbs",
        "incoming",
        "quarantine",
        "platform",
    ):
        (tmp_path / name).mkdir(mode=0o700)
    sqlite3.connect(tmp_path / "photos.db").close()
    environment = os.environ.copy()
    environment.update(
        {
            "PHOTO_DATA": str(tmp_path),
            "DAVID_PI_PLATFORM_DATA": str(tmp_path / "platform"),
            "DAVID_PI_WORKER_MODE": "slideshow",
            "DAVID_PI_SLIDESHOW_EXECUTOR_MODE": "worker",
            "DAVID_PI_DISABLE_METRICS": "1",
            "DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "platform" / "platform.db").exists()
    assert sorted(path.name for path in tmp_path.glob("*.db")) == ["photos.db"]


def config(tmp_path: Path, *, attempts: int = 3) -> worker.Config:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    return worker.Config(
        mode="worker",
        worker_id="test-worker",
        lease_seconds=60,
        renew_seconds=10,
        poll_seconds=1,
        max_attempts=attempts,
        queue_limit=3,
        min_free_bytes=1,
        max_target_bytes=64 * 1024 * 1024,
        max_job_seconds=120,
        ffmpeg_seconds=90,
        ffprobe_seconds=10,
        terminate_seconds=2,
        runtime_root=runtime,
    )


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def initialize(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def insert_job(
    connection: sqlite3.Connection,
    *,
    status: str = "queued",
    generation: int = 1,
    attempts: int = 0,
    lease_token: str | None = None,
    lease_owner: str | None = None,
    lease_expires_at: str | None = None,
    target_name: str | None = None,
    target_dev: int | None = None,
    target_ino: int | None = None,
    target_size: int | None = None,
    target_sha256: str | None = None,
) -> None:
    now = "2026-08-30T12:00:00+00:00"
    target = "1" * 32
    parameters = {
        "collection_id": "collection",
        "collection_version": 1,
        "collection_owner_id": "david@example.test",
        "collection_visibility": "shared",
        "collection_deleted_at": None,
        "source_name": "Collection",
        "result_visibility": "shared",
        "media_items": [
            {
                "id": "source",
                "version": 1,
                "owner_id": "david@example.test",
                "visibility": "shared",
                "deleted_at": None,
                "content_sha256": "3" * 64,
                "byte_size": 10,
                "stored_path": "source.jpg",
                "preview_name": "source.jpg",
                "playback_name": None,
                "content_type": "image/jpeg",
            }
        ],
        "duration_seconds": 30,
        "transition": "fade",
        "layout": "fill",
        "music_id": None,
        "loop_playback": False,
    }
    digest = worker._snapshot_digest(parameters)
    connection.execute(
        """INSERT INTO slideshow_jobs
           (id,status,progress,message,parameters_json,created_at,updated_at,
            owner_id,owner_name,visibility,generation,attempt_count,lease_owner,
            lease_token,lease_expires_at,target_photo_id,target_name,target_dev,
            target_ino,target_size,target_sha256,source_snapshot_sha256,
            publish_intent_id,publish_state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "a" * 32,
            status,
            5,
            "queued",
            json.dumps(parameters, sort_keys=True),
            now,
            now,
            "david@example.test",
            "David",
            "shared",
            generation,
            attempts,
            lease_owner,
            lease_token,
            lease_expires_at,
            target,
            target_name,
            target_dev,
            target_ino,
            target_size,
            target_sha256,
            digest,
            target,
            "none",
        ),
    )


def insert_intent(connection: sqlite3.Connection, job: dict, *, state: str) -> None:
    stamp = "2026-08-30T12:00:00+00:00"
    source_path = f"incoming/{job['target_name']}"
    artifacts = json.dumps(
        [
            {
                "kind": "original",
                "name": f"{job['target_photo_id']}.mp4",
                "byte_size": job["target_size"],
                "sha256": job["target_sha256"],
                "source_path": source_path,
                "source_dev": job["target_dev"],
                "source_ino": job["target_ino"],
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    parameters = json.loads(job["parameters_json"])
    connection.execute(
        """INSERT INTO media_publish_intents
           (id,owner_id,owner_name,original_name,stored_path,preview_name,thumb_name,
            playback_name,content_type,byte_size,content_sha256,legacy_sha256,taken_at,
            capture_timestamp,uploaded_at,uploaded_by,visibility,source_device_id,
            ingestion_source,loop_playback,collection_id,collection_version,
            collection_visibility,source_path,artifacts_json,requires_live_validator,
            state,created_at,committed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job["publish_intent_id"], job["owner_id"], job["owner_name"],
            "Slideshow.mp4", f"{job['target_photo_id']}.mp4",
            f"{job['target_photo_id']}.jpg", f"{job['target_photo_id']}.jpg",
            f"{job['target_photo_id']}.mp4", "video/mp4", job["target_size"],
            job["target_sha256"], job["target_sha256"], stamp, stamp, stamp,
            job["owner_name"], job["visibility"], None, "slideshow_generated",
            int(parameters["loop_playback"]), None, None, None, source_path,
            artifacts, 1, state, stamp, stamp if state == "committed" else None,
        ),
    )


def test_two_workers_cannot_claim_the_same_oldest_job(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    with connect(database) as connection:
        insert_job(connection)

    barrier = threading.Barrier(2)
    claimed = []
    errors = []

    def race(name):
        try:
            settings = config(tmp_path)
            settings = worker.Config(**{**settings.__dict__, "worker_id": name})
            connection = connect(database)
            try:
                barrier.wait()
                with connection:
                    result = worker.claim_next(connection, settings)
                claimed.append(result)
            finally:
                connection.close()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=race, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert sum(item is not None for item in claimed) == 1
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["status"] == "working"
    assert row["attempt_count"] == 1
    assert row["lease_owner"] in {"one", "two"}
    assert row["lease_token"]


def test_activation_blocks_legacy_working_job_without_valid_lease(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    with connect(database) as connection:
        insert_job(connection, status="working", attempts=1)
        with pytest.raises(worker.ActivationError, match="legacy working"):
            worker.verify_activation(connection)


def test_expired_valid_lease_is_requeued_with_a_new_generation(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    current = datetime(2026, 8, 30, 12, 10, tzinfo=timezone.utc)
    with connect(database) as connection:
        insert_job(
            connection,
            status="working",
            attempts=1,
            lease_owner="old-worker",
            lease_token="2" * 32,
            lease_expires_at=(current - timedelta(seconds=1)).isoformat(),
        )
        connection.commit()
        worker.verify_activation(connection, current)
        assert worker.recover_expired_jobs(connection, current) == 1
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["status"] == "queued"
    assert row["generation"] == 2
    assert row["attempt_count"] == 1
    assert row["lease_token"] is None
    assert row["failure_code"] == "lease_expired"


def test_retry_exhaustion_is_terminal_and_sanitized(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    with connect(database) as connection:
        insert_job(connection, attempts=3)
        connection.commit()
        assert worker.claim_next(connection, config(tmp_path, attempts=3)) is None
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["status"] == "failed"
    assert row["failure_code"] == "retry_exhausted"
    assert "ffmpeg" not in row["error"].lower()
    assert "source media" in row["error"].lower()


def test_expired_lease_cannot_be_renewed_or_mutate_job(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    with connect(database) as connection:
        insert_job(connection)
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        connection.execute(
            "UPDATE slideshow_jobs SET lease_expires_at=? WHERE id=?",
            (expired, job["id"]),
        )
        connection.commit()
        with pytest.raises(worker.JobLost, match="lease changed"):
            worker.renew_lease(connection, job, settings)
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["lease_expires_at"] == expired
    assert row["status"] == "working"


def test_keepalive_refreshes_health_during_a_blocked_job_phase(tmp_path, monkeypatch):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    settings = worker.Config(
        **{**settings.__dict__, "renew_seconds": 0.01, "max_job_seconds": 2}
    )
    with connect(database) as connection:
        insert_job(connection)
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()

    @contextmanager
    def database_context():
        connection = connect(database)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    health_calls = []
    refreshed_while_blocked = threading.Event()

    def record_health(_settings, *, ready, reason):
        health_calls.append((ready, reason))
        if len(health_calls) >= 2:
            refreshed_while_blocked.set()

    def blocked_phase(*_args):
        assert refreshed_while_blocked.wait(timeout=1)
        return job["target_photo_id"]

    finished = []
    portal = SimpleNamespace(db=database_context)
    monkeypatch.setattr(worker, "_write_health", record_health)
    monkeypatch.setattr(worker, "_process_job", blocked_phase)
    monkeypatch.setattr(
        worker, "_finish", lambda _portal, _job, photo_id: finished.append(photo_id)
    )

    worker.process_job(portal, job, settings)

    assert len(health_calls) >= 3  # initial, background, and final fenced pulse
    assert set(health_calls) == {(True, "working")}
    assert finished == [job["target_photo_id"]]


def test_keepalive_health_failure_blocks_completion(tmp_path, monkeypatch):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    settings = worker.Config(
        **{**settings.__dict__, "renew_seconds": 0.01, "max_job_seconds": 2}
    )
    with connect(database) as connection:
        insert_job(connection)
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()

    @contextmanager
    def database_context():
        connection = connect(database)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    health_calls = []
    background_failed = threading.Event()

    def fail_second_health(_settings, *, ready, reason):
        health_calls.append((ready, reason))
        if len(health_calls) == 2:
            background_failed.set()
            raise OSError("runtime unavailable")

    def blocked_phase(*_args):
        assert background_failed.wait(timeout=1)
        return job["target_photo_id"]

    portal = SimpleNamespace(db=database_context)
    monkeypatch.setattr(worker, "_write_health", fail_second_health)
    monkeypatch.setattr(worker, "_process_job", blocked_phase)
    monkeypatch.setattr(
        worker,
        "_finish",
        lambda *_args: pytest.fail("health failure reached completion"),
    )

    with pytest.raises(worker.JobFailure, match="worker_health_unavailable") as error:
        worker.process_job(portal, job, settings)
    assert error.value.retryable is True
    assert len(health_calls) == 2


def test_keepalive_never_reports_ready_after_lease_loss(tmp_path, monkeypatch):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    with connect(database) as connection:
        insert_job(connection)
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()
        connection.execute(
            "UPDATE slideshow_jobs SET lease_owner='replacement-worker' WHERE id=?",
            (job["id"],),
        )
        connection.commit()

    @contextmanager
    def database_context():
        connection = connect(database)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    health_calls = []
    portal = SimpleNamespace(db=database_context)
    keepalive = worker._JobKeepalive(
        portal,
        job,
        settings,
        lambda: False,
        worker.time.monotonic() + settings.max_job_seconds,
    )
    monkeypatch.setattr(
        worker,
        "_write_health",
        lambda *_args, **_kwargs: health_calls.append(True),
    )

    with pytest.raises(worker.JobLost, match="lease changed"):
        keepalive.pulse()
    assert health_calls == []


def test_ffmpeg_terminates_when_keepalive_loses_ownership(tmp_path, monkeypatch):
    target = os.open(tmp_path / "target.mp4", os.O_RDWR | os.O_CREAT, 0o600)
    process = SimpleNamespace(pid=43210, returncode=None, poll=lambda: None)
    terminated = []
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        worker,
        "_terminate",
        lambda observed, seconds: terminated.append((observed, seconds)),
    )
    keepalive = SimpleNamespace(
        check=lambda: (_ for _ in ()).throw(worker.JobLost("slideshow lease changed"))
    )
    try:
        with pytest.raises(worker.JobLost, match="lease changed"):
            worker._run_ffmpeg(
                ["ffmpeg"],
                (target,),
                target,
                {},
                config(tmp_path),
                SimpleNamespace(DATA=tmp_path),
                lambda: False,
                worker.time.monotonic() + 60,
                keepalive,
            )
    finally:
        os.close(target)
    assert terminated == [(process, 2)]


def test_failure_after_target_binding_cleans_exact_inode_and_clears_contract(tmp_path):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    incoming = tmp_path / "incoming"
    incoming.mkdir(mode=0o700)
    storage = PinnedStorageRoot(incoming)
    target_name = ".slideshow-" + "a" * 32 + "-g1-" + "b" * 16 + ".mp4"
    descriptor, metadata = storage.create_regular(target_name)
    try:
        os.write(descriptor, b"rendered-video")
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    with connect(database) as connection:
        insert_job(connection)
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()
        worker._owned_update(
            connection,
            job,
            {
                "target_name": target_name,
                "target_dev": metadata.st_dev,
                "target_ino": metadata.st_ino,
                "target_size": metadata.st_size,
                "target_sha256": "4" * 64,
            },
        )
        connection.commit()
    assert job["target_name"] == target_name

    @contextmanager
    def database_context():
        connection = connect(database)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    portal = SimpleNamespace(
        db=database_context,
        INCOMING_STORAGE=storage,
        storage_identity=lambda value: (value.st_dev, value.st_ino),
    )
    worker.record_failure(
        portal, job, settings, worker.JobFailure("render_failed", retryable=True)
    )
    assert not (incoming / target_name).exists()
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["status"] == "queued"
    assert row["generation"] == 2
    assert row["target_name"] is None
    assert row["target_ino"] is None


def test_inode_validated_cleanup_refuses_symlink_or_replacement(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir(mode=0o700)
    protected = tmp_path / "source.jpg"
    protected.write_bytes(b"source-media")
    target_name = ".slideshow-" + "a" * 32 + "-g1-" + "b" * 16 + ".mp4"
    target = incoming / target_name
    target.symlink_to(protected)
    metadata = target.lstat()
    database = tmp_path / "queue.db"
    initialize(database)
    with connect(database) as connection:
        insert_job(
            connection,
            target_name=target_name,
            target_dev=metadata.st_dev,
            target_ino=metadata.st_ino,
            target_size=len(b"source-media"),
            target_sha256="4" * 64,
        )
        job = dict(connection.execute("SELECT * FROM slideshow_jobs").fetchone())
    portal = SimpleNamespace(
        INCOMING_STORAGE=PinnedStorageRoot(incoming),
        storage_identity=lambda value: (value.st_dev, value.st_ino),
    )
    with pytest.raises(worker.JobFailure, match="unsafe_staging"):
        worker._safe_cleanup_target(portal, job)
    assert target.is_symlink()
    assert protected.read_bytes() == b"source-media"


def test_committed_publish_intent_completes_without_rendering_again(tmp_path, monkeypatch):
    database = tmp_path / "queue.db"
    initialize(database)
    settings = config(tmp_path)
    target_name = ".slideshow-" + "a" * 32 + "-g1-" + "b" * 16 + ".mp4"
    with connect(database) as connection:
        insert_job(
            connection,
            target_name=target_name,
            target_dev=10,
            target_ino=20,
            target_size=123,
            target_sha256="4" * 64,
        )
        connection.commit()
        job = worker.claim_next(connection, settings)
        connection.commit()
        insert_intent(connection, job, state="committed")

    @contextmanager
    def database_context():
        connection = connect(database)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    portal = SimpleNamespace(
        DATA=tmp_path,
        db=database_context,
        _media_intent_committed=lambda media_id: media_id == "1" * 32,
    )
    monkeypatch.setattr(worker, "_render", lambda *_args, **_kwargs: pytest.fail("rerendered"))
    worker.process_job(portal, job, settings)
    with connect(database) as connection:
        row = connection.execute("SELECT * FROM slideshow_jobs").fetchone()
    assert row["status"] == "completed"
    assert row["result_photo_id"] == "1" * 32
    assert row["publish_state"] == "committed"
    assert row["lease_token"] is None


def test_health_receipt_is_bounded_and_goes_unready_on_stop(tmp_path):
    settings = config(tmp_path)
    worker._write_health(settings, ready=True, reason="ready")
    assert worker.check_health(settings) is True
    path = settings.runtime_root / "health.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["updated_at"] = (
        datetime.now(timezone.utc)
        - timedelta(seconds=worker.HEALTH_MAX_AGE_SECONDS + 1)
    ).isoformat()
    path.write_text(json.dumps(value), encoding="utf-8")
    assert worker.check_health(settings) is False
    worker._write_health(settings, ready=False, reason="stopping")
    assert worker.check_health(settings) is False


def test_compose_and_lifecycle_include_exactly_one_isolated_executor():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    service = compose.split("  slideshow-worker:\n", 1)[1]
    assert compose.count("  slideshow-worker:\n") == 1
    assert 'DAVID_PI_SLIDESHOW_EXECUTOR_MODE: queue' in compose
    assert 'DAVID_PI_SLIDESHOW_EXECUTOR_MODE: worker' in service
    assert 'DAVID_PI_SLIDESHOW_QUEUE_LIMIT: "3"' in service
    assert 'DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING: "1"' in service
    assert 'command: ["python", "-m", "modules.slideshow_worker"]' in service
    assert "network_mode: none" in service
    assert 'user: "10001:10001"' in service
    assert "read_only: true" in service
    assert "cap_drop:\n      - ALL" in service
    assert "no-new-privileges:true" in service
    assert "mem_limit: 768m" in service
    assert "memswap_limit: 768m" in service
    assert "pids_limit: 96" in service
    assert "cpus: 1.5" in service
    assert "stop_grace_period: 30s" in service
    assert "/run/david-pi-slideshow:size=2m,mode=0700,uid=10001,gid=10001" in service
    assert "ports:" not in service
    assert "secrets:" not in service
    assert "ffmpeg" not in (root / "app.py").read_text(encoding="utf-8").split(
        '@app.post("/api/slideshows")', 1
    )[1].split('@app.get("/api/slideshows/<job_id>")', 1)[0]
    for relative in (
        "deploy/david-pi-data-backup",
        "deploy/david-pi-prepare-maintenance-state",
        "deploy/david-pi-writer-readiness",
        "deploy/david-pi-server-status.py",
        "Makefile",
    ):
        assert "slideshow" in (root / relative).read_text(encoding="utf-8")
