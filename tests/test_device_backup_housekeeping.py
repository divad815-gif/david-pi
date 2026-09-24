from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

device_backup = None
device_backup_worker = None


@pytest.fixture(scope="session", autouse=True)
def _load_device_backup_module(tmp_path_factory):
    global device_backup, device_backup_worker
    previous = os.environ.get("DAVID_PI_PLATFORM_DATA")
    os.environ["DAVID_PI_PLATFORM_DATA"] = str(
        tmp_path_factory.mktemp("device-backup-platform") / "platform"
    )
    device_backup = importlib.import_module("modules.device_backup")
    device_backup_worker = importlib.import_module("modules.device_backup_worker")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DAVID_PI_PLATFORM_DATA", None)
        else:
            os.environ["DAVID_PI_PLATFORM_DATA"] = previous


@contextmanager
def _database(path: Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _workspace(tmp_path: Path):
    data_root = tmp_path / "data"
    secondary_root = tmp_path / "secondary"
    (data_root / "originals").mkdir(parents=True)
    secondary_root.mkdir()
    sentinel_id = "secondary-test-volume"
    (secondary_root / ".david-pi-secondary-storage").write_text(
        sentinel_id, encoding="utf-8"
    )
    database_path = data_root / "photos.db"

    def db_context():
        return _database(database_path)

    with db_context() as connection:
        connection.execute(
            """CREATE TABLE photos (
                   id TEXT PRIMARY KEY,
                   stored_path TEXT NOT NULL,
                   sha256 TEXT,
                   content_sha256 TEXT,
                   secondary_verification_state TEXT NOT NULL
               )"""
        )
        connection.execute(
            """CREATE TABLE media_publish_intents (
                   id TEXT PRIMARY KEY,
                   state TEXT NOT NULL
               )"""
        )
        device_backup.initialize_device_backup(connection)
        connection.execute(
            """INSERT INTO backup_devices
               (id,credential_hash,owner_user_id,owner_name,display_name,platform,created_at)
               VALUES ('device-1','credential','owner-1','David','Test phone','android',?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
    return data_root, secondary_root, sentinel_id, db_context


def _add_record(
    data_root: Path,
    db_context,
    *,
    suffix: str,
    content: bytes,
    expected_hash: str | None = None,
    state: str = "secondary_pending",
    attempts: int = 0,
    lease_token: str | None = None,
    lease_expires_at: str | None = None,
) -> str:
    digest = expected_hash or hashlib.sha256(content).hexdigest()
    stored_path = f"2026/09/{suffix}.jpg"
    source = data_root / "originals" / stored_path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    with db_context() as connection:
        connection.execute(
            """INSERT INTO photos
               (id,stored_path,sha256,content_sha256,secondary_verification_state)
               VALUES (?,?,?,?,?)""",
            (suffix, stored_path, digest, digest, state),
        )
        connection.execute(
            """INSERT INTO device_media_records
               (id,device_id,client_item_id,media_id,owner_user_id,original_filename,
                content_sha256,byte_size,ingestion_source,primary_verification_state,
                secondary_verification_state,secondary_attempts,secondary_lease_token,
                secondary_lease_expires_at,ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"record-{suffix}",
                "device-1",
                f"item-{suffix}",
                suffix,
                "owner-1",
                f"{suffix}.jpg",
                digest,
                len(content),
                "android_backup",
                "primary_verified",
                state,
                attempts,
                lease_token,
                lease_expires_at,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return stored_path


def _record(db_context, suffix: str):
    with db_context() as connection:
        return connection.execute(
            """SELECT secondary_verification_state,secondary_attempts,
                      secondary_lease_token,secondary_lease_expires_at
               FROM device_media_records WHERE id=?""",
            (f"record-{suffix}",),
        ).fetchone()


def _worker_workspace(tmp_path: Path, *, secondary: bool = False):
    data_root = tmp_path / "worker-data"
    runtime_root = tmp_path / "worker-runtime"
    (data_root / "originals").mkdir(parents=True)
    (data_root / "incoming" / "device-backup").mkdir(parents=True)
    runtime_root.mkdir(mode=0o700)
    expected_id = "primary-test-volume"
    (data_root / ".david-pi-storage").write_text(expected_id, encoding="utf-8")
    database_path = data_root / "photos.db"

    def db_context():
        return _database(database_path)

    with db_context() as connection:
        connection.execute(
            """CREATE TABLE photos (
                   id TEXT PRIMARY KEY,
                   stored_path TEXT NOT NULL,
                   sha256 TEXT,
                   content_sha256 TEXT,
                   secondary_verification_state TEXT NOT NULL
               )"""
        )
        connection.execute(
            """CREATE TABLE media_publish_intents (
                   id TEXT PRIMARY KEY,
                   state TEXT NOT NULL
               )"""
        )
        device_backup.initialize_device_backup(connection)
        connection.execute(
            """INSERT INTO backup_devices
               (id,credential_hash,owner_user_id,owner_name,display_name,platform,created_at)
               VALUES ('device-1','credential','owner-1','David','Test phone','android',?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
    secondary_root = None
    secondary_id = ""
    if secondary:
        secondary_root = tmp_path / "worker-secondary"
        secondary_root.mkdir()
        secondary_id = "secondary-test-volume"
        (secondary_root / ".david-pi-secondary-storage").write_text(
            secondary_id, encoding="utf-8"
        )
    config = device_backup_worker.Config(
        data_root=data_root,
        primary_sentinel=data_root / ".david-pi-storage",
        runtime_root=runtime_root,
        expected_data_id=expected_id,
        interval_seconds=10,
        heartbeat_seconds=5,
        secondary_batch_size=2,
        secondary_root=secondary_root,
        secondary_sentinel_id=secondary_id,
    )
    return config, db_context


@contextmanager
def _independent_secondary(config):
    if not Path("/dev/shm").is_dir():
        pytest.skip("a distinct tmpfs is required for the independence test")
    root = Path(tempfile.mkdtemp(prefix="device-backup-test-", dir="/dev/shm"))
    if root.stat().st_dev == config.data_root.stat().st_dev:
        shutil.rmtree(root)
        pytest.skip("a distinct test filesystem is unavailable")
    sentinel_id = "secondary-test-volume"
    (root / "originals").mkdir()
    (root / ".david-pi-secondary-storage").write_text(sentinel_id, encoding="utf-8")
    try:
        yield replace(
            config,
            secondary_root=root,
            secondary_sentinel_id=sentinel_id,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_secondary_copy_is_verified_and_durably_published(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    content = b"verified-secondary-copy"
    stored_path = _add_record(data_root, db_context, suffix="one", content=content)

    assert device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )

    assert (secondary_root / "originals" / stored_path).read_bytes() == content
    row = _record(db_context, "one")
    assert tuple(row) == ("fully_protected", 1, None, None)


def test_expired_copy_lease_reclaims_already_published_destination(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    content = b"published-before-worker-crash"
    stored_path = _add_record(
        data_root,
        db_context,
        suffix="crash",
        content=content,
        state="secondary_copying",
        attempts=1,
        lease_token="dead-worker",
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    destination = secondary_root / "originals" / stored_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    before = destination.stat().st_ino

    assert device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )

    assert destination.stat().st_ino == before
    row = _record(db_context, "crash")
    assert tuple(row) == ("fully_protected", 2, None, None)


def test_secondary_directory_fsync_failure_is_retryable_and_never_claimed_protected(
    tmp_path,
):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    content = b"directory-fsync-is-part-of-publication"
    _add_record(data_root, db_context, suffix="fsync", content=content)

    with patch.object(
        device_backup,
        "_fsync_directory_chain",
        side_effect=OSError("simulated fsync failure"),
    ):
        assert not device_backup.secondary_verify_once(
            db_context, data_root, secondary_root, sentinel_id
        )
    first = _record(db_context, "fsync")
    assert tuple(first) == ("secondary_error", 1, None, None)

    assert device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )
    second = _record(db_context, "fsync")
    assert tuple(second) == ("fully_protected", 2, None, None)


def test_transient_copy_error_retries_and_then_succeeds(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    content = b"retryable-secondary-copy"
    _add_record(data_root, db_context, suffix="transient", content=content)

    with patch.object(device_backup.os, "replace", side_effect=OSError("offline")):
        assert not device_backup.secondary_verify_once(
            db_context, data_root, secondary_root, sentinel_id
        )
    assert tuple(_record(db_context, "transient")) == (
        "secondary_error",
        1,
        None,
        None,
    )

    assert device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )
    assert tuple(_record(db_context, "transient")) == (
        "fully_protected",
        2,
        None,
        None,
    )


def test_hash_mismatch_retries_are_bounded(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    _add_record(
        data_root,
        db_context,
        suffix="mismatch",
        content=b"changed-primary-content",
        expected_hash=hashlib.sha256(b"expected-primary-content").hexdigest(),
    )

    for attempt in range(1, device_backup.SECONDARY_MAX_ATTEMPTS + 1):
        assert not device_backup.secondary_verify_once(
            db_context, data_root, secondary_root, sentinel_id
        )
        row = _record(db_context, "mismatch")
        assert row[1] == attempt
        assert row[0] == (
            "secondary_failed"
            if attempt == device_backup.SECONDARY_MAX_ATTEMPTS
            else "secondary_hash_mismatch"
        )
    assert not device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )
    assert _record(db_context, "mismatch")[1] == device_backup.SECONDARY_MAX_ATTEMPTS


def test_stale_worker_cannot_commit_after_lease_is_reassigned(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    _add_record(data_root, db_context, suffix="fenced", content=b"lease-fenced-copy")

    def steal_lease(*_args):
        with db_context() as connection:
            connection.execute(
                """UPDATE device_media_records
                   SET secondary_lease_token='replacement-worker',
                       secondary_lease_expires_at=?
                   WHERE id='record-fenced'""",
                ((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),),
            )

    with patch.object(device_backup, "_fsync_directory_chain", side_effect=steal_lease):
        assert not device_backup.secondary_verify_once(
            db_context, data_root, secondary_root, sentinel_id
        )

    row = _record(db_context, "fenced")
    assert row[0] == "secondary_copying"
    assert row[2] == "replacement-worker"


def test_same_hash_is_only_marked_protected_after_every_distinct_path_verifies(tmp_path):
    data_root, secondary_root, sentinel_id, db_context = _workspace(tmp_path)
    content = b"same-content-in-two-logical-media-rows"
    digest = hashlib.sha256(content).hexdigest()
    first = _add_record(
        data_root,
        db_context,
        suffix="duplicate-a",
        content=content,
        expected_hash=digest,
    )
    second = _add_record(
        data_root,
        db_context,
        suffix="duplicate-b",
        content=content,
        expected_hash=digest,
    )

    assert device_backup.secondary_verify_once(
        db_context, data_root, secondary_root, sentinel_id
    )

    assert (secondary_root / "originals" / first).read_bytes() == content
    assert (secondary_root / "originals" / second).read_bytes() == content
    assert _record(db_context, "duplicate-a")[0] == "fully_protected"
    assert _record(db_context, "duplicate-b")[0] == "fully_protected"


def test_housekeeping_entrypoint_uses_the_lease_fenced_secondary_verifier():
    source = Path(device_backup.__file__).read_text(encoding="utf-8")
    nested = source.split("def secondary_verify_one() -> bool:", 1)[1].split(
        "def housekeeping()", 1
    )[0]
    assert "secondary_verify_once(" in nested
    assert "secondary_verification_state='secondary_pending'" not in nested


def test_dedicated_worker_retires_stale_upload_and_releases_global_slot(tmp_path):
    config, db_context = _worker_workspace(tmp_path)
    device_root = config.incoming_root / "device-1"
    device_root.mkdir()
    part = device_root / "stale.part"
    part.write_bytes(b"unfinished")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db_context() as connection:
        connection.execute(
            """INSERT INTO device_uploads
               (id,device_id,client_item_id,original_filename,expected_size,
                expected_sha256,mime_type,accepted_offset,part_path,state,created_at,updated_at)
               VALUES ('stale','device-1','item-stale','stale.jpg',10,?,'image/jpeg',0,?,
                       'uploading',?,?)""",
            ("a" * 64, str(part), stale, stale),
        )

    lease = device_backup_worker.Lease(config)
    try:
        result = device_backup_worker.run_cycle(config, lease, lambda _reason: None)
    finally:
        lease.close()

    assert result["retired_uploads"] == 1
    assert not part.exists()
    with db_context() as connection:
        assert connection.execute(
            "SELECT 1 FROM device_uploads WHERE id='stale'"
        ).fetchone() is None


def test_corrupt_stale_part_path_cannot_hold_slot_or_delete_external_file(tmp_path):
    config, db_context = _worker_workspace(tmp_path)
    outside = tmp_path / "must-not-delete.part"
    outside.write_bytes(b"unrelated")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with db_context() as connection:
        connection.execute(
            """INSERT INTO device_uploads
               (id,device_id,client_item_id,original_filename,expected_size,
                expected_sha256,mime_type,accepted_offset,part_path,state,created_at,updated_at)
               VALUES ('unsafe','device-1','item-unsafe','unsafe.jpg',10,?,'image/jpeg',0,?,
                       'retryable_error',?,?)""",
            ("b" * 64, str(outside), stale, stale),
        )

    lease = device_backup_worker.Lease(config)
    try:
        result = device_backup_worker.run_cycle(config, lease, lambda _reason: None)
    finally:
        lease.close()

    assert result["retired_uploads"] == 1
    assert outside.read_bytes() == b"unrelated"
    with db_context() as connection:
        assert connection.execute(
            "SELECT 1 FROM device_uploads WHERE id='unsafe'"
        ).fetchone() is None


def _seed_stale_ingest(config, db_context, *, suffix: str):
    device_root = config.incoming_root / "device-1"
    device_root.mkdir(exist_ok=True)
    part = device_root / f"{suffix}.part"
    part.write_bytes(b"verified-but-not-published")
    stale = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    media_id = f"media-{suffix}"
    upload_id = f"upload-{suffix}"
    with db_context() as connection:
        connection.execute(
            """INSERT INTO device_uploads
               (id,device_id,client_item_id,original_filename,expected_size,
                expected_sha256,mime_type,accepted_offset,part_path,state,media_id,
                created_at,updated_at)
               VALUES (?, 'device-1', ?, ?, ?, ?, 'image/jpeg', ?, ?, 'ingesting', ?, ?, ?)""",
            (
                upload_id, f"item-{suffix}", f"{suffix}.jpg", part.stat().st_size,
                hashlib.sha256(part.read_bytes()).hexdigest(), part.stat().st_size,
                str(part), media_id, stale, stale,
            ),
        )
        connection.execute(
            """INSERT INTO device_ingest_intents
               (id,device_id,client_item_id,upload_id,original_filename,content_sha256,
                byte_size,mime_type,ingestion_source,state,created_at,updated_at)
               SELECT ?,device_id,client_item_id,id,original_filename,expected_sha256,
                      expected_size,mime_type,'android_backup','prepared',created_at,updated_at
               FROM device_uploads WHERE id=?""",
            (media_id, upload_id),
        )
    return upload_id, media_id, part


def test_stale_prepublication_ingest_is_retired_without_saved_content(tmp_path):
    config, db_context = _worker_workspace(tmp_path)
    upload_id, media_id, part = _seed_stale_ingest(
        config, db_context, suffix="safe-retire"
    )

    lease = device_backup_worker.Lease(config)
    try:
        result = device_backup_worker.run_cycle(config, lease, lambda _reason: None)
    finally:
        lease.close()

    assert result["retired_uploads"] == 1
    assert not part.exists()
    with db_context() as connection:
        assert connection.execute(
            "SELECT 1 FROM device_uploads WHERE id=?", (upload_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM device_ingest_intents WHERE id=?", (media_id,)
        ).fetchone() is None


def test_stale_ingest_with_canonical_evidence_is_preserved_for_recovery(tmp_path):
    config, db_context = _worker_workspace(tmp_path)
    upload_id, media_id, part = _seed_stale_ingest(
        config, db_context, suffix="preserve"
    )
    with db_context() as connection:
        connection.execute(
            "INSERT INTO media_publish_intents(id,state) VALUES (?,'prepared')",
            (media_id,),
        )

    lease = device_backup_worker.Lease(config)
    try:
        result = device_backup_worker.run_cycle(config, lease, lambda _reason: None)
    finally:
        lease.close()

    assert result["retired_uploads"] == 0
    assert part.read_bytes() == b"verified-but-not-published"
    with db_context() as connection:
        assert connection.execute(
            "SELECT state FROM device_uploads WHERE id=?", (upload_id,)
        ).fetchone()[0] == "ingesting"
        assert connection.execute(
            "SELECT state FROM device_ingest_intents WHERE id=?", (media_id,)
        ).fetchone()[0] == "prepared"


def test_dedicated_worker_lock_releases_on_process_owner_shutdown(tmp_path):
    config, _db_context = _worker_workspace(tmp_path)
    first = device_backup_worker.Lease(config)
    try:
        with pytest.raises(device_backup_worker.ActivationError, match="singleton"):
            device_backup_worker.Lease(config)
    finally:
        first.close()
    replacement = device_backup_worker.Lease(config)
    replacement.close()


def test_worker_health_is_fresh_private_and_fail_closed_when_stopping(tmp_path):
    config, _db_context = _worker_workspace(tmp_path)
    publisher = device_backup_worker.HealthPublisher(config)
    publisher.succeeded()
    publisher.publish()
    assert device_backup_worker.check_health(config)
    value = __import__("json").loads(
        (config.runtime_root / device_backup_worker.HEALTH_NAME).read_text()
    )
    assert value["privacy"]["contains_user_content"] is False

    publisher.close()
    assert not device_backup_worker.check_health(config)


def test_worker_cycle_reclaims_expired_secondary_publication_after_restart(tmp_path):
    config, db_context = _worker_workspace(tmp_path)
    content = b"worker-crashed-after-secondary-publish"
    stored_path = _add_record(
        config.data_root,
        db_context,
        suffix="worker-crash",
        content=content,
        state="secondary_copying",
        attempts=1,
        lease_token="crashed-process",
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    )
    with _independent_secondary(config) as config:
        destination = config.secondary_root / "originals" / stored_path
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)
        lease = device_backup_worker.Lease(config)
        try:
            result = device_backup_worker.run_cycle(config, lease, lambda _reason: None)
        finally:
            lease.close()

        assert result["secondary_copies"] == 1
    assert tuple(_record(db_context, "worker-crash")) == (
        "fully_protected",
        2,
        None,
        None,
    )


def test_configured_secondary_wrong_sentinel_blocks_activation(tmp_path):
    config, _db_context = _worker_workspace(tmp_path)
    with _independent_secondary(config) as configured:
        (configured.secondary_root / ".david-pi-secondary-storage").write_text(
            "wrong-volume", encoding="utf-8"
        )
        with pytest.raises(device_backup_worker.ActivationError, match="secondary"):
            device_backup_worker.Lease(configured)


def test_configured_secondary_missing_mount_blocks_activation(tmp_path):
    config, _db_context = _worker_workspace(tmp_path)
    configured = replace(
        config,
        secondary_root=tmp_path / "missing-secondary",
        secondary_sentinel_id="secondary-test-volume",
    )
    with pytest.raises(device_backup_worker.ActivationError, match="worker storage"):
        device_backup_worker.Lease(configured)


def test_secondary_disconnect_or_identity_replacement_invalidates_worker_lease(tmp_path):
    config, _db_context = _worker_workspace(tmp_path)
    with _independent_secondary(config) as configured:
        lease = device_backup_worker.Lease(configured)
        original = configured.secondary_root / "originals"
        moved = configured.secondary_root / "originals-old"
        original.rename(moved)
        original.mkdir()
        try:
            with pytest.raises(device_backup_worker.ActivationError, match="identity"):
                lease.validate()
        finally:
            lease.close()
