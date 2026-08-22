import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


PLATFORM_DATA = Path(os.environ.get("DAVID_PI_PLATFORM_DATA", Path(os.environ.get("PHOTO_DATA", "/data")) / "platform"))
PLATFORM_DATA.mkdir(parents=True, exist_ok=True)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path):
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def migrate(path, migration):
    for attempt in range(10):
        try:
            with connect(path) as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                # Serialize schema inspection and ALTER TABLE work across
                # Gunicorn workers. Without this, two fresh workers can both
                # observe a missing column and race to add it.
                connection.execute("BEGIN IMMEDIATE")
                migration(connection)
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or attempt == 9:
                raise
            time.sleep(0.1 * (attempt + 1))
