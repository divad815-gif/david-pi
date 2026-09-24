import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "david_pi_backup_quiescence.py"
LOADER = SourceFileLoader("backup_quiescence", str(SOURCE))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
quiescence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quiescence)


def test_durable_journal_recovers_exact_writer_and_clears(tmp_path, monkeypatch):
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    lock = tmp_path / "lock"; lock.mkdir(mode=0o700)
    monkeypatch.setattr(quiescence, "JOURNAL", state / "quiescence.json")
    monkeypatch.setattr(quiescence, "LOCK_DIRECTORY", lock)
    container_id = "a" * 64
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        if arguments[1] == "inspect":
            return SimpleNamespace(stdout=f"{container_id} false\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(quiescence.subprocess, "run", run)
    quiescence.record("family-photo-portal", container_id)
    document = json.loads(quiescence.JOURNAL.read_text(encoding="utf-8"))
    assert document["machine_id"] == quiescence._machine_id()
    assert document["writers"] == [{"name": "family-photo-portal", "container_id": container_id}]
    quiescence.recover()
    assert not quiescence.JOURNAL.exists()
    assert any(call[1:3] == ["start", "family-photo-portal"] for call in calls)
    assert calls[-1][1:] == ["--timeout", "90", "--interval", "2", "--stable-samples", "3"]


def test_recovery_preserves_journal_when_container_identity_changes(tmp_path, monkeypatch):
    state = tmp_path / "state"; state.mkdir(mode=0o700)
    lock = tmp_path / "lock"; lock.mkdir(mode=0o700)
    monkeypatch.setattr(quiescence, "JOURNAL", state / "quiescence.json")
    monkeypatch.setattr(quiescence, "LOCK_DIRECTORY", lock)
    quiescence.record("family-photo-portal", "a" * 64)
    monkeypatch.setattr(
        quiescence.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'b' * 64} false\n"),
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        quiescence.recover()
    assert quiescence.JOURNAL.exists()


def test_systemd_wires_post_stop_and_boot_recovery():
    backup = (ROOT / "deploy" / "david-pi-data-backup.service").read_text(encoding="utf-8")
    recovery = (ROOT / "deploy" / "david-pi-writer-recovery.service").read_text(encoding="utf-8")
    assert "ExecStartPre=/usr/local/sbin/david_pi_backup_quiescence.py recover" in backup
    assert "ExecStartPre=/usr/local/sbin/david_pi_backup_quiescence.py record-running" in backup
    assert "ExecStopPost=/usr/local/sbin/david_pi_backup_quiescence.py recover" in backup
    assert "RuntimeDirectoryPreserve=yes" in backup
    assert "WantedBy=multi-user.target" in recovery
    assert "ConditionPathExists=/var/lib/david-pi-data-backup/quiescence.json" in recovery
    drop_in = (ROOT / "deploy" / "david-pi-data-backup.service.d" / "10-durable-quiescence.conf").read_text(encoding="utf-8")
    assert "record-running" in drop_in and "ExecStopPost=" in drop_in
