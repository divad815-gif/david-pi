#!/usr/bin/env python3
"""Create a locked daily source and SQLite recovery set."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile


SOURCE = Path("/srv/compose/photo-portal")
DATA = Path("/srv/data/family-photos")
ROOT = Path("/srv/backups/photo-portal")
STATUS = Path("/run/david-pi/backup-status.json")
AUDIOBOOK_QUEUE = DATA / ".david-pi-operations/audiobook/playback-queue.db"
LEGACY_AUDIOBOOK_QUEUE = DATA / "audiobooks/playback-queue.db"
DATABASES = (
    DATA / "photos.db", DATA / "metrics.db", DATA / "platform/notes.db",
    DATA / "platform/movies.db", DATA / "platform/recipes.db",
    DATA / "platform/files.db", DATA / "platform/platform.db",
    DATA / "platform/assistant/assistant.db",
    DATA / "platform/places.db",
    DATA / "platform/audiobooks.db",
    DATA / "platform/chat.db",
    AUDIOBOOK_QUEUE,
)
RETENTION_DAYS = 14
DATABASE_MANIFEST = "database-manifest.json"


def database_plan():
    """Return a validated source-to-backup mapping for the legacy flat layout."""
    plan = []
    destinations = {}
    for configured_source in DATABASES:
        source = configured_source
        if source == AUDIOBOOK_QUEUE and not source.exists() and LEGACY_AUDIOBOOK_QUEUE.exists():
            # The first pre-upgrade boot backup can run before the new worker
            # initializes its isolated queue. Preserve recovery coverage with
            # the untouched legacy queue until the new one exists.
            source = LEGACY_AUDIOBOOK_QUEUE
        try:
            source_relative = source.relative_to(DATA)
        except ValueError as error:
            raise RuntimeError(f"Database is outside the protected data root: {source}") from error
        backup_relative = Path("databases") / source.name
        destination_key = backup_relative.as_posix()
        if destination_key in destinations:
            other = destinations[destination_key]
            raise RuntimeError(
                f"Database backup name collision: {other.as_posix()} and "
                f"{source_relative.as_posix()} both map to {destination_key}"
            )
        destinations[destination_key] = source_relative
        plan.append((source, source_relative, backup_relative))
    return tuple(plan)


def write_database_manifest(target: Path, plan) -> Path:
    """Record relative restore paths without exposing host-absolute paths."""
    payload = {
        "schema_version": 1,
        "layout": "flat-basename-v1",
        "database_count": len(plan),
        "databases": [
            {
                "source_relative_path": source_relative.as_posix(),
                "backup_relative_path": backup_relative.as_posix(),
            }
            for _source, source_relative, backup_relative in plan
        ],
    }
    destination = target / DATABASE_MANIFEST
    temporary = target / f".{DATABASE_MANIFEST}.tmp"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, destination)
    return destination


def publish(payload):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, STATUS)


def main():
    plan = database_plan()
    now = dt.datetime.now(dt.timezone.utc)
    ROOT.mkdir(parents=True, exist_ok=True)
    ROOT.chmod(0o700)
    target = ROOT / f"{now.strftime('%Y%m%d-%H%M%S')}-daily"
    target.mkdir(mode=0o700)
    try:
        archive = target / "source-with-secrets.tgz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(SOURCE, arcname="photo-portal", recursive=True)
        archive.chmod(0o600)
        db_dir = target / "databases"
        db_dir.mkdir(mode=0o700)
        for source, _source_relative, backup_relative in plan:
            destination = target / backup_relative
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live:
                with sqlite3.connect(destination) as backup:
                    live.backup(backup)
            destination.chmod(0o600)
            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as check:
                if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError(f"Database verification failed: {source.name}")
        write_database_manifest(target, plan)
        cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        for old in ROOT.glob("*-daily"):
            try:
                timestamp = dt.datetime.strptime(old.name[:15], "%Y%m%d-%H%M%S").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            if timestamp < cutoff:
                shutil.rmtree(old)
        retained_sets = len([path for path in ROOT.glob("*-daily") if path.is_dir()])
        publish({
            "ok": True, "last_success": now.isoformat(), "set": target.name,
            "last_attempt": now.isoformat(), "database_count": len(plan),
            "source_archive": True, "quick_check": True, "retained_sets": retained_sets,
            "includes_media": False, "includes_documents": False,
        })
    except Exception:
        publish({"ok": False, "last_attempt": now.isoformat(), "error": "backup_failed"})
        raise


if __name__ == "__main__":
    main()
