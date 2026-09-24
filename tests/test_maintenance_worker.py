import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from modules import maintenance_worker as maintenance
from modules.maintenance_observations import ObservationError


EXPECTED_ID = "david-pi-family-storage-v1"


def _config(workspace: Path, sentinel=EXPECTED_ID, **changes) -> maintenance.Config:
    data = workspace / "data"
    operations = data.joinpath(*maintenance.OPERATIONS_COMPONENTS)
    runtime = workspace / "runtime"
    host = workspace / "host"
    data.mkdir(parents=True, exist_ok=True)
    operations.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    host.mkdir(parents=True, exist_ok=True)
    operations.chmod(0o750)
    runtime.chmod(0o700)
    (data / ".david-pi-storage").write_text(sentinel, encoding="utf-8")
    values = {
        "data_root": data,
        "operations_root": operations,
        "operations_anchor": operations,
        "runtime_root": runtime,
        "server_status_path": host / "server-status.json",
        "expected_data_id": EXPECTED_ID,
        "operations_uid": os.geteuid(),
    }
    values.update(changes)
    return maintenance.Config(**values)


def _try_acquire(config: maintenance.Config, queue) -> None:
    try:
        lease = maintenance.acquire_singleton(config)
        queue.put(lease is not None)
        if lease is not None:
            lease.close()
    except Exception as error:  # pragma: no cover - reported to parent process
        queue.put(type(error).__name__)


def _server_status(generated_at: str) -> dict:
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "state": "healthy",
        "privacy": {
            "contains_personal_filenames": False,
            "contains_domains": False,
            "contains_clients": False,
            "contains_secrets": False,
        },
        "databases": {},
        "subsystems": {
            "portal": {
                "details": {
                    "cpu_percent": "12.5%",
                    "memory_percent": "9%",
                    "health_latency_ms": 14,
                }
            },
            "storage": {
                "details": {
                    "external": {"used_percent": 35.0},
                    "microsd": {"used_percent": 20.0},
                }
            },
            "temperature_power": {
                "details": {
                    "temperature_c": 51.2,
                    "load_average": [0.1, 0.2, 0.3],
                    "ram_total_gb": 8,
                    "ram_available_gb": 6,
                    "swap_total_gb": 1,
                    "swap_free_gb": 0.75,
                }
            },
            "background_jobs": {
                "details": {"slideshows": {"active": 1, "pending": 2}}
            },
            "backups": {"state": "healthy"},
        },
    }


def _write_server_status(config: maintenance.Config, generated_at: str) -> None:
    config.server_status_path.write_text(
        json.dumps(_server_status(generated_at)), encoding="utf-8"
    )


def _healthy_status(updated_at: str | None = None) -> dict:
    stamp = updated_at or datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 2,
        "worker": "david-pi-maintenance",
        "state": "healthy",
        "started_at": stamp,
        "updated_at": stamp,
        "privacy": maintenance.STATUS_PRIVACY,
        "tasks": {"probe": {"state": "ok", "error_code": "none"}},
    }


def test_worker_import_does_not_import_web_application():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import modules.maintenance_worker; "
            "raise SystemExit(1 if 'app' in sys.modules else 0)",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_sentinel_mismatch_causes_zero_worker_writes(tmp_path):
    config = _config(tmp_path, sentinel="wrong-device")
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    with pytest.raises(maintenance.MaintenanceError, match="sentinel_invalid"):
        maintenance.run_once(config)

    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before
    assert not config.lock_path.exists()
    assert not config.status_path.exists()
    assert not config.heartbeat_path.exists()
    assert not config.metrics_db.exists()


def test_directory_inode_flock_allows_only_one_process(tmp_path):
    config = _config(tmp_path)
    held = maintenance.acquire_singleton(config)
    assert held is not None
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_try_acquire, args=(config, queue))
    process.start()
    process.join(timeout=5)
    try:
        assert process.exitcode == 0
        assert queue.get(timeout=1) is False
    finally:
        held.close()


def test_replacing_lock_path_does_not_create_a_second_singleton(tmp_path):
    config = _config(tmp_path)
    held = maintenance.acquire_singleton(config)
    assert held is not None
    config.lock_path.unlink()
    config.lock_path.write_bytes(b"replacement")
    config.lock_path.chmod(0o640)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_try_acquire, args=(config, queue))
    process.start()
    process.join(timeout=5)
    try:
        assert process.exitcode == 0
        assert queue.get(timeout=1) is False
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            held.validate()
    finally:
        held.close()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_lock_alias_is_rejected_before_acquisition(tmp_path, alias):
    config = _config(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    if alias == "symlink":
        config.lock_path.symlink_to(victim)
    else:
        os.link(victim, config.lock_path)

    with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
        maintenance.acquire_singleton(config)

    assert victim.read_bytes() == b"unchanged"
    assert not config.status_path.exists()


@pytest.mark.parametrize("name,publisher", [
    (maintenance.STATUS_NAME, maintenance.publish_persistent_status),
    (maintenance.HEARTBEAT_NAME, maintenance.publish_heartbeat),
])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_status_and_heartbeat_aliases_are_rejected(tmp_path, name, publisher, alias):
    config = _config(tmp_path)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    victim = tmp_path / f"{name}-victim"
    victim.write_bytes(b"unchanged")
    target = config.status_path if name == maintenance.STATUS_NAME else config.heartbeat_path
    if alias == "symlink":
        target.symlink_to(victim)
    else:
        os.link(victim, target)
    try:
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            publisher(lease, _healthy_status())
        assert victim.read_bytes() == b"unchanged"
    finally:
        lease.close()


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_metrics_alias_is_rejected_before_sqlite_write(tmp_path, alias):
    config = _config(tmp_path)
    _write_server_status(config, datetime.now(timezone.utc).isoformat())
    victim = tmp_path / "metrics-victim"
    victim.write_bytes(b"unchanged")
    if alias == "symlink":
        config.metrics_db.symlink_to(victim)
    else:
        os.link(victim, config.metrics_db)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    try:
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            maintenance.write_metric_sample(lease)
        assert victim.read_bytes() == b"unchanged"
    finally:
        lease.close()


def test_metrics_inode_swap_is_detected_after_secure_creation(tmp_path):
    config = _config(tmp_path)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    descriptor, identity = lease.operations.open_regular(
        maintenance.METRICS_NAME, os.O_RDWR, create=True
    )
    config.metrics_db.unlink()
    config.metrics_db.write_bytes(b"replacement")
    config.metrics_db.chmod(0o640)
    try:
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            maintenance._validate_metrics_files(lease, identity)
    finally:
        os.close(descriptor)
        lease.close()


def test_operations_mount_must_be_same_pinned_inode_as_read_only_anchor(tmp_path):
    config = _config(tmp_path)
    alternate = tmp_path / "alternate-operations"
    alternate.mkdir()
    alternate.chmod(0o700)
    config = maintenance.Config(**{**config.__dict__, "operations_root": alternate})

    with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
        maintenance.acquire_singleton(config)

    assert not (alternate / maintenance.LOCK_NAME).exists()


def test_symlinked_data_component_is_not_traversed_for_preview(tmp_path):
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    database = outside / "notes.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE notes (id TEXT, deleted_at TEXT)")
    (config.data_root / "platform").symlink_to(outside)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    try:
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            maintenance.preview_note_retention(lease)
    finally:
        lease.close()


def test_cross_device_operations_mount_is_rejected_without_write(tmp_path):
    shm = Path("/dev/shm")
    if not shm.is_dir() or shm.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("no distinct temporary filesystem available")
    config = _config(tmp_path)
    with tempfile.TemporaryDirectory(dir=shm) as temporary:
        alternate = Path(temporary)
        alternate.chmod(0o700)
        cross_device = maintenance.Config(
            **{**config.__dict__, "operations_root": alternate}
        )
        with pytest.raises(maintenance.MaintenanceError, match="storage_identity_changed"):
            maintenance.acquire_singleton(cross_device)
        assert not (alternate / maintenance.LOCK_NAME).exists()


def test_task_failures_are_bounded_and_do_not_stop_independent_tasks(tmp_path):
    config = _config(tmp_path)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    completed = []

    def fail(_lease):
        raise RuntimeError("private path and content must never enter status")

    def succeed(_lease):
        completed.append("second")
        return {"candidate_count": 4, "unsafe": "saved title"}

    worker = maintenance.Worker(
        config,
        [
            maintenance.Task("first", 60, True, fail),
            maintenance.Task("second", 60, True, succeed),
        ],
    )
    try:
        status = worker.run_due(lease, monotonic_now=10, wall_now=2_000_000_000)
    finally:
        lease.close()

    assert completed == ["second"]
    assert status["state"] == "degraded"
    assert status["tasks"]["first"]["error_code"] == "task_failed"
    assert status["tasks"]["second"]["state"] == "ok"
    assert status["tasks"]["second"]["candidate_count"] == 4
    assert "unsafe" not in status["tasks"]["second"]
    assert "private" not in json.dumps(status)
    assert {
        item["error_code"] for item in status["tasks"].values()
    } <= maintenance.ALLOWED_ERROR_CODES


def test_heartbeat_and_status_are_metadata_only_and_health_bounded(tmp_path):
    config = _config(tmp_path)
    task = maintenance.Task(
        "preview", 60, True, lambda _lease: {"candidate_count": 2, "mode": "preview"}
    )

    assert maintenance.run_once(config, [task]) == 0
    status = json.loads(config.status_path.read_text(encoding="utf-8"))
    heartbeat = json.loads(config.heartbeat_path.read_text(encoding="utf-8"))

    assert status["privacy"] == maintenance.STATUS_PRIVACY
    assert heartbeat["privacy"] == maintenance.STATUS_PRIVACY
    assert set(heartbeat) == {
        "schema_version", "worker", "state", "updated_at", "privacy"
    }
    serialized = json.dumps({"status": status, "heartbeat": heartbeat})
    assert str(config.data_root) not in serialized
    assert "@" not in serialized
    assert maintenance.check_health(config)
    updated = datetime.fromisoformat(heartbeat["updated_at"]).timestamp()
    assert not maintenance.check_health(config, maximum_age=90, now=updated + 91)


def test_degraded_heartbeat_fails_healthcheck(tmp_path):
    config = _config(tmp_path)
    task = maintenance.Task(
        "failure", 60, True, lambda _lease: (_ for _ in ()).throw(RuntimeError())
    )
    assert maintenance.run_once(config, [task]) == 0
    assert json.loads(config.heartbeat_path.read_text())["state"] == "degraded"
    assert not maintenance.check_health(config)


def test_status_persistence_ignores_heartbeat_timestamps_until_interval():
    first = _healthy_status("2026-01-01T00:00:00+00:00")
    changed_timestamp = _healthy_status("2026-01-01T00:00:15+00:00")
    due, fingerprint = maintenance.status_write_due(first, None, 0, 10, 900)
    assert due
    due, same_fingerprint = maintenance.status_write_due(
        changed_timestamp, fingerprint, 10, 25, 900
    )
    assert not due
    assert same_fingerprint == fingerprint
    assert maintenance.status_write_due(
        changed_timestamp, fingerprint, 10, 910, 900
    )[0]
    changed_timestamp["state"] = "degraded"
    assert maintenance.status_write_due(
        changed_timestamp, fingerprint, 10, 25, 900
    )[0]


def test_maximum_heartbeat_interval_matches_ninety_second_health_window(tmp_path):
    config = _config(tmp_path, heartbeat_interval=31)
    with pytest.raises(maintenance.MaintenanceError, match="configuration_invalid"):
        maintenance.acquire_singleton(config)
    assert not config.lock_path.exists()


@pytest.mark.parametrize(
    "offset,generated,error_code",
    [
        (-901, None, "observation_stale"),
        (6, None, "observation_invalid"),
        (0, "2026-01-01T00:00:00", "observation_invalid"),
    ],
)
def test_invalid_stale_or_future_observation_is_rejected_before_metrics_write(
    tmp_path, offset, generated, error_code
):
    now = 2_000_000_000.0
    stamp = generated or datetime.fromtimestamp(
        now + offset, timezone.utc
    ).isoformat()
    config = _config(tmp_path)
    _write_server_status(config, stamp)
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    try:
        with pytest.raises(ObservationError, match=error_code):
            maintenance.write_metric_sample(lease, now=now)
        assert not config.metrics_db.exists()
    finally:
        lease.close()


def test_metric_timestamp_is_collector_generated_at_not_worker_clock(tmp_path):
    now = 2_000_000_000.0
    generated = now - 42
    config = _config(tmp_path)
    _write_server_status(
        config, datetime.fromtimestamp(generated, timezone.utc).isoformat()
    )
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    try:
        assert maintenance.write_metric_sample(lease, now=now) == {
            "sample_written": True
        }
    finally:
        lease.close()
    with sqlite3.connect(config.metrics_db) as connection:
        row = connection.execute(
            "SELECT timestamp,cpu,memory,temperature,disk_used,load1,swap,queue_depth "
            "FROM system_metrics"
        ).fetchone()
    assert row == (int(generated), 12.5, 25.0, 51.2, 35.0, 0.1, 25.0, 3.0)


def test_preview_tasks_count_without_changing_saved_content(tmp_path):
    config = _config(tmp_path)
    notes_parent = config.data_root / "platform"
    incoming = config.data_root / "incoming"
    notes_parent.mkdir()
    incoming.mkdir()
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    photos = config.data_root / "photos.db"
    notes = notes_parent / "notes.db"
    with sqlite3.connect(photos) as connection:
        connection.execute("CREATE TABLE photos (id TEXT PRIMARY KEY, deleted_at TEXT)")
        connection.execute("INSERT INTO photos VALUES ('photo-1', ?)", (old,))
    with sqlite3.connect(notes) as connection:
        connection.execute("CREATE TABLE notes (id TEXT PRIMARY KEY, deleted_at TEXT)")
        connection.execute("INSERT INTO notes VALUES ('note-1', ?)", (old,))
    part = incoming / "upload.part"
    part.write_bytes(b"do-not-delete")
    old_epoch = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(part, (old_epoch, old_epoch))
    before = {path: path.read_bytes() for path in (photos, notes, part)}
    lease = maintenance.acquire_singleton(config)
    assert lease is not None
    try:
        assert maintenance.preview_photo_retention(lease)["candidate_count"] == 1
        assert maintenance.preview_note_retention(lease)["candidate_count"] == 1
        assert maintenance.preview_upload_parts(lease)["candidate_count"] == 1
    finally:
        lease.close()

    assert {path: path.read_bytes() for path in before} == before
    assert part.exists()


def test_retention_modes_reject_any_apply_value_before_writing(tmp_path):
    config = _config(tmp_path, photo_retention_mode="apply")
    with pytest.raises(maintenance.MaintenanceError, match="configuration_invalid"):
        maintenance.run_once(config)
    assert not config.lock_path.exists()
    assert not config.status_path.exists()


def test_source_contains_no_saved_content_retention_delete_statement():
    source = Path(maintenance.__file__).read_text(encoding="utf-8")
    assert "DELETE FROM photos" not in source
    assert "DELETE FROM notes" not in source
    assert "candidate.unlink" not in source
    assert "secondary_verify" not in source
    assert "photo_retention_mode == \"apply\"" not in source
    assert "note_retention_mode == \"apply\"" not in source
