#!/usr/bin/env python3
"""Publish aggregate-only Pi-hole statistics for David-Pi."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


SOURCE = Path("/srv/data/pihole/pihole-FTL.db")
OUTPUT = Path("/run/david-pi/pihole-summary.json")
BLOCKED = (1, 4, 5, 6, 7, 8, 9, 10, 11, 15, 16, 18)


def main() -> None:
    if not SOURCE.is_file():
        summary = {
            "enabled": False,
            "total": 0,
            "blocked": 0,
            "blocked_percent": 0,
            "clients": 0,
            "window": "Unavailable",
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "stale": True,
        }
        publish(summary)
        return

    since = int(time.time()) - 86400
    placeholders = ",".join("?" for _ in BLOCKED)
    with sqlite3.connect(f"file:{SOURCE}?mode=ro", uri=True, timeout=3) as database:
        total = database.execute(
            "SELECT COUNT(*) FROM queries WHERE timestamp >= ?", (since,)
        ).fetchone()[0]
        blocked = database.execute(
            f"SELECT COUNT(*) FROM queries WHERE timestamp >= ? "
            f"AND status IN ({placeholders})",
            (since, *BLOCKED),
        ).fetchone()[0]
        clients = database.execute(
            "SELECT COUNT(DISTINCT client) FROM queries WHERE timestamp >= ?", (since,)
        ).fetchone()[0]
    summary = {
        "enabled": True,
        "total": int(total),
        "blocked": int(blocked),
        "blocked_percent": round(100 * blocked / total, 1) if total else 0,
        "clients": int(clients),
        "window": "Last 24 hours",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stale": False,
    }
    publish(summary)


def publish(summary: dict[str, object]) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".pihole-", dir=OUTPUT.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(summary, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main()
