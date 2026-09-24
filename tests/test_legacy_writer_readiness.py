import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "david-pi-legacy-writer-readiness"
LOADER = SourceFileLoader("legacy_writer_readiness", str(SOURCE))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


def states(*, portal="healthy", maintenance="healthy", chat=True, audiobook=True, suffix="1"):
    return {
        "family-photo-portal": {
            "container_id": "portal-id", "running": True, "paused": False,
            "restarting": False, "health": portal, "started_at": f"portal-{suffix}",
            "restart_count": 0,
        },
        "david-pi-maintenance": {
            "container_id": "maintenance-id", "running": True, "paused": False,
            "restarting": False, "health": maintenance,
            "started_at": f"maintenance-{suffix}", "restart_count": 0,
        },
        "david-pi-chat-notifier": {
            "container_id": "chat-id", "running": chat, "paused": False,
            "restarting": False, "health": None, "started_at": f"chat-{suffix}",
            "restart_count": 0,
        },
        "david-pi-audiobook-preparer": {
            "container_id": "audiobook-id", "running": audiobook, "paused": False,
            "restarting": False, "health": None,
            "started_at": f"audiobook-{suffix}", "restart_count": 0,
        },
    }


def worker(*, running=False, paused=False, restarting=False):
    return {
        "container_id": "worker-id", "running": running, "paused": paused,
        "restarting": restarting, "health": "healthy", "started_at": "worker-1",
        "restart_count": 0,
    }


def disabled_workers(**overrides):
    return {
        name: overrides.get(name)
        for name in readiness.DISABLED_WRITERS
    }


def test_legacy_profile_accepts_stable_core_with_worker_absent_or_stopped():
    for disabled in (
        disabled_workers(),
        disabled_workers(**{
            "david-pi-device-backup-worker": worker(),
            "david-pi-slideshow-worker": worker(),
        }),
    ):
        ready, fingerprint, reason = readiness.readiness_sample(states(), disabled)
        assert ready is True
        assert reason == "ready"
        assert fingerprint[-1][0] == "david-pi-slideshow-worker"


@pytest.mark.parametrize("disabled", [worker(running=True), worker(paused=True), worker(restarting=True)])
def test_legacy_profile_rejects_any_active_worker_state(disabled):
    disabled = disabled_workers(**{"david-pi-slideshow-worker": disabled})
    assert readiness.readiness_sample(states(), disabled) == (
        False, (), "david-pi-slideshow-worker:not_stopped"
    )


def test_legacy_profile_rejects_active_device_backup_worker():
    disabled = disabled_workers(**{
        "david-pi-device-backup-worker": worker(running=True)
    })
    assert readiness.readiness_sample(states(), disabled) == (
        False, (), "david-pi-device-backup-worker:not_stopped"
    )


@pytest.mark.parametrize(
    "sample,reason",
    [
        (states(portal="starting"), "family-photo-portal:health_not_healthy"),
        (states(maintenance="unhealthy"), "david-pi-maintenance:health_not_healthy"),
        (states(chat=False), "david-pi-chat-notifier:not_running"),
        (states(audiobook=False), "david-pi-audiobook-preparer:not_running"),
    ],
)
def test_legacy_profile_requires_every_old_writer(sample, reason):
    assert readiness.readiness_sample(sample, disabled_workers()) == (False, (), reason)
    missing = states()
    missing.pop("david-pi-maintenance")
    assert readiness.readiness_sample(missing, disabled_workers()) == (
        False, (), "legacy_writer_set_incomplete"
    )


def test_wait_requires_stable_container_identity_after_rollback():
    samples = iter([
        (states(suffix="1"), disabled_workers()),
        (states(suffix="2"), disabled_workers()),
        (states(suffix="2"), disabled_workers()),
        (states(suffix="2"), disabled_workers()),
    ])
    clock = Clock()
    ready, reason = readiness.wait_for_writers(
        timeout=10, interval=1, stable_samples=3,
        inspector=lambda: next(samples), monotonic=clock.monotonic, sleeper=clock.sleep,
    )
    assert (ready, reason, clock.value) == (True, "ready", 3)


def test_wait_is_bounded_and_fails_closed_on_inspection_errors():
    clock = Clock()
    ready, reason = readiness.wait_for_writers(
        timeout=3, interval=2, stable_samples=3,
        inspector=lambda: (_ for _ in ()).throw(readiness.ReadinessError("unavailable")),
        monotonic=clock.monotonic, sleeper=clock.sleep,
    )
    assert (ready, reason, clock.value) == (False, "unavailable", 3)


def _record(name, *, running=True, health=None):
    state = {
        "Running": running, "Paused": False, "Restarting": False,
        "StartedAt": "2099-01-01T00:00:00Z",
    }
    if health is not None:
        state["Health"] = {"Status": health}
    return {"Name": f"/{name}", "Id": f"{name}-id", "RestartCount": 0, "State": state}


def test_inspector_accepts_absent_worker_only_after_successful_exact_listing(monkeypatch):
    core_json = json.dumps([
        _record("family-photo-portal", health="healthy"),
        _record("david-pi-maintenance", health="healthy"),
        _record("david-pi-chat-notifier"),
        _record("david-pi-audiobook-preparer"),
    ])
    results = iter([
        SimpleNamespace(returncode=0, stdout=core_json),
        SimpleNamespace(returncode=0, stdout=""),
        SimpleNamespace(returncode=0, stdout=""),
    ])
    monkeypatch.setattr(readiness, "_run_docker", lambda _arguments: next(results))
    core, disabled = readiness.inspect_rollback_state()
    assert set(core) == set(readiness.LEGACY_WRITERS)
    assert disabled == disabled_workers()


def test_inspector_requires_each_candidate_only_worker_to_be_stopped(monkeypatch):
    core_json = json.dumps([
        _record("family-photo-portal", health="healthy"),
        _record("david-pi-maintenance", health="healthy"),
        _record("david-pi-chat-notifier"),
        _record("david-pi-audiobook-preparer"),
    ])
    backup_name = "david-pi-device-backup-worker"
    results = iter([
        SimpleNamespace(returncode=0, stdout=core_json),
        SimpleNamespace(returncode=0, stdout=f"{backup_name}\n"),
        SimpleNamespace(returncode=0, stdout=json.dumps([_record(backup_name, running=False)])),
        SimpleNamespace(returncode=0, stdout=""),
    ])
    monkeypatch.setattr(readiness, "_run_docker", lambda _arguments: next(results))
    core, disabled = readiness.inspect_rollback_state()
    ready, _fingerprint, reason = readiness.readiness_sample(core, disabled)
    assert ready is True
    assert reason == "ready"
    assert disabled[backup_name]["running"] is False


def test_inspector_rejects_ambiguous_worker_listing(monkeypatch):
    core_json = json.dumps([
        _record("family-photo-portal", health="healthy"),
        _record("david-pi-maintenance", health="healthy"),
        _record("david-pi-chat-notifier"),
        _record("david-pi-audiobook-preparer"),
    ])
    results = iter([
        SimpleNamespace(returncode=0, stdout=core_json),
        SimpleNamespace(
            returncode=0,
            stdout="david-pi-device-backup-worker\nlookalike\n",
        ),
    ])
    monkeypatch.setattr(readiness, "_run_docker", lambda _arguments: next(results))
    with pytest.raises(readiness.ReadinessError, match="ambiguous"):
        readiness.inspect_rollback_state()


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout": 0},
        {"timeout": readiness.MAX_TIMEOUT_SECONDS + 1},
        {"interval": 0},
        {"interval": readiness.MAX_INTERVAL_SECONDS + 1},
        {"stable_samples": 1},
        {"stable_samples": readiness.MAX_STABLE_SAMPLES + 1},
    ],
)
def test_configuration_is_strictly_bounded(changes):
    arguments = {"timeout": 10, "interval": 1, "stable_samples": 3, **changes}
    with pytest.raises(ValueError):
        readiness.wait_for_writers(
            inspector=lambda: (states(), disabled_workers()), **arguments
        )
