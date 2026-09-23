from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _service(document: str, name: str, following: str) -> str:
    start = document.index(f"  {name}:\n")
    end = document.index(f"  {following}:\n", start + 1)
    return document[start:end]


def test_slow_startup_workers_have_pi_appropriate_health_timeouts():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    maintenance = _service(compose, "david-pi-maintenance", "device-backup-worker")
    device_backup = _service(compose, "device-backup-worker", "slideshow-worker")

    assert "timeout: 20s" in maintenance
    assert "timeout: 20s" in device_backup


def test_slideshow_waits_for_portal_migrations_before_starting():
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    slideshow = compose[compose.index("  slideshow-worker:\n") :]

    assert "depends_on:" in slideshow
    assert "photo-portal:" in slideshow
    assert "condition: service_healthy" in slideshow
