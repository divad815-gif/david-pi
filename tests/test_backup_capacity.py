import importlib.util
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "david_pi_backup_capacity.py"
SPEC = importlib.util.spec_from_file_location("backup_capacity", SCRIPT)
capacity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capacity)


def backup_fixture():
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    (root / capacity.BACKUP_SENTINEL).write_text(capacity.EXPECTED_SENTINEL, encoding="ascii")
    snapshots = root / "snapshots"
    first = snapshots / "20260830T040000Z"
    second = snapshots / "20260831T040000Z"
    third = snapshots / "20260901T040000Z"
    for snapshot in (first, second, third):
        (snapshot / "data").mkdir(parents=True)
    original = first / "data" / "original.bin"
    original.write_bytes(b"stable" * 1024)
    (second / "data" / "original.bin").hardlink_to(original)
    (third / "data" / "original.bin").hardlink_to(original)
    (second / "data" / "new.bin").write_bytes(b"new" * 1024)
    (third / "data" / "new.bin").hardlink_to(second / "data" / "new.bin")
    (third / "data" / "latest.bin").write_bytes(b"latest" * 1024)
    return temporary, root


def test_hardlinked_files_count_once_across_snapshots():
    temporary, root = backup_fixture()
    try:
        rows = capacity.allocated_bytes_by_snapshot(root)
        assert len(rows) == 3
        assert rows[0]["incremental_allocated_bytes"] > 0
        assert rows[1]["incremental_allocated_bytes"] > 0
        assert rows[1]["incremental_allocated_bytes"] < rows[1]["allocated_bytes"]
        assert rows[2]["incremental_allocated_bytes"] < rows[2]["allocated_bytes"]
    finally:
        temporary.cleanup()


def test_forecast_reports_critical_threshold_without_content_metadata():
    temporary, root = backup_fixture()
    try:
        report = capacity.capacity_report(
            root,
            total_bytes=1_000_000,
            free_bytes=50_000,
            now=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )
        assert report["state"] == "critical"
        assert report["forecast"]["days_to_threshold"] == 0
        assert report["snapshot_count"] == 3
        assert "path" not in str(report).lower()
        assert "original.bin" not in str(report)
    finally:
        temporary.cleanup()


def test_forecast_rejects_unmarked_roots_and_invalid_capacity():
    with tempfile.TemporaryDirectory() as directory:
        with pytest.raises(ValueError, match="sentinel"):
            capacity.capacity_report(directory, total_bytes=100, free_bytes=50)
    temporary, root = backup_fixture()
    try:
        with pytest.raises(ValueError, match="capacity"):
            capacity.capacity_report(root, total_bytes=100, free_bytes=101)
    finally:
        temporary.cleanup()
