"""Import-safe, aggregate-only observations for the maintenance worker.

This module deliberately has no Flask or application imports and performs no
work at import time.  It accepts the sanitized host status document produced by
``david-pi-server-status.py`` and returns only numeric operational metrics.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from datetime import datetime
from pathlib import Path


MAX_STATUS_BYTES = 1024 * 1024
MAX_STATUS_AGE_SECONDS = 900
MAX_FUTURE_SKEW_SECONDS = 5
EXPECTED_PRIVACY = {
    "contains_personal_filenames": False,
    "contains_domains": False,
    "contains_clients": False,
    "contains_secrets": False,
}
ALLOWED_SUBSYSTEMS = {
    "access_control",
    "portal",
    "external_drive",
    "storage",
    "backups",
    "temperature_power",
    "tailscale",
    "pihole",
    "background_jobs",
    "services",
    "updates",
}


class ObservationError(RuntimeError):
    """A bounded operational failure safe to include in worker status."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _read_regular_file(path: Path, maximum: int) -> bytes:
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ObservationError("observation_unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        pathname = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
            or (metadata.st_dev, metadata.st_ino)
            != (pathname.st_dev, pathname.st_ino)
        ):
            raise ObservationError("observation_invalid")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise ObservationError("observation_invalid")
        after_descriptor = os.fstat(descriptor)
        after_pathname = path.lstat()
        if (
            (after_descriptor.st_dev, after_descriptor.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or (after_pathname.st_dev, after_pathname.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or after_descriptor.st_nlink != 1
        ):
            raise ObservationError("observation_invalid")
        return payload
    except OSError as error:
        raise ObservationError("observation_invalid") from error
    finally:
        os.close(descriptor)


def _generated_timestamp(value, now: float) -> int:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise ObservationError("observation_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        timestamp = parsed.timestamp()
    except (OverflowError, TypeError, ValueError) as error:
        raise ObservationError("observation_invalid") from error
    if not math.isfinite(now) or not math.isfinite(timestamp):
        raise ObservationError("observation_invalid")
    age = now - timestamp
    if age < -MAX_FUTURE_SKEW_SECONDS:
        raise ObservationError("observation_invalid")
    if age > MAX_STATUS_AGE_SECONDS:
        raise ObservationError("observation_stale")
    return int(timestamp)


def load_server_status(path: Path, now: float | None = None) -> dict:
    """Load and validate the collector's explicitly sanitized status schema."""
    try:
        payload = json.loads(_read_regular_file(path, MAX_STATUS_BYTES).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ObservationError("observation_invalid") from error
    required = {"schema_version", "generated_at", "state", "subsystems", "databases", "privacy"}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ObservationError("observation_invalid")
    if not required.issubset(payload) or payload.get("privacy") != EXPECTED_PRIVACY:
        raise ObservationError("observation_invalid")
    subsystems = payload.get("subsystems")
    if not isinstance(subsystems, dict) or set(subsystems) - ALLOWED_SUBSYSTEMS:
        raise ObservationError("observation_invalid")
    payload["_generated_timestamp"] = _generated_timestamp(
        payload["generated_at"], time.time() if now is None else float(now)
    )
    return payload


def number(value):
    try:
        parsed = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def metric_sample(payload: dict) -> dict:
    """Return the metrics DB shape without retaining source status content."""
    cards = payload["subsystems"]
    portal = cards.get("portal", {}).get("details", {})
    storage = cards.get("storage", {}).get("details", {})
    external = storage.get("external", {})
    power = cards.get("temperature_power", {}).get("details", {})
    load = power.get("load_average")
    load_values = [number(item) for item in load[:3]] if isinstance(load, list) and len(load) >= 3 else [None, None, None]
    memory_total = number(power.get("ram_total_gb"))
    memory_available = number(power.get("ram_available_gb"))
    memory = None
    if memory_total is not None and memory_total > 0 and memory_available is not None:
        memory = round(100 * (1 - memory_available / memory_total), 1)
    sample = {
        "timestamp": int(payload["_generated_timestamp"]),
        "cpu": number(portal.get("cpu_percent")),
        "memory": memory,
        "temperature": number(power.get("temperature_c")),
        "disk_used": number(external.get("used_percent")),
        "load1": load_values[0],
    }
    required = ("cpu", "memory", "disk_used", "load1")
    if any(sample[key] is None for key in required):
        raise ObservationError("observation_incomplete")
    return sample


def history_values(payload: dict) -> dict:
    """Return optional aggregate status-history values only."""
    try:
        cards = payload["subsystems"]
        power = cards["temperature_power"]["details"]
        storage = cards["storage"]["details"]
        portal = cards["portal"]["details"]
        jobs = cards["background_jobs"]["details"]["slideshows"]
        backups = cards["backups"]
        swap_total = number(power.get("swap_total_gb")) or 0
        swap_free = number(power.get("swap_free_gb")) or 0
        return {
            "swap": round(100 * (swap_total - swap_free) / max(swap_total, 0.001), 1) if swap_total else 0,
            "root_disk_used": number(storage.get("microsd", {}).get("used_percent")),
            "container_memory": number(portal.get("memory_percent")),
            "health_latency": number(portal.get("health_latency_ms")),
            "backup_state": {
                "healthy": 0,
                "warning": 1,
                "critical": 2,
                "unavailable": 3,
            }.get(backups.get("state"), 3),
            "queue_depth": int(jobs.get("active", 0)) + int(jobs.get("pending", 0)),
        }
    except (KeyError, TypeError, ValueError):
        return {}
