"""Private local update snapshots, separate from the live application tree.

An incremental snapshot links only to a verified, helper-owned snapshot copy.
It never links to a live library file: later uploads cannot alter recovery data.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat


class SnapshotError(ValueError):
    pass


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise SnapshotError("Update recovery storage is not a private directory")
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise SnapshotError("Update recovery storage must be owned by the local helper and private")
    return path


def records(root, instance_id):
    """Ignore unrelated/incomplete entries; refuse links in owned snapshots."""
    root = private_directory(root)
    found = []
    for path in root.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", path.name):
            continue
        private_directory(path)
        manifest_path = path / "snapshot.json"
        if manifest_path.is_symlink():
            raise SnapshotError("Update snapshot contains a symbolic link")
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        if (not isinstance(manifest, dict) or manifest.get("instance_id") != instance_id
                or manifest.get("independent") is not False or manifest.get("complete") is not True
                or not isinstance(manifest.get("files"), dict)
                or type(manifest.get("created_at")) not in (int, float)):
            continue
        found.append((path, manifest))
    return sorted(found, key=lambda item: item[1]["created_at"], reverse=True)


def copy_plan(source, previous=None):
    """Plan against a stopped library; validate old copies before sharing them."""
    source = Path(source)
    previous_path, previous_manifest = previous or (None, {})
    reusable, copied_bytes, reused_bytes = {}, 0, 0
    for path in source.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise SnapshotError("Managed storage contains links or special files; review it before creating a snapshot")
        name = "data/" + path.relative_to(source).as_posix()
        expected = previous_manifest.get("files", {}).get(name)
        candidate = previous_path / name if previous_path else None
        # SQLite needs a new backup incorporating committed WAL frames. Sidecars
        # are never shared either, even when they happen to have equal contents.
        database = path.suffix in {".db", ".sqlite", ".sqlite3"} or path.name.endswith(("-wal", "-shm"))
        if candidate and not database and isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected):
            if any(p.is_symlink() for p in [candidate, *candidate.parents]):
                raise SnapshotError("Update snapshot contains a symbolic link")
            if candidate.is_file():
                old = candidate.stat()
                if old.st_uid != os.geteuid() or (old.st_dev, old.st_ino) == (metadata.st_dev, metadata.st_ino):
                    raise SnapshotError("Update snapshot must contain helper-owned copies, never live-file links")
                if old.st_size == metadata.st_size and digest(path) == expected and digest(candidate) == expected:
                    reusable[str(path)] = candidate
                    reused_bytes += metadata.st_size
                    continue
        copied_bytes += metadata.st_size
    return reusable, copied_bytes, reused_bytes


def incomplete_records(root, job_directory):
    """Only journaled failed attempts may be offered for explicit cleanup."""
    found = []
    for path in private_directory(root).iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", path.name):
            continue
        private_directory(path)
        if (path / "snapshot.json").exists() or (path / "snapshot.json").is_symlink():
            continue
        job_path = Path(job_directory) / (path.name + ".json")
        if job_path.is_symlink():
            raise SnapshotError("Update job history contains a symbolic link")
        try:
            job = json.loads(job_path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("operation") == "update" and job.get("state") in {"failed", "interrupted"}:
            found.append((path, job))
    return found


def copy_function(reusable):
    def copy(source, destination):
        candidate = reusable.get(str(source))
        if candidate is None:
            return shutil.copy2(source, destination)
        # Both entries are recovery copies on the same configured filesystem.
        # Do not fall back to linking live data on an error.
        os.link(candidate, destination, follow_symlinks=False)
        return destination
    return copy


def prune_successful(root, instance_id, current_id, job_directory):
    """Keep the newest successful recovery point and every failed/interrupted one.

    Called only after the new release passed readiness and reopened. An unknown
    or unfinished job never grants automatic permission to remove a snapshot.
    """
    removed = []
    for path, _ in records(root, instance_id):
        if path.name == current_id:
            continue
        job_path = Path(job_directory) / (path.name + ".json")
        if job_path.is_symlink():
            raise SnapshotError("Update job history contains a symbolic link")
        try:
            job = json.loads(job_path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or job.get("operation") != "update" or job.get("state") != "complete":
            continue
        # rmtree does not follow directory symlinks, but reject them explicitly
        # so a changed recovery tree is left for local inspection.
        if any(p.is_symlink() for p in path.rglob("*")):
            raise SnapshotError("Update snapshot changed; automatic cleanup stopped")
        shutil.rmtree(path)
        removed.append(path.name)
    return removed
