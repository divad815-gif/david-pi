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
DATABASES = (
    DATA / "photos.db", DATA / "metrics.db", DATA / "platform/notes.db",
    DATA / "platform/movies.db", DATA / "platform/recipes.db",
    DATA / "platform/files.db", DATA / "platform/platform.db",
    DATA / "platform/assistant/assistant.db",
    DATA / "platform/places.db",
    DATA / "platform/audiobooks.db",
    DATA / "platform/chat.db",
)
RETENTION_DAYS = 14


def publish(payload):
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, STATUS)


def main():
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
        for source in DATABASES:
            destination = db_dir / source.name
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as live:
                with sqlite3.connect(destination) as backup:
                    live.backup(backup)
            destination.chmod(0o600)
            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as check:
                if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise RuntimeError(f"Database verification failed: {source.name}")
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
            "last_attempt": now.isoformat(), "database_count": len(DATABASES),
            "source_archive": True, "quick_check": True, "retained_sets": retained_sets,
            "includes_media": False, "includes_documents": False,
        })
    except Exception:
        publish({"ok": False, "last_attempt": now.isoformat(), "error": "backup_failed"})
        raise


if __name__ == "__main__":
    main()
