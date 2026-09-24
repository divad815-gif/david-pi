import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "david-pi-writer-readiness"
LOADER = SourceFileLoader("writer_readiness", str(SOURCE))
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


def states(
    *, portal="healthy", maintenance="healthy", slideshow="healthy",
    device_backup="healthy", chat=True, audiobook=True, suffix="1"
):
    return {
        "family-photo-portal": {
            "container_id": "portal-id", "running": True, "health": portal,
            "started_at": "portal", "restart_count": 0,
        },
        "david-pi-maintenance": {
            "container_id": "maintenance-id", "running": True, "health": maintenance,
            "started_at": "maintenance", "restart_count": 0,
        },
        "david-pi-slideshow-worker": {
            "container_id": "slideshow-id", "running": True, "health": slideshow,
            "started_at": "slideshow", "restart_count": 0,
        },
        "david-pi-device-backup-worker": {
            "container_id": "device-backup-id", "running": True,
            "health": device_backup, "started_at": "device-backup",
            "restart_count": 0,
        },
        "david-pi-chat-notifier": {
            "container_id": "chat-id", "running": chat, "health": None,
            "started_at": f"chat-{suffix}", "restart_count": 0,
        },
        "david-pi-audiobook-preparer": {
            "container_id": "audiobook-id", "running": audiobook, "health": None,
            "started_at": f"audiobook-{suffix}", "restart_count": 0,
        },
    }


@pytest.mark.parametrize(
    "sample,reason",
    [
        (states(portal="starting"), "family-photo-portal:health_not_healthy"),
        (states(maintenance="starting"), "david-pi-maintenance:health_not_healthy"),
        (states(slideshow="starting"), "david-pi-slideshow-worker:health_not_healthy"),
        (states(device_backup="starting"), "david-pi-device-backup-worker:health_not_healthy"),
        (states(chat=False), "david-pi-chat-notifier:not_running"),
        (states(audiobook=False), "david-pi-audiobook-preparer:not_running"),
    ],
)
def test_exact_health_and_running_contract(sample, reason):
    ready, _fingerprint, actual = readiness.readiness_sample(sample)
    assert ready is False
    assert actual == reason


def test_missing_or_unknown_writer_state_is_not_ready():
    missing = states()
    missing.pop("david-pi-maintenance")
    assert readiness.readiness_sample(missing) == (False, (), "writer_set_incomplete")
    unknown = states()
    unknown["david-pi-maintenance"]["health"] = None
    assert readiness.readiness_sample(unknown)[0] is False


def test_gate_has_exactly_all_known_writers():
    assert set(readiness.WRITERS) == {
        "family-photo-portal",
        "david-pi-maintenance",
        "david-pi-chat-notifier",
        "david-pi-audiobook-preparer",
        "david-pi-slideshow-worker",
        "david-pi-device-backup-worker",
    }


def test_unhealthchecked_writers_must_be_stably_running():
    samples = iter([states(suffix="1"), states(suffix="2"), states(suffix="2"), states(suffix="2")])
    clock = Clock()
    ready, reason = readiness.wait_for_writers(
        timeout=10,
        interval=1,
        stable_samples=3,
        inspector=lambda: next(samples),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert ready is True
    assert reason == "ready"
    assert clock.value == 3


def test_wait_is_bounded_and_returns_last_sanitized_reason():
    clock = Clock()
    ready, reason = readiness.wait_for_writers(
        timeout=3,
        interval=2,
        stable_samples=3,
        inspector=lambda: states(maintenance="unhealthy"),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert ready is False
    assert reason == "david-pi-maintenance:health_not_healthy"
    assert clock.value == 3


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
        readiness.wait_for_writers(inspector=states, **arguments)
