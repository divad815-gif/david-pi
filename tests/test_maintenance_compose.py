from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service(document: str, name: str, following: str | None = None) -> str:
    start = document.index(f"  {name}:\n")
    if following is None:
        return document[start:]
    end = document.index(f"  {following}:\n", start + 1)
    return document[start:end]


def test_portal_disables_all_in_process_housekeeping():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    portal = _service(compose, "photo-portal", "chat-notifier")
    assert 'DAVID_PI_DISABLE_METRICS: "1"' in portal
    assert 'DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING: "1"' in portal
    assert (
        "DAVID_PI_HISTORY_METRICS_DB: /data/.david-pi-operations/maintenance/metrics.db"
        in portal
    )
    application = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "def record_metrics(" not in application
    assert "cleanup_stale_upload_parts" not in application
    assert "purge_expired_photos" not in application
    notes = (ROOT / "modules" / "notes.py").read_text(encoding="utf-8")
    assert "purge_expired_notes" not in notes


def test_maintenance_service_isolated_least_privilege_and_same_release_image():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    portal = _service(compose, "photo-portal", "chat-notifier")
    worker = _service(compose, "david-pi-maintenance")
    image = next(
        line.strip()
        for line in portal.splitlines()
        if line.strip().startswith("image:")
    )

    assert image in worker
    assert "build:" not in worker
    assert "ports:" not in worker
    assert "secrets:" not in worker
    assert "/run/secrets" not in worker
    assert "network_mode: none" in worker
    assert 'user: "10002:10001"' in worker
    assert "read_only: true" in worker
    assert "cap_drop:\n      - ALL" in worker
    assert "no-new-privileges:true" in worker
    assert "mem_limit: 96m" in worker
    assert "memswap_limit: 96m" in worker
    assert "pids_limit: 32" in worker
    assert "cpus: 0.20" in worker
    assert "stop_grace_period: 30s" in worker
    assert 'modules.maintenance_worker", "--check-health' in worker
    assert "/run/david-pi-maintenance:size=2m,mode=0700,uid=10002,gid=10001" in worker
    assert 'DAVID_PI_MAINTENANCE_HEARTBEAT_INTERVAL: "15"' in worker
    assert 'DAVID_PI_MAINTENANCE_STATUS_INTERVAL: "900"' in worker

    family_mount = worker.split("source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}\n", 1)[1].split(
        "      - type: bind", 1
    )[0]
    state_mount = worker.split(
        "source: ${DAVID_PI_DATA_ROOT:-/srv/david-pi/data}/.david-pi-operations/maintenance\n", 1
    )[1].split("      - type: bind", 1)[0]
    assert "target: /data" in family_mount
    assert "read_only: true" in family_mount
    assert "target: /maintenance-state" in state_mount
    assert "read_only: true" not in state_mount
    assert "DAVID_PI_MAINTENANCE_STATE: /maintenance-state" in worker
    assert (
        "DAVID_PI_MAINTENANCE_ANCHOR: /data/.david-pi-operations/maintenance" in worker
    )
    assert "DAVID_PI_MAINTENANCE_LOCK" not in worker
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "adduser -S -D -H -u 10002 -G davidpi" in dockerfile


def test_saved_content_tasks_are_explicitly_off_and_secondary_copy_is_absent():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    worker = _service(compose, "david-pi-maintenance", "device-backup-worker")
    assert 'DAVID_PI_MAINTENANCE_PHOTO_RETENTION_MODE: "off"' in worker
    assert 'DAVID_PI_MAINTENANCE_NOTE_RETENTION_MODE: "off"' in worker
    assert 'DAVID_PI_MAINTENANCE_UPLOAD_PARTS_MODE: "off"' in worker
    assert "DAVID_PI_SECONDARY_BACKUP_ROOT" not in worker
    assert "DAVID_PI_SECONDARY_DATA_ID" not in worker


def test_device_backup_worker_requires_an_explicit_independent_secondary_bind():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    worker = _service(compose, "device-backup-worker", "slideshow-worker")
    assert "network_mode: none" in worker
    assert "create_host_path: false" in worker
    assert "DAVID_PI_SECONDARY_BACKUP_HOST_ROOT" in worker
    assert "target: /secondary" in worker
    assert "DAVID_PI_SECONDARY_BACKUP_ROOT" in worker
    guide = (ROOT / "deploy" / "DEVICE_BACKUP_WORKER.md").read_text(
        encoding="utf-8"
    )
    for setting in (
        "DAVID_PI_SECONDARY_BACKUP_HOST_ROOT",
        "DAVID_PI_SECONDARY_BACKUP_ROOT=/secondary",
        "DAVID_PI_SECONDARY_DATA_ID",
    ):
        assert setting in guide
    assert "different device from primary storage" in guide


def test_maintenance_entrypoint_has_a_dedicated_storage_guard():
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert 'DAVID_PI_WORKER_MODE:-}" = "maintenance"' in entrypoint
    branch = entrypoint.split('DAVID_PI_WORKER_MODE:-}" = "maintenance"', 1)[1]
    assert '[ -L "$sentinel" ]' in branch
    assert '[ ! -w "$maintenance_state" ]' in branch
    assert '[ -L "$maintenance_anchor" ]' in branch
    assert '[ ! -w "$maintenance_runtime" ]' in branch
    assert 'if [ "$actual" != "$expected" ]' in branch


def test_sanitized_status_collector_monitors_the_worker_healthcheck():
    collector = (ROOT / "deploy" / "david-pi-server-status.py").read_text(
        encoding="utf-8"
    )
    assert '"david-pi-maintenance",' in collector


def test_service_startup_and_backup_recovery_require_maintenance_health():
    unit = (ROOT / "deploy" / "david-pi-portal.service").read_text(encoding="utf-8")
    assert "RuntimeDirectory=david-pi-writer-transition" in unit
    assert "RuntimeDirectoryMode=0700" in unit
    assert "RuntimeDirectoryPreserve=yes" in unit
    assert "ExecStart=/usr/local/sbin/david-pi-portal-lifecycle start" in unit
    assert "ExecStop=/usr/local/sbin/david-pi-portal-lifecycle stop" in unit
    assert "TimeoutStartSec=180" in unit
    assert "install -d" not in unit
    assert "/bin/sh -c" not in unit
    lifecycle = (ROOT / "deploy" / "david-pi-portal-lifecycle").read_text(
        encoding="utf-8"
    )
    lock = "acquire_writer_transition_lock"
    chat_secret_preflight = "validate_chat_secret_metadata"
    stop = '"$DOCKER_BIN" compose stop --timeout 45'
    prepare = '"$PREPARE_MAINTENANCE_STATE"'
    start = '"$DOCKER_BIN" compose up -d --no-build'
    readiness = '"$WRITER_READINESS" --timeout 90 --interval 2 --stable-samples 3'
    for contract in (lock, chat_secret_preflight, stop, prepare, start, readiness):
        assert contract in lifecycle
    main = lifecycle.split("main() {", 1)[1]
    assert main.index(lock) < main.index('case "$1"')
    start_body = lifecycle.split("start_portal() {", 1)[1].split("}\n", 1)[0]
    assert start_body.index(chat_secret_preflight) < start_body.index(stop)
    assert start_body.index(stop) < start_body.index(prepare) < start_body.index(start)
    assert start_body.index(start) < start_body.index(readiness)
    backup = (ROOT / "deploy" / "david-pi-data-backup").read_text(encoding="utf-8")
    writer_line = next(
        line for line in backup.splitlines() if line.startswith("WRITER_CONTAINERS=")
    )
    assert "david-pi-maintenance" in writer_line
    assert "david-pi-device-backup-worker" in writer_line
    assert "david-pi-mytube-preparer" in writer_line
    assert "DAVID_PI_WRITER_READINESS" in backup
    assert '"$WRITER_READINESS" --timeout 90 --interval 2 --stable-samples 3' in backup
    assert "application writers did not pass bounded readiness" in backup
    assert "WRITER_TRANSITION_LOCK_DIRECTORY" in backup
    assert backup.index(
        "acquire_writer_transition_lock\nBACKUP_DESTINATION_VERIFIED"
    ) < backup.index('log "copying immutable and application data"')
    backup_unit = (ROOT / "deploy" / "david-pi-data-backup.service").read_text(
        encoding="utf-8"
    )
    assert "RuntimeDirectory=david-pi-writer-transition" in backup_unit
    assert "RuntimeDirectoryMode=0700" in backup_unit
    assert "RuntimeDirectoryPreserve=yes" in backup_unit


def test_saved_content_copy_does_not_promise_automatic_thirty_day_retention():
    for relative in ("templates/photos.html", "static/gallery.js", "static/notes.js"):
        copy = (ROOT / relative).read_text(encoding="utf-8").lower()
        assert "30 days" not in copy
        assert "30-day" not in copy
