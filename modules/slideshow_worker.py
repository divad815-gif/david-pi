"""Lease-fenced, single-purpose slideshow renderer.

The web process only validates and queues jobs.  This process is the sole
slideshow executor: it has no network, serves no requests, and publishes a
result only through the portal's durable media-intent transaction.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from types import ModuleType
from typing import Callable

from modules.secure_storage import safe_relative_parts


LOGGER = logging.getLogger(__name__)
REQUIRED_JOB_COLUMNS = frozenset(
    {
        "generation",
        "attempt_count",
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "target_photo_id",
        "target_name",
        "target_dev",
        "target_ino",
        "target_size",
        "target_sha256",
        "started_at",
        "finished_at",
        "source_snapshot_sha256",
        "publish_intent_id",
        "publish_state",
        "failure_code",
    }
)
PUBLISH_STATES = frozenset({"none", "prepared", "committed"})
SAFE_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[0-9a-f]{32}\Z")
SAFE_TARGET = re.compile(r"\.slideshow-[0-9a-f]{32}-g[1-9][0-9]*-[0-9a-f]{16}\.mp4\Z")
MAX_LEASE_SECONDS = 900
HEALTH_MAX_AGE_SECONDS = 300


class ActivationError(RuntimeError):
    """The worker cannot prove that adopting the current queue is safe."""


class JobLost(RuntimeError):
    """The lease or generation no longer belongs to this executor."""


class JobFailure(RuntimeError):
    """A sanitized, classified worker failure."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError) as error:
        raise ActivationError(f"{name} is invalid") from error
    if value < minimum or value > maximum:
        raise ActivationError(f"{name} is outside its approved range")
    return value


@dataclass(frozen=True)
class Config:
    mode: str
    worker_id: str
    lease_seconds: int
    renew_seconds: int
    poll_seconds: int
    max_attempts: int
    queue_limit: int
    min_free_bytes: int
    max_target_bytes: int
    max_job_seconds: int
    ffmpeg_seconds: int
    ffprobe_seconds: int
    terminate_seconds: int
    runtime_root: Path

    @classmethod
    def from_env(cls) -> "Config":
        mode = os.environ.get("DAVID_PI_SLIDESHOW_EXECUTOR_MODE", "").strip().lower()
        if mode != "worker":
            raise ActivationError("slideshow executor mode must be exactly worker")
        lease = _bounded_int("DAVID_PI_SLIDESHOW_LEASE_SECONDS", 300, 30, 900)
        renew = _bounded_int("DAVID_PI_SLIDESHOW_RENEW_SECONDS", 30, 5, 120)
        if renew * 2 >= lease:
            raise ActivationError("slideshow lease renewal is not safely bounded")
        max_job = _bounded_int("DAVID_PI_SLIDESHOW_MAX_RUNTIME", 14400, 60, 21600)
        ffmpeg = _bounded_int("DAVID_PI_SLIDESHOW_FFMPEG_TIMEOUT", 13800, 30, 21000)
        if ffmpeg >= max_job:
            raise ActivationError("ffmpeg timeout must be below the job runtime")
        identity = os.environ.get("DAVID_PI_SLIDESHOW_WORKER_ID", "").strip()
        if not identity:
            identity = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        if len(identity) > 160 or any(ord(character) < 32 for character in identity):
            raise ActivationError("slideshow worker identity is invalid")
        return cls(
            mode=mode,
            worker_id=identity,
            lease_seconds=lease,
            renew_seconds=renew,
            poll_seconds=_bounded_int("DAVID_PI_SLIDESHOW_POLL_SECONDS", 2, 1, 30),
            max_attempts=_bounded_int("DAVID_PI_SLIDESHOW_MAX_ATTEMPTS", 3, 1, 5),
            queue_limit=_bounded_int("DAVID_PI_SLIDESHOW_QUEUE_LIMIT", 3, 1, 20),
            min_free_bytes=_bounded_int(
                "DAVID_PI_SLIDESHOW_MIN_FREE", 1024 * 1024 * 1024,
                512 * 1024 * 1024, 1024 * 1024 * 1024 * 1024,
            ),
            max_target_bytes=_bounded_int(
                "DAVID_PI_SLIDESHOW_MAX_OUTPUT", 2 * 1024 * 1024 * 1024,
                64 * 1024 * 1024, 2 * 1024 * 1024 * 1024,
            ),
            max_job_seconds=max_job,
            ffmpeg_seconds=ffmpeg,
            ffprobe_seconds=_bounded_int("DAVID_PI_SLIDESHOW_FFPROBE_TIMEOUT", 60, 5, 300),
            terminate_seconds=_bounded_int("DAVID_PI_SLIDESHOW_TERMINATE_SECONDS", 10, 2, 30),
            runtime_root=Path(
                os.environ.get("DAVID_PI_SLIDESHOW_RUNTIME", "/run/david-pi-slideshow")
            ),
        )


def utcnow(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _snapshot_digest(parameters: dict) -> str:
    snapshot = {
        "collection_id": parameters.get("collection_id"),
        "collection_version": parameters.get("collection_version"),
        "collection_owner_id": parameters.get("collection_owner_id"),
        "collection_visibility": parameters.get("collection_visibility"),
        "collection_deleted_at": parameters.get("collection_deleted_at"),
        "source_name": parameters.get("source_name"),
        "result_visibility": parameters.get("result_visibility"),
        "media_items": parameters.get("media_items"),
        "duration_seconds": parameters.get("duration_seconds"),
        "transition": parameters.get("transition"),
        "layout": parameters.get("layout"),
        "music_id": parameters.get("music_id"),
        "loop_playback": parameters.get("loop_playback"),
    }
    return hashlib.sha256(
        json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _valid_job_contract(job: sqlite3.Row | dict) -> bool:
    try:
        generation = int(job["generation"])
        attempts = int(job["attempt_count"])
        job_id = str(job["id"])
        target_values = (
            job["target_name"], job["target_dev"], job["target_ino"],
            job["target_size"], job["target_sha256"],
        )
        target_bound = all(value is not None for value in target_values)
        target_identity_valid = not target_bound or (
            bool(SAFE_TARGET.fullmatch(str(job["target_name"])))
            and str(job["target_name"]).startswith(f".slideshow-{job_id}-g")
            and int(job["target_dev"]) >= 0
            and int(job["target_ino"]) > 0
            and int(job["target_size"]) > 0
            and bool(SAFE_DIGEST.fullmatch(str(job["target_sha256"])))
        )
        parameters = json.loads(job["parameters_json"])
        expected_sources = _expected_sources(parameters)
        snapshot_valid = hmac.compare_digest(
            _snapshot_digest(parameters), str(job["source_snapshot_sha256"] or "")
        )
        collection_valid = (
            isinstance(parameters.get("collection_id"), str)
            and bool(parameters["collection_id"])
            and 1 <= int(parameters.get("collection_version"))
            and isinstance(parameters.get("collection_owner_id"), str)
            and bool(parameters["collection_owner_id"])
            and parameters.get("collection_visibility") in {"shared", "private"}
            and parameters.get("result_visibility") in {"shared", "private"}
            and parameters.get("result_visibility") == job["visibility"]
            and parameters.get("result_visibility")
            == (
                "private"
                if parameters.get("collection_visibility") == "private"
                or any(
                    source["visibility"] == "private"
                    for source in expected_sources.values()
                )
                else "shared"
            )
            and parameters.get("collection_deleted_at") is None
            and isinstance(parameters.get("source_name"), str)
            and 1 <= len(parameters["source_name"]) <= 255
        )
        render_valid = (
            5 <= int(parameters.get("duration_seconds")) <= 1200
            and parameters.get("transition")
            in {"none", "mixed", "fade", "dissolve", "wipeleft", "slideright", "circleopen"}
            and parameters.get("layout") in {"balanced", "fill", "fit"}
            and isinstance(parameters.get("loop_playback"), bool)
            and (
                parameters.get("music_id") is None
                or isinstance(parameters.get("music_id"), str)
            )
        )
    except (JobFailure, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    if any(value is not None for value in target_values) and not target_bound:
        return False
    if not target_identity_valid:
        return False
    return (
        generation >= 1
        and attempts >= 0
        and bool(SAFE_ID.fullmatch(job_id))
        and isinstance(job["owner_id"], str)
        and bool(job["owner_id"])
        and job["visibility"] in {"shared", "private"}
        and bool(SAFE_ID.fullmatch(str(job["target_photo_id"] or "")))
        and job["publish_intent_id"] == job["target_photo_id"]
        and job["publish_state"] in PUBLISH_STATES
        and bool(SAFE_DIGEST.fullmatch(str(job["source_snapshot_sha256"] or "")))
        and snapshot_valid
        and collection_valid
        and render_valid
    )


def verify_activation(connection: sqlite3.Connection, now: datetime | None = None) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(slideshow_jobs)")}
    if not REQUIRED_JOB_COLUMNS.issubset(columns):
        raise ActivationError("slideshow queue schema is incomplete")
    current = now or datetime.now(timezone.utc)
    working = connection.execute(
        "SELECT * FROM slideshow_jobs WHERE status='working' ORDER BY created_at,id"
    ).fetchall()
    for job in working:
        expiry = _parse_time(job["lease_expires_at"])
        if (
            not _valid_job_contract(job)
            or int(job["attempt_count"]) < 1
            or not isinstance(job["lease_owner"], str)
            or not job["lease_owner"]
            or not isinstance(job["lease_token"], str)
            or not SAFE_ID.fullmatch(job["lease_token"])
            or expiry is None
            or expiry > current + timedelta(seconds=MAX_LEASE_SECONDS)
        ):
            raise ActivationError(
                "legacy working slideshow lacks an unambiguous lease; activation blocked"
            )
        # A valid future lease belongs to a still-eligible executor. An expired
        # lease is reclaimed transactionally by recover_expired_jobs.
        _ = expiry > current


def recover_expired_jobs(
    connection: sqlite3.Connection, now: datetime | None = None
) -> int:
    current = now or datetime.now(timezone.utc)
    stamp = utcnow(current)
    connection.execute("BEGIN IMMEDIATE")
    jobs = connection.execute(
        "SELECT * FROM slideshow_jobs WHERE status='working' ORDER BY created_at,id"
    ).fetchall()
    recovered = 0
    for job in jobs:
        expiry = _parse_time(job["lease_expires_at"])
        if not _valid_job_contract(job) or expiry is None:
            raise ActivationError(
                "legacy working slideshow lacks an unambiguous lease; activation blocked"
            )
        if expiry > current:
            continue
        changed = connection.execute(
            """UPDATE slideshow_jobs
               SET status='queued',progress=5,message=?,generation=generation+1,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   updated_at=?,version=version+1,failure_code='lease_expired'
               WHERE id=? AND status='working' AND generation=? AND lease_token=?""",
            (
                "Recovering an interrupted video safely…",
                stamp,
                job["id"],
                job["generation"],
                job["lease_token"],
            ),
        )
        recovered += changed.rowcount
    return recovered


def claim_next(
    connection: sqlite3.Connection,
    config: Config,
    now: datetime | None = None,
) -> dict | None:
    current = now or datetime.now(timezone.utc)
    stamp = utcnow(current)
    expiry = utcnow(current + timedelta(seconds=config.lease_seconds))
    connection.execute("BEGIN IMMEDIATE")
    while True:
        row = connection.execute(
            """SELECT * FROM slideshow_jobs WHERE status='queued'
               ORDER BY created_at,id LIMIT 1"""
        ).fetchone()
        if row is None:
            return None
        if not _valid_job_contract(row):
            connection.execute(
                """UPDATE slideshow_jobs SET status='failed',progress=0,message=?,error=?,
                       failure_code='legacy_queue_schema',finished_at=?,updated_at=?,version=version+1
                   WHERE id=? AND status='queued' AND generation=?""",
                (
                    "This queued video needs to be submitted again after the worker upgrade.",
                    "The legacy queue entry was not adopted; no media was changed.",
                    stamp,
                    stamp,
                    row["id"],
                    row["generation"],
                ),
            )
            continue
        if int(row["attempt_count"]) >= config.max_attempts:
            connection.execute(
                """UPDATE slideshow_jobs SET status='failed',progress=0,message=?,error=?,
                       failure_code='retry_exhausted',finished_at=?,updated_at=?,version=version+1
                   WHERE id=? AND status='queued' AND generation=?""",
                (
                    "The video could not be created after safe retries.",
                    "Video generation stopped safely. No source media was changed.",
                    stamp,
                    stamp,
                    row["id"],
                    row["generation"],
                ),
            )
            continue
        token = uuid.uuid4().hex
        changed = connection.execute(
            """UPDATE slideshow_jobs
               SET status='working',progress=7,message=?,attempt_count=attempt_count+1,
                   lease_owner=?,lease_token=?,lease_expires_at=?,
                   started_at=COALESCE(started_at,?),updated_at=?,failure_code=NULL,
                   version=version+1
               WHERE id=? AND status='queued' AND generation=? AND attempt_count=?""",
            (
                "Preparing the selected media…",
                config.worker_id,
                token,
                expiry,
                stamp,
                stamp,
                row["id"],
                row["generation"],
                row["attempt_count"],
            ),
        )
        if changed.rowcount != 1:
            continue
        claimed = connection.execute(
            "SELECT * FROM slideshow_jobs WHERE id=?", (row["id"],)
        ).fetchone()
        return dict(claimed)


def _owned_update(
    connection: sqlite3.Connection,
    job: dict,
    values: dict[str, object],
) -> None:
    allowed = {
        "status", "progress", "message", "error", "failure_code", "generation",
        "lease_owner", "lease_token", "lease_expires_at", "target_name", "target_dev",
        "target_ino", "target_size", "target_sha256", "finished_at", "result_photo_id",
        "publish_state", "updated_at",
    }
    if not values or not set(values).issubset(allowed):
        raise ValueError("unsafe slideshow job update")
    updated = dict(values)
    current = utcnow()
    updated.setdefault("updated_at", current)
    assignments = ",".join(f"{name}=?" for name in updated)
    changed = connection.execute(
        f"""UPDATE slideshow_jobs SET {assignments},version=version+1
            WHERE id=? AND status='working' AND generation=?
              AND lease_owner=? AND lease_token=?
              AND julianday(lease_expires_at)>julianday(?)""",
        (
            *updated.values(),
            job["id"],
            job["generation"],
            job["lease_owner"],
            job["lease_token"],
            current,
        ),
    )
    if changed.rowcount != 1:
        raise JobLost("slideshow lease changed")
    # The in-memory contract is the cleanup authority if the next operation
    # fails. Keep it synchronized immediately after every fenced DB mutation.
    job.update(updated)


def renew_lease(
    connection: sqlite3.Connection,
    job: dict,
    config: Config,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    _owned_update(
        connection,
        job,
        {"lease_expires_at": utcnow(current + timedelta(seconds=config.lease_seconds))},
    )


class _JobKeepalive:
    """Refresh one fenced job lease and its process-health receipt together."""

    def __init__(
        self,
        portal: ModuleType,
        job: dict,
        config: Config,
        stop_requested: Callable[[], bool],
        job_deadline: float,
    ) -> None:
        self.portal = portal
        self.job = job
        self.config = config
        self.stop_requested = stop_requested
        self.job_deadline = job_deadline
        self._stop = Event()
        self._failure_lock = Lock()
        self._pulse_lock = Lock()
        self._failure: Exception | None = None
        self._thread: Thread | None = None

    @staticmethod
    def _classify(error: Exception) -> Exception:
        if isinstance(error, (JobLost, JobFailure)):
            return error
        return JobFailure("worker_health_unavailable", retryable=True)

    def _operational_failure(self) -> JobFailure | None:
        if self.stop_requested():
            return JobFailure("stopped", retryable=True)
        if time.monotonic() >= self.job_deadline:
            return JobFailure("job_timeout", retryable=True)
        return None

    def _remember_failure(self, error: Exception) -> None:
        failure = self._classify(error)
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
        self._stop.set()

    def raise_if_failed(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise failure

    def check(self) -> None:
        self.raise_if_failed()
        operational = self._operational_failure()
        if operational is not None:
            raise operational

    def pulse(self) -> None:
        # Commit the fenced lease extension before advertising process health.
        # A stale executor therefore cannot make an unhealthy or stolen job look
        # ready merely by writing the runtime receipt.
        with self._pulse_lock:
            self.check()
            try:
                with self.portal.db() as connection:
                    renew_lease(connection, self.job, self.config)
                _write_health(self.config, ready=True, reason="working")
            except JobLost:
                raise
            except JobFailure:
                raise
            except (ActivationError, OSError, sqlite3.Error) as error:
                raise JobFailure(
                    "worker_health_unavailable", retryable=True
                ) from error
            self.check()

    def _run(self) -> None:
        while True:
            remaining = self.job_deadline - time.monotonic()
            if remaining <= 0:
                self._remember_failure(JobFailure("job_timeout", retryable=True))
                return
            if self._stop.wait(min(float(self.config.renew_seconds), remaining)):
                return
            try:
                self.pulse()
            except Exception as error:
                self._remember_failure(error)
                return

    def start(self) -> None:
        self.pulse()
        thread = Thread(
            target=self._run,
            name=f"slideshow-keepalive-{self.job['id'][:12]}",
            daemon=True,
        )
        self._thread = thread
        try:
            thread.start()
        except RuntimeError as error:
            self._thread = None
            raise JobFailure("worker_health_unavailable", retryable=True) from error

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None


def _expected_sources(parameters: dict) -> dict[str, dict]:
    items = parameters.get("media_items")
    if not isinstance(items, list) or not items:
        raise JobFailure("invalid_snapshot")
    expected: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            raise JobFailure("invalid_snapshot")
        try:
            media_id = str(item["id"])
            version = int(item["version"])
            owner = str(item["owner_id"])
            visibility = str(item["visibility"])
            digest = str(item["content_sha256"]).lower()
            byte_size = int(item["byte_size"])
            stored_path = str(item["stored_path"])
            preview_name = str(item["preview_name"])
            playback_value = item["playback_name"]
            playback_name = None if playback_value is None else str(playback_value)
            content_type = str(item["content_type"])
        except (KeyError, TypeError, ValueError) as error:
            raise JobFailure("invalid_snapshot") from error
        if (
            not media_id
            or len(media_id) > 200
            or any(ord(character) < 32 for character in media_id)
            or version < 1
            or not owner
            or visibility not in {"shared", "private"}
            or item.get("deleted_at") is not None
            or not SAFE_DIGEST.fullmatch(digest)
            or byte_size <= 0
            or byte_size > 2 * 1024 * 1024 * 1024
            or not content_type.startswith(("image/", "video/"))
            or media_id in expected
        ):
            raise JobFailure("invalid_snapshot")
        try:
            safe_relative_parts(stored_path)
            if len(safe_relative_parts(preview_name)) != 1:
                raise ValueError("preview name is nested")
            if playback_name is not None and len(safe_relative_parts(playback_name)) != 1:
                raise ValueError("playback name is nested")
        except (OSError, ValueError) as error:
            raise JobFailure("invalid_snapshot") from error
        expected[media_id] = {
            "version": version,
            "owner_id": owner,
            "visibility": visibility,
            "deleted_at": None,
            "content_sha256": digest,
            "byte_size": byte_size,
            "stored_path": stored_path,
            "preview_name": preview_name,
            "playback_name": playback_name,
            "content_type": content_type,
        }
    return expected


def _safe_cleanup_target(portal: ModuleType, job: dict) -> None:
    name = job.get("target_name")
    if name is None:
        return
    if not _valid_job_contract(job):
        raise JobFailure("unsafe_staging")
    expected = (int(job["target_dev"]), int(job["target_ino"]))
    metadata = portal.INCOMING_STORAGE.entry_stat(name, allow_missing=True)
    if metadata is None:
        return
    if (
        not stat.S_ISREG(metadata.st_mode)
        or portal.storage_identity(metadata) != expected
        or not portal.INCOMING_STORAGE.unlink_if_identity(name, expected)
    ):
        raise JobFailure("unsafe_staging")


def _validate_sources_in_connection(
    portal: ModuleType,
    connection: sqlite3.Connection,
    job: dict,
    parameters: dict,
    expected: dict,
) -> list[dict]:
    try:
        return portal.slideshow_source_snapshot(
            connection,
            job,
            parameters,
            expected,
            require_worker_lease=True,
        )
    except JobFailure:
        raise
    except ValueError as error:
        raise JobFailure("source_changed") from error
    except sqlite3.Error as error:
        raise JobFailure("source_check_unavailable", retryable=True) from error


def _validate_sources(
    portal: ModuleType, job: dict, parameters: dict, expected: dict
) -> list[dict]:
    with portal.db() as connection:
        return _validate_sources_in_connection(
            portal, connection, job, parameters, expected
        )


def _media_details(path: str, descriptors: tuple[int, ...], timeout: int) -> dict:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            check=False,
            pass_fds=descriptors,
        )
        details = json.loads(result.stdout) if result.returncode == 0 else {}
        duration = float(details.get("format", {}).get("duration") or 0)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise JobFailure("source_probe_failed") from error
    if duration <= 0 or duration > 24 * 60 * 60:
        raise JobFailure("source_probe_failed")
    return {
        "duration": duration,
        "has_audio": any(
            stream.get("codec_type") == "audio"
            for stream in details.get("streams", [])
            if isinstance(stream, dict)
        ),
    }


def _anonymous_file(portal: ModuleType) -> int:
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_TMPFILE", 0)
    if not getattr(os, "O_TMPFILE", 0):
        raise JobFailure("anonymous_staging_unavailable")
    try:
        with portal.INCOMING_STORAGE.directory_descriptor() as directory:
            descriptor = os.open(".", flags, 0o600, dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 0:
            os.close(descriptor)
            raise JobFailure("anonymous_staging_unavailable")
        return descriptor
    except OSError as error:
        raise JobFailure("anonymous_staging_unavailable") from error


def _terminate(process: subprocess.Popen, seconds: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=seconds)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            pass


def _run_ffmpeg(
    arguments: list[str],
    pass_fds: tuple[int, ...],
    target_descriptor: int,
    job: dict,
    config: Config,
    portal: ModuleType,
    stop_requested: Callable[[], bool],
    job_deadline: float,
    keepalive: _JobKeepalive | None = None,
) -> None:
    started = time.monotonic()
    deadline = min(started + config.ffmpeg_seconds, job_deadline)
    next_renewal = started + config.renew_seconds
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=pass_fds,
            start_new_session=True,
        )
    except OSError as error:
        raise JobFailure("render_unavailable", retryable=True) from error
    try:
        while process.poll() is None:
            now_mono = time.monotonic()
            if keepalive is not None:
                keepalive.check()
            if stop_requested():
                raise JobFailure("stopped", retryable=True)
            if now_mono >= deadline:
                code = "job_timeout" if job_deadline <= started + config.ffmpeg_seconds else "render_timeout"
                raise JobFailure(code, retryable=True)
            if os.fstat(target_descriptor).st_size > config.max_target_bytes:
                raise JobFailure("output_limit")
            if shutil.disk_usage(portal.DATA).free < config.min_free_bytes:
                raise JobFailure("low_disk")
            if keepalive is None and now_mono >= next_renewal:
                with portal.db() as connection:
                    renew_lease(connection, job, config)
                next_renewal = now_mono + config.renew_seconds
            time.sleep(min(1.0, max(0.05, deadline - now_mono)))
        if process.returncode != 0:
            raise JobFailure("render_failed", retryable=True)
    except BaseException:
        _terminate(process, config.terminate_seconds)
        raise


def _render(portal: ModuleType, job: dict, parameters: dict, expected: dict, config: Config,
            stop_requested: Callable[[], bool], job_deadline: float,
            keepalive: _JobKeepalive) -> tuple[str, tuple[int, int], int, str]:
    keepalive.check()
    ordered = _validate_sources(portal, job, parameters, expected)
    keepalive.check()
    descriptors: list[int] = []
    target_descriptor = -1
    try:
        total_seconds = float(parameters["duration_seconds"])
        transition = str(parameters["transition"])
        layout = str(parameters.get("layout", "balanced"))
        for item in ordered:
            keepalive.check()
            if stop_requested():
                raise JobFailure("stopped", retryable=True)
            if time.monotonic() >= job_deadline:
                raise JobFailure("job_timeout", retryable=True)
            item["is_video"] = str(item["content_type"]).startswith("video/")
            if item["is_video"]:
                storage = portal.PREVIEW_STORAGE if item.get("playback_name") else portal.ORIGINAL_STORAGE
                name = item["playback_name"] if item.get("playback_name") else item["stored_path"]
                descriptor, _metadata = storage.open_regular_path(
                    name,
                    expected_size=None if item.get("playback_name") else item["byte_size"],
                )
                descriptors.append(descriptor)
                item["source_path"] = f"/proc/self/fd/{descriptor}"
                details = _media_details(
                    item["source_path"], (descriptor,), config.ffprobe_seconds
                )
                item["source_duration"] = details["duration"]
                item["has_audio"] = details["has_audio"]
            else:
                descriptor, _metadata = portal.PREVIEW_STORAGE.open_regular_path(item["preview_name"])
                descriptors.append(descriptor)
                item["source_path"] = f"/proc/self/fd/{descriptor}"
                item["source_duration"] = 0
                item["has_audio"] = False
            if layout == "balanced" and not item["is_video"]:
                balanced = _anonymous_file(portal)
                portal.create_balanced_frame(
                    item["source_path"], f"/proc/self/fd/{balanced}"
                )
                os.fsync(balanced)
                if os.fstat(balanced).st_size <= 0:
                    os.close(balanced)
                    raise JobFailure("balanced_frame_failed")
                descriptors.append(balanced)
                item["source_path"] = f"/proc/self/fd/{balanced}"
            keepalive.check()
            if time.monotonic() >= job_deadline:
                raise JobFailure("job_timeout", retryable=True)

        keepalive.check()
        visual, durations, starts = portal.slideshow_filter(
            ordered, total_seconds, transition, layout
        )
        arguments = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
        for index, item in enumerate(ordered):
            if item["is_video"]:
                arguments.extend(["-i", item["source_path"]])
            else:
                arguments.extend(
                    ["-loop", "1", "-t", f"{durations[index]:.3f}", "-i", item["source_path"]]
                )
        music_input = None
        music_id = parameters.get("music_id")
        if music_id:
            track = next(
                (track for track in portal.SLIDESHOW_MUSIC if track["id"] == music_id), None
            )
            if not track:
                raise JobFailure("music_unavailable")
            music_path = portal.MUSIC_LIBRARY / track["filename"]
            try:
                music_descriptor = os.open(
                    music_path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                )
                if not stat.S_ISREG(os.fstat(music_descriptor).st_mode):
                    raise OSError("not regular")
            except OSError as error:
                raise JobFailure("music_unavailable") from error
            descriptors.append(music_descriptor)
            music_input = len(ordered)
            arguments.extend(["-stream_loop", "-1", "-i", f"/proc/self/fd/{music_descriptor}"])
        graph = ";".join(
            [*visual, *portal.slideshow_audio_filter(ordered, durations, starts, total_seconds, music_input)]
        )
        arguments.extend(
            [
                "-filter_complex", graph, "-map", "[outv]", "-map", "[outa]",
                "-t", f"{total_seconds:.3f}", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "23", "-c:a", "aac", "-b:a", "160k", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", "-metadata",
                f"comment=David-Pi slideshow {job['id']}",
            ]
        )
        # Render into an unnamed inode. A crash anywhere inside FFmpeg closes
        # and discards it, rather than leaving an untracked staging pathname.
        target_descriptor = _anonymous_file(portal)
        metadata = os.fstat(target_descriptor)
        target_identity = portal.storage_identity(metadata)
        with portal.db() as connection:
            _owned_update(
                connection,
                job,
                {"progress": 15, "message": "Building your video…"},
            )
        arguments.extend(["-f", "mp4", f"/proc/self/fd/{target_descriptor}"])
        _run_ffmpeg(
            arguments,
            tuple(sorted({*descriptors, target_descriptor})),
            target_descriptor,
            job,
            config,
            portal,
            stop_requested,
            job_deadline,
            keepalive,
        )
        keepalive.check()
        os.fsync(target_descriptor)
        rendered = os.fstat(target_descriptor)
        if (
            rendered.st_size <= 0
            or rendered.st_size > config.max_target_bytes
            or rendered.st_nlink != 0
        ):
            raise JobFailure("output_invalid")
        digest = portal.descriptor_sha256(target_descriptor)
        keepalive.check()
        target_name = portal.safe_component(
            f".slideshow-{job['id']}-g{job['generation']}-{uuid.uuid4().hex[:16]}.mp4"
        )
        # Persist the future pathname and exact anonymous inode before linking.
        # A crash before the link leaves a missing path that can be cleared;
        # a crash after it leaves an inode-verifiable cleanup record.
        with portal.db() as connection:
            _owned_update(
                connection,
                job,
                {
                    "target_name": target_name,
                    "target_dev": int(rendered.st_dev),
                    "target_ino": int(rendered.st_ino),
                    "target_size": int(rendered.st_size),
                    "target_sha256": digest,
                    "progress": 86,
                    "message": "Saving your video…",
                },
            )
        published = portal.INCOMING_STORAGE.link_descriptor(target_descriptor, target_name)
        if (
            portal.storage_identity(published) != target_identity
            or not portal.INCOMING_STORAGE.matches_descriptor(target_name, target_descriptor)
        ):
            raise JobFailure("unsafe_staging")
        keepalive.check()
        return target_name, target_identity, int(rendered.st_size), digest
    except JobFailure:
        raise
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        raise JobFailure("render_failed", retryable=True) from error
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        for descriptor in descriptors:
            os.close(descriptor)


def _intent_matches_job(intent: sqlite3.Row | dict, job: dict) -> bool:
    try:
        parameters = json.loads(job["parameters_json"])
        artifacts = json.loads(intent["artifacts_json"])
        originals = [
            artifact for artifact in artifacts
            if isinstance(artifact, dict) and artifact.get("kind") == "original"
        ]
        original = originals[0]
        target_identity = (int(job["target_dev"]), int(job["target_ino"]))
        artifact_identity = (
            int(original["source_dev"]), int(original["source_ino"])
        )
        return (
            len(originals) == 1
            and intent["id"] == job["target_photo_id"]
            and intent["id"] == job["publish_intent_id"]
            and intent["owner_id"] == job["owner_id"]
            and intent["owner_name"] == job["owner_name"]
            and intent["visibility"] == job["visibility"]
            and intent["content_type"] == "video/mp4"
            and int(intent["byte_size"]) == int(job["target_size"])
            and hmac.compare_digest(
                str(intent["content_sha256"]), str(job["target_sha256"])
            )
            and intent["ingestion_source"] == "slideshow_generated"
            and intent["source_device_id"] is None
            and int(intent["requires_live_validator"]) == 1
            and intent["collection_id"] is None
            and int(intent["loop_playback"])
            == int(bool(parameters["loop_playback"]))
            and intent["source_path"] == f"incoming/{job['target_name']}"
            and original.get("source_path") == intent["source_path"]
            and artifact_identity == target_identity
            and int(original["byte_size"]) == int(job["target_size"])
            and hmac.compare_digest(
                str(original["sha256"]), str(job["target_sha256"])
            )
            and intent["state"] in {"prepared", "committed"}
        )
    except (
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


def _read_publish_state(portal: ModuleType, job: dict) -> str:
    with portal.db() as connection:
        intent = connection.execute(
            "SELECT * FROM media_publish_intents WHERE id=?",
            (job["publish_intent_id"],),
        ).fetchone()
    if intent is None:
        if job.get("publish_state") != "none":
            raise JobFailure("publish_contract_invalid")
        return "none"
    if not _intent_matches_job(intent, job):
        raise JobFailure("publish_contract_invalid")
    state = str(intent["state"])
    if state not in PUBLISH_STATES:
        raise JobFailure("publish_state_invalid")
    return state


def _sync_publish_state(portal: ModuleType, job: dict) -> str:
    state = _read_publish_state(portal, job)
    with portal.db() as connection:
        _owned_update(connection, job, {"publish_state": state})
    return state


def _finish(portal: ModuleType, job: dict, photo_id: str) -> None:
    stamp = utcnow()
    with portal.db() as connection:
        _owned_update(
            connection,
            job,
            {
                "status": "completed",
                "progress": 100,
                "message": "Your slideshow is ready.",
                "error": None,
                "failure_code": None,
                "result_photo_id": photo_id,
                "publish_state": "committed",
                "finished_at": stamp,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
            },
        )


def _process_job(
    portal: ModuleType,
    job: dict,
    config: Config,
    stop_requested: Callable[[], bool],
    job_deadline: float,
    keepalive: _JobKeepalive,
) -> str:
    try:
        parameters = json.loads(job["parameters_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise JobFailure("invalid_snapshot") from error
    expected = _expected_sources(parameters)
    observed_digest = _snapshot_digest(parameters)
    if not hmac.compare_digest(observed_digest, job["source_snapshot_sha256"]):
        raise JobFailure("invalid_snapshot")

    def validate_for_publication(connection: sqlite3.Connection) -> list[dict]:
        keepalive.check()
        return _validate_sources_in_connection(
            portal, connection, job, parameters, expected
        )

    keepalive.check()
    state = _sync_publish_state(portal, job)
    keepalive.check()
    if state in {"prepared", "committed"}:
        try:
            if state == "prepared":
                if stop_requested():
                    raise JobFailure("stopped", retryable=True)
                _validate_sources(portal, job, parameters, expected)
                keepalive.check()
                portal._recover_media_intent(
                    job["publish_intent_id"],
                    precommit_validator=validate_for_publication,
                )
                keepalive.check()
            if not portal._media_intent_committed(job["publish_intent_id"]):
                raise JobFailure("publication_deferred", retryable=True)
            return job["publish_intent_id"]
        except JobFailure:
            raise
        except (OSError, ValueError, sqlite3.Error) as error:
            raise JobFailure("publication_deferred", retryable=True) from error

    keepalive.check()
    _validate_sources(portal, job, parameters, expected)
    keepalive.check()
    if stop_requested():
        raise JobFailure("stopped", retryable=True)
    if shutil.disk_usage(portal.DATA).free < config.min_free_bytes:
        raise JobFailure("low_disk")
    if job.get("target_name"):
        _safe_cleanup_target(portal, job)
        with portal.db() as connection:
            _owned_update(
                connection,
                job,
                {
                    "target_name": None,
                    "target_dev": None,
                    "target_ino": None,
                    "target_size": None,
                    "target_sha256": None,
                },
            )

    target_name, target_identity, _target_size, _digest = _render(
        portal, job, parameters, expected, config, stop_requested, job_deadline,
        keepalive,
    )
    keepalive.check()
    if stop_requested():
        raise JobFailure("stopped", retryable=True)
    if time.monotonic() >= job_deadline:
        raise JobFailure("job_timeout", retryable=True)
    keepalive.pulse()
    with portal.db() as connection:
        _validate_sources_in_connection(portal, connection, job, parameters, expected)
    keepalive.check()
    original_name = portal.secure_filename(
        f"Slideshow - {parameters['source_name']}.mp4"
    ) or "Slideshow.mp4"
    try:
        result = portal.canonical_ingest_media(
            staged_path=portal.INCOMING / target_name,
            original_filename=original_name,
            mime_type="video/mp4",
            owner_user_id=job["owner_id"],
            owner_name=job["owner_name"],
            visibility=job["visibility"],
            capture_timestamp=utcnow(),
            ingestion_source="slideshow_generated",
            loop_playback=bool(parameters["loop_playback"]),
            precommit_validator=validate_for_publication,
            media_id=job["target_photo_id"],
            expected_staged_identity=target_identity,
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        _sync_publish_state(portal, job)
        raise JobFailure("publication_deferred", retryable=True) from error
    keepalive.check()
    if result["id"] != job["target_photo_id"]:
        raise JobFailure("publication_identity_mismatch")
    _sync_publish_state(portal, job)
    keepalive.check()
    return result["id"]


def process_job(
    portal: ModuleType,
    job: dict,
    config: Config,
    stop_requested: Callable[[], bool] = lambda: False,
) -> None:
    job_deadline = time.monotonic() + config.max_job_seconds
    keepalive = _JobKeepalive(
        portal, job, config, stop_requested, job_deadline
    )
    try:
        keepalive.start()
        photo_id = _process_job(
            portal, job, config, stop_requested, job_deadline, keepalive
        )
    except BaseException:
        keepalive.stop()
        # A background lease loss must outrank any coincident render failure;
        # stale executors are never allowed to record or publish a result.
        keepalive.raise_if_failed()
        raise
    keepalive.stop()
    keepalive.check()
    # Stop the background writer before clearing the lease, then make one final
    # synchronous fenced pulse so completion cannot race a stale health receipt.
    keepalive.pulse()
    _finish(portal, job, photo_id)


def record_failure(portal: ModuleType, job: dict, config: Config, failure: JobFailure) -> None:
    state = job.get("publish_state") if job.get("publish_state") in PUBLISH_STATES else "none"
    cleanup_is_safe = False
    try:
        state = _read_publish_state(portal, job)
        cleanup_is_safe = state == "none"
    except JobFailure:
        failure = JobFailure("publish_contract_invalid")
    except (OSError, sqlite3.Error):
        cleanup_is_safe = False
    retry = failure.retryable and int(job["attempt_count"]) < config.max_attempts
    if cleanup_is_safe and job.get("target_name"):
        try:
            _safe_cleanup_target(portal, job)
        except JobFailure:
            failure = JobFailure("unsafe_staging")
            retry = False
    stamp = utcnow()
    values: dict[str, object] = {
        "status": "queued" if retry else "failed",
        "progress": 5 if retry else 0,
        "message": (
            "Retrying the video safely…"
            if retry
            else (
                "David-Pi needs more free space before creating this video."
                if failure.code == "low_disk"
                else "The video could not be created."
            )
        ),
        "error": (
            None
            if retry
            else "Video generation stopped safely. No source media was changed."
        ),
        "failure_code": failure.code,
        "publish_state": state,
        "lease_owner": None,
        "lease_token": None,
        "lease_expires_at": None,
        "finished_at": None if retry else stamp,
    }
    if retry:
        values["generation"] = int(job["generation"]) + 1
    if cleanup_is_safe and failure.code != "unsafe_staging":
        values.update(
            target_name=None,
            target_dev=None,
            target_ino=None,
            target_size=None,
            target_sha256=None,
        )
    try:
        with portal.db() as connection:
            _owned_update(connection, job, values)
    except JobLost:
        pass


def _write_health(config: Config, *, ready: bool, reason: str) -> None:
    root = config.runtime_root
    if root.is_symlink() or not root.is_dir():
        raise ActivationError("slideshow runtime is unavailable")
    metadata = root.stat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ActivationError("slideshow runtime permissions are unsafe")
    value = {
        "schema_version": 1,
        "worker": "david-pi-slideshow-worker",
        "ready": bool(ready),
        "reason": reason,
        "updated_at": utcnow(),
    }
    temporary = root / f".health-{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, root / "health.json")


def check_health(config: Config) -> bool:
    path = config.runtime_root / "health.json"
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
        updated = _parse_time(value.get("updated_at"))
        return (
            value.get("schema_version") == 1
            and value.get("worker") == "david-pi-slideshow-worker"
            and value.get("ready") is True
            and updated is not None
            and datetime.now(timezone.utc) - updated
            <= timedelta(seconds=HEALTH_MAX_AGE_SECONDS)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def run_forever(config: Config) -> int:
    import app as portal

    if portal.SLIDESHOW_EXECUTOR_MODE != "worker":
        raise ActivationError("portal and slideshow worker executor modes differ")
    if portal.SLIDESHOW_QUEUE_LIMIT != config.queue_limit:
        raise ActivationError("portal and slideshow worker queue limits differ")
    stopping = False

    def request_stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        with portal.db() as connection:
            verify_activation(connection)
    except sqlite3.Error as error:
        raise ActivationError("slideshow queue schema is unavailable") from error
    while not stopping:
        with portal.db() as connection:
            recover_expired_jobs(connection)
        with portal.db() as connection:
            job = claim_next(connection, config)
        _write_health(config, ready=True, reason="ready")
        if job is None:
            time.sleep(config.poll_seconds)
            continue
        try:
            process_job(portal, job, config, lambda: stopping)
        except JobLost:
            LOGGER.warning("Slideshow lease changed; stale executor stopped")
        except JobFailure as failure:
            LOGGER.warning("Slideshow job stopped safely [%s]", failure.code)
            record_failure(portal, job, config, failure)
        except BaseException:
            LOGGER.error("Slideshow job stopped after an internal worker failure")
            record_failure(portal, job, config, JobFailure("internal_failure", retryable=True))
    _write_health(config, ready=False, reason="stopping")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--check-health", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        config = Config.from_env()
        if arguments.check_health:
            return 0 if check_health(config) else 1
        return run_forever(config)
    except ActivationError as error:
        LOGGER.error("Slideshow worker activation blocked: %s", error)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
