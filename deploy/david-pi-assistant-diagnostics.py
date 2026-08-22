#!/usr/bin/env python3
"""Publish an allowlisted, privacy-safe Assistant diagnostic snapshot."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess


STATUS = Path("/run/david-pi/server-status.json")
OUTPUT = Path("/srv/data/family-photos/platform/assistant/diagnostics/latest.json")
ALLOWED_SUBSYSTEMS = (
    "portal", "external_drive", "storage", "backups", "temperature_power",
    "tailscale", "pihole", "background_jobs", "services", "updates",
)


def command(arguments, timeout=3):
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout,
            check=False, env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        return result.returncode == 0, result.stdout[:4096]
    except (OSError, subprocess.SubprocessError):
        return False, ""


def main():
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        source = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        source = {}
    subsystems = {}
    for name in ALLOWED_SUBSYSTEMS:
        item = source.get("subsystems", {}).get(name, {})
        subsystems[name] = {
            "state": item.get("state", "unavailable"),
            "summary": str(item.get("summary", ""))[:300],
            "updated_at": item.get("updated_at"),
            "evidence_code": item.get("evidence_code"),
        }
    ollama_ok, version = command(["/usr/local/bin/ollama", "--version"])
    service_ok, state = command(["/usr/bin/systemctl", "is-active", "ollama.service"])
    payload = {
        "schema_version": 1,
        "generated_at": now,
        "privacy": "aggregates_only",
        "subsystems": subsystems,
        "local_model": {
            "service_active": service_ok and state.strip() == "active",
            "runtime_available": ollama_ok,
            "version": version.strip().split()[-1][:40] if ollama_ok else None,
            "endpoint": "loopback_only",
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o640)
    os.replace(temporary, OUTPUT)


if __name__ == "__main__":
    main()
