"""Single-concurrency preparation worker for rebuildable MyTube derivatives."""
from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .mytube import DB_PATH, POSTERS, STREAMING, _source_path, ensure_storage, initialize_mytube
from .platform import connect, migrate, utcnow


LOCK_PATH = Path(os.environ.get("DAVID_PI_MYTUBE_WORKER_LOCK", DB_PATH.parent / "mytube-prepare.lock"))
MAX_TEMP_C = float(os.environ.get("DAVID_PI_MYTUBE_PREPARE_MAX_TEMP", "75"))
PAUSE_TEMP_C = float(os.environ.get("DAVID_PI_MYTUBE_PREPARE_PAUSE_TEMP", "72"))
MAX_LOAD = float(os.environ.get("DAVID_PI_MYTUBE_PREPARE_MAX_LOAD", "2.5"))
MIN_AVAILABLE_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_PREPARE_MIN_AVAILABLE", str(768 * 1024**2)))
ABORT_AVAILABLE_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_PREPARE_ABORT_AVAILABLE", str(512 * 1024**2)))
MIN_STORAGE_BYTES = int(os.environ.get("DAVID_PI_MYTUBE_PREPARE_MIN_STORAGE", str(100 * 1024**3)))
COMMAND_TIMEOUT = int(os.environ.get("DAVID_PI_MYTUBE_PREPARE_TIMEOUT", str(6 * 3600)))
LEASE_SECONDS = int(os.environ.get("DAVID_PI_MYTUBE_PREPARE_LEASE", "900"))
CGROUP_ROOT = Path(os.environ.get("DAVID_PI_MYTUBE_CGROUP_ROOT", "/sys/fs/cgroup"))


@dataclass(frozen=True)
class ResourceGate:
    allowed: bool
    abort: bool
    reason: str | None
    temperature_c: float | None
    load_1m: float
    available_bytes: int
    storage_bytes: int


def _temperature() -> float | None:
    candidates = (
        Path("/host/sys/class/thermal/thermal_zone0/temp"),
        Path("/sys/class/thermal/thermal_zone0/temp"),
    )
    for path in candidates:
        try:
            return float(path.read_text().strip()) / 1000
        except (OSError, ValueError):
            continue
    return None


def _available_memory() -> int:
    for path in (Path("/host/proc/meminfo"), Path("/proc/meminfo")):
        try:
            fields = {}
            for line in path.read_text().splitlines():
                key, value = line.split(":", 1)
                fields[key] = int(value.strip().split()[0]) * 1024
            if fields.get("MemAvailable") is not None:
                return fields["MemAvailable"]
        except (OSError, ValueError, IndexError):
            continue
    return 0


def _bounded_positive_integer(path: Path) -> bool:
    try:
        value = path.read_text().strip()
        return value not in {"", "max"} and 0 < int(value) < (1 << 62)
    except (OSError, ValueError):
        return False


def resource_limits_enforced(root: Path | None = None) -> bool:
    """Require finite kernel-enforced memory and CPU limits before FFmpeg runs."""
    root = root or CGROUP_ROOT
    # Unified cgroup v2 exposes the effective limits at the container root.
    memory_max = root / "memory.max"
    cpu_max = root / "cpu.max"
    if memory_max.exists() or cpu_max.exists():
        try:
            cpu_quota, cpu_period = cpu_max.read_text().strip().split()
            cpu_limited = (
                cpu_quota != "max"
                and 0 < int(cpu_quota) < (1 << 62)
                and 0 < int(cpu_period) < (1 << 62)
            )
        except (OSError, ValueError):
            cpu_limited = False
        return _bounded_positive_integer(memory_max) and cpu_limited

    # Docker on older Raspberry Pi kernels commonly uses cgroup v1.
    memory_limit = root / "memory" / "memory.limit_in_bytes"
    cpu_quota = root / "cpu" / "cpu.cfs_quota_us"
    cpu_period = root / "cpu" / "cpu.cfs_period_us"
    return (
        _bounded_positive_integer(memory_limit)
        and _bounded_positive_integer(cpu_quota)
        and _bounded_positive_integer(cpu_period)
    )


def resource_gate() -> ResourceGate:
    ensure_storage()
    temperature = _temperature()
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = math.inf
    available = _available_memory()
    storage = shutil.disk_usage(STREAMING).free
    limits_enforced = resource_limits_enforced()
    abort = bool(
        not limits_enforced
        or (temperature is not None and temperature >= MAX_TEMP_C)
        or available < ABORT_AVAILABLE_BYTES
    )
    reason = None
    if not limits_enforced:
        reason = "limits_unenforced"
    elif abort:
        reason = "resource_abort"
    elif temperature is not None and temperature >= PAUSE_TEMP_C:
        reason = "temperature"
    elif load >= MAX_LOAD:
        reason = "load"
    elif available < MIN_AVAILABLE_BYTES:
        reason = "memory"
    elif storage < MIN_STORAGE_BYTES:
        reason = "storage"
    return ResourceGate(not reason, abort, reason, temperature, load, available, storage)


def _future(seconds=LEASE_SECONDS):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def claim_job():
    token = uuid.uuid4().hex; timestamp = utcnow()
    with connect(DB_PATH) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT j.*,v.source_kind,v.stored_name,v.media_id,v.content_type,v.sha256
            FROM mytube_prepare_jobs j JOIN mytube_videos v ON v.id=j.video_id
            WHERE v.deleted_at IS NULL AND (
              (j.state IN ('pending','paused','failed') AND j.available_at<=?) OR
              (j.state='preparing' AND j.lease_expires_at<?))
            ORDER BY j.available_at,j.updated_at LIMIT 1""",
            (timestamp, timestamp),
        ).fetchone()
        if not row:
            return None
        updated = connection.execute(
            """UPDATE mytube_prepare_jobs SET state='preparing',lease_token=?,lease_expires_at=?,
            attempts=attempts+1,updated_at=? WHERE video_id=? AND updated_at=?""",
            (token, _future(), timestamp, row["video_id"], row["updated_at"]),
        ).rowcount
        return (dict(row), token) if updated == 1 else None


def _run(command, timeout=COMMAND_TIMEOUT):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started = time.monotonic()
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=2)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() - started > timeout:
                    process.terminate()
                    raise subprocess.TimeoutExpired(command, timeout)
                temperature = _temperature(); available = _available_memory()
                if (temperature is not None and temperature >= MAX_TEMP_C) or available < ABORT_AVAILABLE_BYTES:
                    process.terminate()
                    raise RuntimeError("resource_abort")
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _probe(path: Path):
    result = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=format_name,duration:stream=index,codec_type,codec_name,width,height",
        "-of", "json", str(path),
    ], timeout=30)
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    return {
        "format": str((payload.get("format") or {}).get("format_name") or ""),
        "duration": float((payload.get("format") or {}).get("duration") or 0),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or ""),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
    }


def _poster(source: Path, video_id: str, staging: Path) -> str:
    output = staging / "poster.webp"
    _run([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", "00:00:03", "-i", str(source), "-frames:v", "1",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libwebp", "-quality", "82", str(output),
    ], timeout=180)
    final_name = f"{video_id}.webp"; os.replace(output, POSTERS / final_name)
    return final_name


def _rendition(source: Path, staging: Path, label: str, width: int, height: int, bitrate: str, maxrate: str, bufsize: str):
    playlist = staging / f"{label}.m3u8"
    _run([
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", bufsize, "-g", "180", "-keyint_min", "180",
        "-sc_threshold", "0", "-force_key_frames", "expr:gte(t,n_forced*6)",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", f"{label}_init.mp4", "-hls_segment_filename", str(staging / f"{label}_%05d.m4s"),
        str(playlist),
    ])
    return playlist


def _prepare_mp4(source: Path, staging: Path, *, copy_audio: bool):
    output = staging / "playback.mp4"
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "copy", "-c:a", "copy" if copy_audio else "aac", "-b:a", "128k", "-movflags", "+faststart", str(output),
    ]
    if copy_audio:
        index = command.index("-b:a")
        del command[index:index + 2]
    _run(command)
    checked = _probe(output)
    if "mp4" not in checked["format"] or checked["video_codec"] != "h264" or checked["audio_codec"] not in {"", "aac"}:
        raise ValueError("prepared_mp4_invalid")
    return output


def _validate_generation(staging: Path):
    expected = {"master.m3u8", "720p.m3u8", "480p.m3u8", "720p_init.mp4", "480p_init.mp4"}
    names = {item.name for item in staging.iterdir() if item.is_file() and not item.is_symlink()}
    if not expected.issubset(names) or not any(name.startswith("720p_") and name.endswith(".m4s") for name in names):
        raise ValueError("hls_generation_incomplete")
    if not any(name.startswith("480p_") and name.endswith(".m4s") for name in names):
        raise ValueError("hls_generation_incomplete")
    for playlist in (staging / "master.m3u8", staging / "720p.m3u8", staging / "480p.m3u8"):
        text = playlist.read_text()
        if not text.startswith("#EXTM3U") or ".." in text or "/" in "".join(line for line in text.splitlines() if not line.startswith("#")):
            raise ValueError("hls_playlist_invalid")
        references = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
        if not references or any(not (staging / name).is_file() for name in references):
            raise ValueError("hls_playlist_reference_missing")


def prepare(row, token):
    video_id = row["video_id"]
    # The web tier installs a resolver in its application context. Workers may
    # receive a separately reviewed resolver for Media projections.
    source = _source_path(row)
    metadata = _probe(source)
    generation_number = int(row["generation"]) + 1
    generation_name = f"g{generation_number:08d}"
    parent = STREAMING / video_id; parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{generation_name}-", dir=parent))
    poster_name = None
    try:
        poster_name = _poster(source, video_id, staging)
        is_direct = "mp4" in metadata["format"] and metadata["video_codec"] == "h264" and metadata["audio_codec"] in {"", "aac"}
        if is_direct:
            mode = "direct"; final_generation = None; prepared_name = None
        elif metadata["video_codec"] == "h264":
            _prepare_mp4(source, staging, copy_audio=metadata["audio_codec"] in {"", "aac"})
            final = parent / generation_name
            os.replace(staging, final)
            mode = "direct"; final_generation = generation_name; prepared_name = "playback.mp4"
        else:
            _rendition(source, staging, "720p", 1280, 720, "2500k", "2800k", "5000k")
            _rendition(source, staging, "480p", 854, 480, "1100k", "1300k", "2200k")
            (staging / "master.m3u8").write_text(
                "#EXTM3U\n#EXT-X-VERSION:7\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=2928000,RESOLUTION=1280x720\n720p.m3u8\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1298000,RESOLUTION=854x480\n480p.m3u8\n"
            )
            _validate_generation(staging)
            final = parent / generation_name
            os.replace(staging, final)
            mode = "hls"; final_generation = generation_name; prepared_name = None
        timestamp = utcnow()
        with connect(DB_PATH) as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """UPDATE mytube_prepare_jobs SET state='ready',generation=?,lease_token=NULL,lease_expires_at=NULL,
                error_code=NULL,updated_at=? WHERE video_id=? AND lease_token=?""",
                (generation_number, timestamp, video_id, token),
            ).rowcount
            if updated != 1:
                raise RuntimeError("prepare_lease_lost")
            connection.execute(
                """UPDATE mytube_videos SET duration_seconds=?,width=?,height=?,video_codec=?,audio_codec=?,poster_name=?,
                playback_state='ready',playback_mode=?,hls_generation=?,prepared_name=?,updated_at=? WHERE id=?""",
                (metadata["duration"], metadata["width"], metadata["height"], metadata["video_codec"],
                 metadata["audio_codec"], poster_name, mode, final_generation, prepared_name, timestamp, video_id),
            )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def defer(row, token, reason, *, failed=False):
    attempts = int(row.get("attempts") or 0) + 1
    delay = min(3600, 60 * (2 ** min(attempts, 5)))
    with connect(DB_PATH) as connection:
        connection.execute(
            """UPDATE mytube_prepare_jobs SET state=?,available_at=?,lease_token=NULL,lease_expires_at=NULL,
            error_code=?,updated_at=? WHERE video_id=? AND lease_token=?""",
            ("failed" if failed else "paused", _future(delay), str(reason)[:80], utcnow(), row["video_id"], token),
        )
        connection.execute(
            "UPDATE mytube_videos SET playback_state=?,updated_at=? WHERE id=?",
            ("failed" if failed else "pending", utcnow(), row["video_id"]),
        )


def run_once():
    ensure_storage(); migrate(DB_PATH, initialize_mytube)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(LOCK_PATH, flags, 0o600)
    try:
        details = os.fstat(descriptor); binding = os.stat(LOCK_PATH, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode) or details.st_nlink != 1 or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o077
            or (details.st_dev, details.st_ino) != (binding.st_dev, binding.st_ino)
        ):
            raise RuntimeError("worker_lock_untrusted")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "busy"
        gate = resource_gate()
        if not gate.allowed:
            return gate.reason or "paused"
        claimed = claim_job()
        if not claimed:
            return "idle"
        row, token = claimed
        try:
            prepare(row, token)
            return "ready"
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError, json.JSONDecodeError) as error:
            defer(row, token, type(error).__name__, failed=True)
            return "failed"
    finally:
        os.close(descriptor)


def main():
    while True:
        result = run_once()
        time.sleep(5 if result in {"ready", "failed"} else 30)


if __name__ == "__main__":
    main()
