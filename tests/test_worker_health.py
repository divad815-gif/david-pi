"""Exercise the production heartbeat commands without loading worker tasks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

from modules import worker_health


@pytest.fixture(params=["maintenance", "device-backup"])
def heartbeat(tmp_path, request):
    worker = request.param
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    now = int(time.time())
    value = {
        "schema_version": 2 if worker == "maintenance" else 1,
        "worker": "david-pi-maintenance" if worker == "maintenance" else "david-pi-device-backup-worker",
        "privacy": dict(worker_health.PRIVACY),
        "updated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        **({"state": "healthy"} if worker == "maintenance" else {"ready": True}),
    }
    path = root / ("heartbeat.json" if worker == "maintenance" else "health.json")
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return worker, root, path, value, now


def healthy(heartbeat):
    worker, root, _, _, now = heartbeat
    return worker_health.check_health(worker, root, os.geteuid(), now=now)


@pytest.mark.parametrize("age,accepted", [(-6, False), (-5, True), (0, True), (90, True), (91, False)])
def test_probe_retains_bounded_age(heartbeat, age, accepted):
    worker, root, _, _, now = heartbeat
    assert worker_health.check_health(worker, root, os.geteuid(), now=now + age) is accepted


@pytest.mark.parametrize("change", [
    {"schema_version": True}, {"schema_version": 999}, {"worker": "other-worker"},
    {"privacy": {}}, {"privacy": {**worker_health.PRIVACY, "contains_secrets": True}},
    {"privacy": {**worker_health.PRIVACY, "contains_secrets": 0}},
    {"updated_at": "2026-01-01T00:00:00"}, {"updated_at": "invalid"},
    {"updated_at": None}, {"updated_at": []}, {"state": "degraded", "ready": False},
])
def test_probe_rejects_invalid_and_unready_payloads(heartbeat, change):
    _, _, path, value, _ = heartbeat
    path.write_text(json.dumps({**value, **change}))
    assert not healthy(heartbeat)


@pytest.mark.parametrize("content", [b"[]", b"null", b'"text"', b"{", b"\xff", b"[" * 2000, b" " * 65537])
def test_probe_fails_closed_on_malformed_or_oversized_files(heartbeat, content):
    heartbeat[2].write_bytes(content)
    assert not healthy(heartbeat)


@pytest.mark.parametrize("unsafe", ["missing", "symlink", "hardlink", "directory", "fifo", "writable-file", "writable-directory", "runtime-symlink"])
def test_probe_rejects_unsafe_file_or_directory(heartbeat, unsafe):
    _, root, path, _, _ = heartbeat
    if unsafe == "writable-file":
        path.chmod(0o620)
    elif unsafe == "writable-directory":
        root.chmod(0o720)
    elif unsafe == "runtime-symlink":
        moved = root.with_name("moved")
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    else:
        saved = root / "saved"
        path.rename(saved)
        if unsafe == "symlink":
            path.symlink_to(saved)
        elif unsafe == "hardlink":
            os.link(saved, path)
        elif unsafe == "directory":
            path.mkdir()
        elif unsafe == "fifo":
            os.mkfifo(path)
    assert not healthy(heartbeat)


@pytest.mark.parametrize("replacement", ["file", "directory", "permissions"])
def test_probe_rejects_path_changes_during_read(heartbeat, monkeypatch, replacement):
    _, root, path, _, _ = heartbeat
    original_read = os.read
    def replace_after_read(descriptor, limit):
        raw = original_read(descriptor, limit)
        if replacement == "file":
            path.rename(root / "previous")
            path.write_bytes(raw)
        elif replacement == "directory":
            root.rename(root.with_name("previous"))
            root.mkdir(mode=0o700)
        else:
            path.chmod(0o666)
        return raw
    monkeypatch.setattr(worker_health.os, "read", replace_after_read)
    assert not healthy(heartbeat)


def test_probe_rejects_wrong_runtime_owner(heartbeat, monkeypatch):
    worker, root, _, _, now = heartbeat
    other_uid = os.geteuid() + 1
    monkeypatch.setattr(worker_health.os, "geteuid", lambda: other_uid)
    assert not worker_health.check_health(worker, root, other_uid, now=now)


def command_environment(heartbeat):
    worker, root, _, _, _ = heartbeat
    environment = dict(os.environ)
    if worker == "maintenance":
        environment.update(DAVID_PI_MAINTENANCE_RUNTIME=str(root), DAVID_PI_MAINTENANCE_UID=str(os.geteuid()))
        module = "modules.maintenance_worker"
    else:
        environment["DAVID_PI_DEVICE_BACKUP_RUNTIME"] = str(root)
        module = "modules.device_backup_worker"
    return module, environment


def test_actual_docker_command_accepts_health_and_rejects_stopped_worker(heartbeat):
    module, environment = command_environment(heartbeat)
    command = [sys.executable, "-m", module, "--check-health"]
    kwargs = {"env": environment, "cwd": Path(__file__).resolve().parents[1], "capture_output": True, "timeout": 10}
    assert subprocess.run(command, **kwargs).returncode == 0
    _, _, path, value, _ = heartbeat
    path.write_text(json.dumps({**value, "state": "degraded", "ready": False}))
    result = subprocess.run(command, **kwargs)
    assert result.returncode == 1 and not result.stdout and not result.stderr


def test_health_entrypoint_cannot_load_task_or_application_dependencies(heartbeat):
    module, environment = command_environment(heartbeat)
    script = """
import runpy, sys
blocked = {'argparse', 'dataclasses', 'sqlite3', 'threading', 'pathlib', 'flask', 'PIL', 'qrcode', 'cryptography', 'modules.device_backup', 'modules.maintenance_observations'}
def audit(event, args):
    if event == 'import' and (args[0] in blocked or args[0].split('.')[0] in blocked):
        raise AssertionError('Health probe loaded a task dependency: ' + args[0])
sys.addaudithook(audit)
module = sys.argv[1]
sys.argv = [module, '--check-health']
runpy.run_module(module, run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-c", script, module], env=environment,
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
