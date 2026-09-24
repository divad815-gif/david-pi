#!/usr/bin/env python3
"""Forecast independent-backup capacity from metadata without reading file content."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


BACKUP_SENTINEL = ".david-pi-backup-storage"
EXPECTED_SENTINEL = "david-pi-independent-backup-v1"
SNAPSHOT_NAME = re.compile(r"\d{8}T\d{6}Z\Z")


def snapshot_time(name: str) -> datetime:
    if not SNAPSHOT_NAME.fullmatch(name):
        raise ValueError("snapshot name is invalid")
    return datetime.strptime(name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def validate_backup_root(root: Path | str) -> Path:
    root = Path(root).resolve()
    sentinel = root / BACKUP_SENTINEL
    if not root.is_dir() or not sentinel.is_file():
        raise ValueError("backup root is missing its safety sentinel")
    if sentinel.read_text(encoding="ascii").strip() != EXPECTED_SENTINEL:
        raise ValueError("backup root safety sentinel is invalid")
    return root


def allocated_bytes_by_snapshot(root: Path | str) -> list[dict]:
    """Count blocks once per inode across chronological hard-linked snapshots."""
    root = validate_backup_root(root)
    snapshots_root = root / "snapshots"
    snapshots = sorted(
        (
            path for path in snapshots_root.iterdir()
            if not path.is_symlink() and path.is_dir() and SNAPSHOT_NAME.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    ) if snapshots_root.is_dir() else []
    seen_inodes: set[tuple[int, int]] = set()
    rows = []
    for snapshot in snapshots:
        snapshot_inodes: set[tuple[int, int]] = set()
        snapshot_bytes = 0
        incremental_bytes = 0
        file_count = 0
        for directory, directories, names in os.walk(snapshot, followlinks=False):
            directories.sort()
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
                if inode in snapshot_inodes:
                    continue
                snapshot_inodes.add(inode)
                blocks = stat.st_blocks * 512
                snapshot_bytes += blocks
                file_count += 1
                if inode not in seen_inodes:
                    seen_inodes.add(inode)
                    incremental_bytes += blocks
        rows.append({
            "snapshot_id": snapshot.name,
            "captured_at": snapshot_time(snapshot.name),
            "allocated_bytes": snapshot_bytes,
            "incremental_allocated_bytes": incremental_bytes,
            "unique_file_count": file_count,
        })
    return rows


def capacity_report(
    root: Path | str,
    *,
    threshold_fraction: float = 0.90,
    lookback_days: int = 30,
    total_bytes: int | None = None,
    free_bytes: int | None = None,
    now: datetime | None = None,
) -> dict:
    if not 0.50 <= float(threshold_fraction) <= 0.99:
        raise ValueError("capacity threshold must be between 0.50 and 0.99")
    lookback_days = min(max(int(lookback_days), 7), 180)
    root = validate_backup_root(root)
    rows = allocated_bytes_by_snapshot(root)
    if total_bytes is None or free_bytes is None:
        filesystem = os.statvfs(root)
        total_bytes = filesystem.f_blocks * filesystem.f_frsize
        free_bytes = filesystem.f_bavail * filesystem.f_frsize
    total_bytes = int(total_bytes)
    free_bytes = int(free_bytes)
    if total_bytes <= 0 or free_bytes < 0 or free_bytes > total_bytes:
        raise ValueError("filesystem capacity values are invalid")

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    window = [row for row in rows if row["captured_at"] >= cutoff]
    if len(window) < 2 and len(rows) >= 2:
        window = rows[-min(len(rows), 15):]
    daily_growth = 0
    observation_days = 0.0
    if len(window) >= 2:
        observation_days = max(
            (window[-1]["captured_at"] - window[0]["captured_at"]).total_seconds() / 86400,
            1.0,
        )
        daily_growth = math.ceil(
            sum(row["incremental_allocated_bytes"] for row in window[1:]) / observation_days
        )

    used_bytes = total_bytes - free_bytes
    threshold_bytes = math.floor(total_bytes * float(threshold_fraction))
    margin_bytes = max(0, threshold_bytes - used_bytes)
    if used_bytes >= threshold_bytes:
        days_to_threshold = 0
    elif daily_growth > 0:
        days_to_threshold = math.floor(margin_bytes / daily_growth)
    else:
        days_to_threshold = None

    if used_bytes >= threshold_bytes or days_to_threshold is not None and days_to_threshold < 30:
        state = "critical"
    elif days_to_threshold is not None and days_to_threshold < 90:
        state = "warning"
    elif len(window) < 2:
        state = "insufficient_data"
    else:
        state = "healthy"
    forecast_at = (
        (now + timedelta(days=days_to_threshold)).isoformat()
        if days_to_threshold is not None else None
    )
    return {
        "schema_version": 1,
        "state": state,
        "measured_at": now.isoformat(),
        "snapshot_count": len(rows),
        "latest_snapshot": rows[-1]["snapshot_id"] if rows else None,
        "filesystem": {
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "threshold_fraction": float(threshold_fraction),
            "threshold_bytes": threshold_bytes,
            "margin_bytes": margin_bytes,
        },
        "forecast": {
            "lookback_days": lookback_days,
            "observation_days": round(observation_days, 3),
            "daily_growth_bytes": daily_growth,
            "days_to_threshold": days_to_threshold,
            "threshold_at": forecast_at,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-root", type=Path, default=Path("/srv/backup-data"))
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument("--lookback-days", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(capacity_report(
        args.backup_root,
        threshold_fraction=args.threshold,
        lookback_days=args.lookback_days,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
