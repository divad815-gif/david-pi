#!/usr/bin/env python3
"""Collect a sanitized, aggregate-only David-Pi server health snapshot."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from david_pi_restore_receipt import (
    DEFAULT_MAX_AGE_SECONDS as RESTORE_EVIDENCE_MAX_AGE_SECONDS,
    ReceiptBindingError,
    ReceiptStaleError,
    read_restore_receipt,
    verify_restore_receipt,
)
from david_pi_snapshot_manifest import load_signing_key


OUTPUT = Path("/run/david-pi/server-status.json")
DATA = Path("/srv/data/family-photos")
BACKUPS = Path("/srv/backups/photo-portal")
SENTINEL = DATA / ".david-pi-storage"
SENTINEL_VALUE = "david-pi-family-storage-v1"
PIHOLE = Path("/run/david-pi/pihole-summary.json")
BACKUP_STATUS = Path("/run/david-pi/backup-status.json")
INDEPENDENT_BACKUP = Path("/srv/backup-data")
INDEPENDENT_BACKUP_STATUS = INDEPENDENT_BACKUP / "last-success.json"
INDEPENDENT_BACKUP_SENTINEL = INDEPENDENT_BACKUP / ".david-pi-backup-storage"
INDEPENDENT_BACKUP_SENTINEL_VALUE = "david-pi-independent-backup-v1"
B2_BACKUP_STATUS = Path("/var/lib/david-pi-b2-backup/status.json")
OFFSITE_REQUIRED = os.environ.get("DAVID_PI_OFFSITE_REQUIRED", "false").lower() == "true"
UPDATE_SUCCESS = Path("/var/lib/apt/periodic/update-stamp")
REBOOT_REQUIRED = Path("/var/run/reboot-required")
RESTORE_EVIDENCE = Path("/var/lib/david-pi-recovery/latest-restore-evidence.json")
RESTORE_EVIDENCE_KEY_ENV = "DAVID_PI_RESTORE_EVIDENCE_KEY_FILE"
ACCESS_CONTAINER = "family-photo-portal"
ACCESS_MODE_LABEL = "com.david-pi.access-mode"
ACCESS_MODES = frozenset({"off", "shadow", "enforce"})
ACCESS_COUNTER_WINDOW_HOURS = 24
ACCESS_COUNTER_LINE_LIMIT = 5000
ACCESS_COUNTER_OUTPUT_LIMIT = 1024 * 1024
ACCESS_COUNTER_PATTERN = re.compile(
    r"(?:^|\s)david_pi_access_counter "
    r"mode=(off|shadow|enforce) "
    r"disposition=(blocked|shadow|unenforced)\s*\Z"
)
AUDIOBOOK_QUEUE = DATA / ".david-pi-operations/audiobook/playback-queue.db"
LEGACY_AUDIOBOOK_QUEUE = DATA / "audiobooks/playback-queue.db"
EXPECTED_DATABASES = (
    DATA / "photos.db", DATA / "metrics.db", DATA / "platform/notes.db",
    DATA / "platform/movies.db", DATA / "platform/recipes.db",
    DATA / "platform/files.db", DATA / "platform/platform.db",
    DATA / "platform/assistant/assistant.db",
    DATA / "platform/places.db",
    DATA / "platform/audiobooks.db",
    DATA / "platform/chat.db",
    AUDIOBOOK_QUEUE,
)
SERVICES = (
    "david-pi-portal.service", "docker.service", "tailscaled.service", "ssh.service",
    "david-pi-backup.service", "david-pi-backup.timer",
    "david-pi-data-backup.service", "david-pi-data-backup.timer",
    "david-pi-pihole-summary.service", "david-pi-pihole-summary.timer",
    "david-pi-server-status.service", "david-pi-server-status.timer",
    "unattended-upgrades.service",
)
STATE_RANK = {"healthy": 0, "unavailable": 1, "warning": 2, "critical": 3}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def expected_databases() -> tuple[Path, ...]:
    """Use the isolated queue, with a non-destructive first-boot fallback."""
    if AUDIOBOOK_QUEUE.exists() or not LEGACY_AUDIOBOOK_QUEUE.exists():
        return EXPECTED_DATABASES
    return tuple(
        LEGACY_AUDIOBOOK_QUEUE if path == AUDIOBOOK_QUEUE else path
        for path in EXPECTED_DATABASES
    )


def command(
    arguments: list[str],
    timeout: int = 5,
    limit: int = 65536,
    include_stderr: bool = False,
) -> tuple[bool, str]:
    """Run one fixed command with bounded time and output."""
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout,
            check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        output = result.stdout or ""
        if include_stderr and result.stderr:
            # Docker attaches a container's stderr log stream to the command's
            # stderr.  This remains bounded before callers parse only fixed,
            # content-neutral evidence records.
            if output and not output.endswith("\n"):
                output += "\n"
            output += result.stderr
        output = output[:limit]
        return result.returncode == 0, output
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def card(state: str, summary: str, details: dict, action: str, code: str) -> dict:
    return {
        "state": state, "summary": summary, "updated_at": now_iso(),
        "details": details, "recommended_action": action, "evidence_code": code,
    }


def safe_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def age_hours(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return round((dt.datetime.now(dt.timezone.utc) - parsed).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def credential_path(configured: str, environment=os.environ) -> Path:
    """Resolve only an absolute path or one systemd credential basename."""
    path = Path(configured)
    if path.is_absolute():
        return path
    if path.name != configured or configured in {"", ".", ".."}:
        raise ValueError("credential name is invalid")
    directory = str(environment.get("CREDENTIALS_DIRECTORY", "")).strip()
    if not directory:
        raise ValueError("credential directory is unavailable")
    return Path(directory) / configured


def restore_evidence_summary(independent: dict, environment=os.environ) -> dict:
    """Verify the private receipt and return only content-neutral status facts."""
    summary = {
        "configured": False,
        "state": "unconfigured",
        "scope": "isolated_data_restore",
        "last_verified": None,
        "age_hours": None,
        "current_snapshot_match": False,
        "network_isolation_verified": False,
        "application_boot_verified": False,
        "disaster_recovery_complete": False,
    }
    configured_key = str(environment.get(RESTORE_EVIDENCE_KEY_ENV, "")).strip()
    if not configured_key:
        return summary
    summary["configured"] = True
    snapshot_id = independent.get("snapshot")
    manifest_digest = independent.get("manifest_sha256")
    if not isinstance(snapshot_id, str) or not isinstance(manifest_digest, str):
        summary["state"] = "backup_binding_unavailable"
        return summary
    try:
        key = load_signing_key(credential_path(configured_key, environment))
        document = read_restore_receipt(RESTORE_EVIDENCE)
        receipt = verify_restore_receipt(
            document,
            key,
            expected_snapshot_id=snapshot_id,
            expected_manifest_sha256=manifest_digest,
            max_age_seconds=RESTORE_EVIDENCE_MAX_AGE_SECONDS,
        )
    except ReceiptStaleError:
        summary["state"] = "stale"
        return summary
    except ReceiptBindingError:
        # An old but authentic receipt must never be replayed as proof for the
        # currently advertised signed snapshot.
        summary["state"] = "snapshot_mismatch"
        return summary
    except (OSError, RuntimeError, ValueError):
        summary["state"] = "invalid_or_unavailable"
        return summary
    summary.update(
        {
            "state": "isolated_data_verified",
            "last_verified": receipt["issued_at"],
            "age_hours": age_hours(receipt["issued_at"]),
            "current_snapshot_match": True,
            "network_isolation_verified": True,
            # Schema v1 explicitly cannot assert either of these stronger
            # outcomes.  Keep them visible so data verification cannot become
            # a false-green disaster-recovery result.
            "application_boot_verified": False,
            "disaster_recovery_complete": False,
        }
    )
    return summary


def file_mtime(path: Path) -> str | None:
    """Return a file timestamp without reading its potentially sensitive contents."""
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
    except OSError:
        return None


def disk_details(path: Path) -> dict:
    usage = shutil.disk_usage(path)
    return {
        "total_gb": round(usage.total / 1073741824, 1),
        "used_gb": round(usage.used / 1073741824, 1),
        "free_gb": round(usage.free / 1073741824, 1),
        # Match df: reserved filesystem blocks are not available to applications.
        "used_percent": round(100 * usage.used / max(usage.used + usage.free, 1), 1),
    }


def directory_size(path: Path) -> int:
    total = 0
    try:
        for root, directories, files in os.walk(path):
            directories[:] = [name for name in directories if not (Path(root) / name).is_symlink()]
            for name in files:
                candidate = Path(root) / name
                try:
                    if not candidate.is_symlink():
                        total += candidate.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def size_gb(path: Path) -> float:
    return round(directory_size(path) / 1073741824, 3)


def host_uptime_seconds(path: Path = Path("/proc/uptime")) -> int | None:
    """Read host uptime without exposing process or user details."""
    try:
        value = float(path.read_text(encoding="utf-8").split()[0])
        return int(value) if value >= 0 else None
    except (OSError, IndexError, TypeError, ValueError):
        return None


def portal_card() -> dict:
    ok, raw = command([
        "docker", "inspect", "family-photo-portal", "--format",
        "{{json .State}}|{{.Config.Image}}|{{.Config.User}}|{{.RestartCount}}|{{.HostConfig.Memory}}|{{.HostConfig.PidsLimit}}",
    ])
    if not ok or "|" not in raw:
        return card("critical", "The portal container is not available.", {}, "Check the portal service.", "PORTAL_CONTAINER_MISSING")
    state_json, image, user, restarts, memory_limit, pids_limit = raw.strip().split("|", 5)
    try:
        state = json.loads(state_json)
    except json.JSONDecodeError:
        state = {}
    health = (state.get("Health") or {}).get("Status", state.get("Status", "unknown"))
    started = state.get("StartedAt")
    uptime = None
    if started:
        try:
            uptime = max(0, int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()))
        except ValueError:
            pass
    health_ok, latency_raw = command([
        "curl", "-fsS", "-o", "/dev/null", "-w", "%{time_total}", "--max-time", "4",
        "http://127.0.0.1:8090/health",
    ])
    latency_ms = round(float(latency_raw or 0) * 1000, 1) if health_ok else None
    stats_ok, stats_raw = command([
        "docker", "stats", "--no-stream", "--format", "{{json .}}", "family-photo-portal",
    ])
    stats = {}
    if stats_ok:
        try:
            stats = json.loads(stats_raw)
        except json.JSONDecodeError:
            pass
    root_user = user in ("", "0", "0:0", "root")
    sentinel_ok = SENTINEL.is_file() and SENTINEL.read_text(encoding="utf-8").strip() == SENTINEL_VALUE
    try:
        restart_count = int(restarts or 0)
        memory_limit_bytes = int(memory_limit or 0)
        pid_limit = int(pids_limit or 0)
    except ValueError:
        restart_count = 0
        memory_limit_bytes = 0
        pid_limit = 0
    resource_limits_enforced = memory_limit_bytes > 0 and pid_limit > 0
    telemetry_available = bool(stats_ok and stats)
    status = "healthy"
    code = "PORTAL_HEALTHY"
    summary = "The household portal is healthy."
    if health != "healthy" or root_user or not sentinel_ok:
        status, code, summary = "critical", "PORTAL_INVARIANT_FAILED", "The portal failed a safety or health check."
    elif restart_count >= 3:
        status, code, summary = "warning", "PORTAL_RESTARTS", "The portal is running but has restarted repeatedly."
    elif not resource_limits_enforced:
        status, code, summary = "warning", "PORTAL_LIMITS_UNENFORCED", "The portal is healthy, but resource limits are not confirmed."
    elif not telemetry_available:
        status, code, summary = "warning", "PORTAL_TELEMETRY_UNAVAILABLE", "The portal is healthy, but live resource readings are unavailable."
    return card(status, summary, {
        "container_health": health, "image": image, "uptime_seconds": uptime,
        "restart_count": restart_count, "health_latency_ms": latency_ms,
        "memory_usage": stats.get("MemUsage", "Unavailable"),
        "memory_percent": stats.get("MemPerc", "Unavailable"),
        "cpu_percent": stats.get("CPUPerc", "Unavailable"),
        "pid_usage": stats.get("PIDs", "Unavailable"),
        "memory_limit_bytes": memory_limit_bytes, "pid_limit": pid_limit,
        "resource_limits_enforced": resource_limits_enforced,
        "resource_telemetry_available": telemetry_available,
        "expected_uid": "10001", "running_user": user, "storage_sentinel": sentinel_ok,
    }, "" if status == "healthy" else "Open the details and check the failed invariant.", code)


def drive_card() -> dict:
    ok, raw = command(["findmnt", "-J", "-T", "/srv/data", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS,UUID"])
    record = {}
    if ok:
        try:
            filesystems = json.loads(raw).get("filesystems", [])
            record = filesystems[0] if filesystems else {}
        except json.JSONDecodeError:
            pass
    mounted = record.get("target") == "/srv/data"
    filesystem = record.get("fstype")
    options = str(record.get("options", ""))
    writable = "rw" in options.split(",")
    try:
        for line in Path("/proc/1/mountinfo").read_text(encoding="utf-8").splitlines():
            before, _separator, after = line.partition(" - ")
            fields = before.split()
            if len(fields) > 5 and fields[4] == "/srv/data":
                writable = "rw" in fields[5].split(",") and " rw" in f" {after}"
                break
    except OSError:
        pass
    sentinel_ok = SENTINEL.is_file() and SENTINEL.read_text(encoding="utf-8").strip() == SENTINEL_VALUE
    usb_ok, usb = command(["lsusb", "-t"])
    uas = "Driver=uas" in usb
    usb3 = any(speed in usb for speed in ("5000M", "10000M", "20000M"))
    kernel_ok, kernel = command(["journalctl", "-k", "--since", "24 hours ago", "--no-pager"], timeout=8, limit=262144)
    lowered = kernel.lower() if kernel_ok else ""
    errors = sum(lowered.count(term) for term in ("i/o error", "buffer i/o", "ext4-fs error"))
    resets = lowered.count("reset superspeed usb") + lowered.count("usb disconnect")
    status, code, summary = "healthy", "DRIVE_HEALTHY", "The external data drive is mounted correctly."
    if not mounted or filesystem != "ext4" or not writable or not sentinel_ok or errors:
        status, code, summary = "critical", "DRIVE_INVARIANT_FAILED", "The external data drive needs attention."
    elif resets:
        status, code, summary = "warning", "DRIVE_USB_WARNING", "The drive is mounted, with a USB warning to review."
    elif usb3 and not uas:
        summary = "The external data drive is healthy over USB 3; UAS and SMART are unavailable through this adapter."
    return card(status, summary, {
        "mounted": mounted, "source": record.get("source", "Unavailable"),
        "uuid": record.get("uuid", "Unavailable"), "filesystem": filesystem or "Unavailable",
        "read_write": writable, "sentinel_valid": sentinel_ok, "usb3": usb3, "usb3_uas": uas,
        "recent_usb_resets": resets, "recent_io_or_filesystem_errors": errors,
        "smart": "unavailable",
    }, "" if status == "healthy" else "Check the drive connection and system log.", code)


def storage_card() -> dict:
    external = disk_details(Path("/srv/data"))
    microsd = disk_details(Path("/"))
    categories = {
        "originals_gb": size_gb(DATA / "originals"),
        "generated_videos_gb": size_gb(DATA / "previews"),
        "documents_gb": size_gb(DATA / "files" / "objects"),
        "previews_thumbnails_gb": round(size_gb(DATA / "previews") + size_gb(DATA / "thumbs"), 3),
        "pdf_cache_gb": size_gb(DATA / "files" / "cache"),
        "trash_gb": size_gb(DATA / "quarantine"),
        "upload_spool_gb": round(size_gb(DATA / "incoming") + size_gb(DATA / "tmp" / "uploads"), 3),
        "databases_gb": round(sum(path.stat().st_size for path in expected_databases() if path.is_file()) / 1073741824, 3),
        "backups_gb": size_gb(BACKUPS),
    }
    stale_parts = 0
    cutoff = time.time() - 86400
    for root in (DATA / "incoming", DATA / "tmp" / "uploads", DATA / "files" / "incoming"):
        if root.is_dir():
            stale_parts += sum(1 for item in root.glob("*.part") if item.is_file() and item.stat().st_mtime < cutoff)
    used = max(external["used_percent"], microsd["used_percent"])
    status = "critical" if used >= 90 or external["free_gb"] < 5 else "warning" if used >= 80 or stale_parts else "healthy"
    code = {"healthy": "STORAGE_HEALTHY", "warning": "STORAGE_WARNING", "critical": "STORAGE_CRITICAL"}[status]
    growth = {"24h_percentage_points": None, "7d_percentage_points": None}
    metrics_db = DATA / "metrics.db"
    if metrics_db.is_file():
        try:
            with sqlite3.connect(f"file:{metrics_db}?mode=ro", uri=True, timeout=1) as connection:
                for label, seconds in (("24h_percentage_points", 86400), ("7d_percentage_points", 604800)):
                    row = connection.execute(
                        "SELECT disk_used FROM system_metrics "
                        "WHERE recorded_at <= datetime('now', ?) AND disk_used IS NOT NULL "
                        "ORDER BY recorded_at DESC LIMIT 1",
                        (f"-{seconds} seconds",),
                    ).fetchone()
                    if row and row[0] is not None:
                        growth[label] = round(external["used_percent"] - float(row[0]), 2)
        except (sqlite3.Error, OSError):
            pass
    return card(status, "Storage has comfortable free space." if status == "healthy" else "Storage needs attention.", {
        "external": external, "microsd": microsd, "categories": categories,
        "stale_part_files": stale_parts, "upload_render_reserve_gb": 1,
        "growth": growth, "low_space_rejections": None,
    }, "" if status == "healthy" else "Review free space and stale temporary data.", code)


def backups_card() -> dict:
    info = safe_json(BACKUP_STATUS)
    last = info.get("last_success") or info.get("completed_at")
    age = age_hours(last)
    ok = bool(info.get("ok") or info.get("state") == "healthy") and age is not None
    independent = safe_json(INDEPENDENT_BACKUP_STATUS)
    restore_evidence = restore_evidence_summary(independent)
    independent_last = independent.get("completed_at")
    independent_age = age_hours(independent_last)
    mounted, _ = command(["mountpoint", "-q", str(INDEPENDENT_BACKUP)])
    source_uuid_ok, source_uuid = command(["findmnt", "-no", "UUID", "-T", str(DATA)])
    backup_uuid_ok, backup_uuid = command(
        ["findmnt", "-no", "UUID", "-T", str(INDEPENDENT_BACKUP)]
    )
    try:
        sentinel_valid = (
            INDEPENDENT_BACKUP_SENTINEL.read_text(encoding="utf-8").strip()
            == INDEPENDENT_BACKUP_SENTINEL_VALUE
        )
    except OSError:
        sentinel_valid = False
    physically_independent = bool(
        source_uuid_ok
        and backup_uuid_ok
        and source_uuid.strip()
        and backup_uuid.strip()
        and source_uuid.strip() != backup_uuid.strip()
    )
    independent_ok = bool(
        mounted
        and sentinel_valid
        and physically_independent
        and independent.get("state") == "healthy"
        and independent_age is not None
    )
    independent_configured = bool(mounted and sentinel_valid)
    try:
        capacity = disk_details(INDEPENDENT_BACKUP) if mounted else None
    except OSError:
        capacity = None
    offsite = safe_json(B2_BACKUP_STATUS)
    offsite_last = offsite.get("completed_at")
    offsite_age = age_hours(offsite_last)
    offsite_source_confirmed = bool(
        offsite.get("state") in {"healthy", "uploaded_pending_restore"}
        and offsite.get("source_snapshot_confirmed") is True
        and offsite.get("repository_sample_checked") is True
    )
    offsite_restore_verified = offsite.get("offsite_restore_verified") is True
    object_lock_verified = offsite.get("object_lock_verified") is True
    if not ok or (age is not None and age >= 72):
        state, code = "critical", "BACKUP_FAILED_OR_OLD"
    elif not independent_configured:
        state, code = "warning", "DATA_BACKUP_NOT_CONFIGURED"
    elif not independent_ok or (independent_age is not None and independent_age >= 72):
        state, code = "critical", "DATA_BACKUP_FAILED_OR_OLD"
    elif age >= 30 or independent_age >= 30:
        state, code = "warning", "BACKUP_STALE"
    elif capacity and (capacity["used_percent"] >= 90 or capacity["free_gb"] < 5):
        state, code = "critical", "BACKUP_CAPACITY_CRITICAL"
    elif capacity and capacity["used_percent"] >= 80:
        state, code = "warning", "BACKUP_CAPACITY_LOW"
    elif OFFSITE_REQUIRED and not offsite:
        state, code = "warning", "OFFSITE_BACKUP_NOT_CONFIGURED"
    elif OFFSITE_REQUIRED and offsite.get("state") == "failed":
        state, code = "warning", "OFFSITE_BACKUP_FAILED"
    elif OFFSITE_REQUIRED and (offsite_age is None or offsite_age >= 72 or not offsite_source_confirmed):
        state, code = "warning", "OFFSITE_BACKUP_UNVERIFIED"
    elif OFFSITE_REQUIRED and (not offsite_restore_verified or not object_lock_verified):
        state, code = "warning", "OFFSITE_RESTORE_PENDING"
    elif restore_evidence["state"] != "isolated_data_verified":
        state, code = "warning", "INDEPENDENT_RESTORE_EVIDENCE_PENDING"
    elif not restore_evidence["application_boot_verified"]:
        state, code = "warning", "RESTORE_APPLICATION_BOOT_PENDING"
    else:
        state, code = "healthy", "BACKUPS_HEALTHY"
    retained = len([path for path in BACKUPS.glob("*-daily") if path.is_dir()]) if BACKUPS.is_dir() else 0
    summary = (
        "Required backup copies are current and restore-verified."
        if state == "healthy"
        else "Backup copies exist, but one or more recovery checks still need attention."
    )
    return card(state, summary, {
        "last_attempt": info.get("last_attempt") or info.get("started_at") or last, "last_success": last,
        "age_hours": age, "last_failure": info.get("error"),
        "expected_databases": len(expected_databases()),
        "databases_included": info.get("database_count", 0),
        "source_archive_included": info.get("source_archive") is True,
        "signed_manifest_available": bool(info.get("manifest_sha256")),
        "quick_check": "passed" if info.get("quick_check") is True else "unavailable",
        "retained_sets": retained, "timer": unit_state("david-pi-backup.timer"),
        "independent_data_backup": {
            "capacity": capacity,
            "writers_quiesced": independent.get("writers_quiesced") is True,
            "configured": independent_configured,
            "originals_included": independent_ok,
            "documents_included": independent_ok,
            "physically_independent": physically_independent,
            "last_success": independent_last,
            "age_hours": independent_age,
            "snapshot": independent.get("snapshot"),
            "databases_included": independent.get("database_count", 0),
            "timer": unit_state("david-pi-data-backup.timer"),
            "last_restoration_test": restore_evidence["last_verified"],
            "restore_proof_state": restore_evidence["state"],
            "restore_evidence": restore_evidence,
        },
        "offsite_backup": {
            "required": OFFSITE_REQUIRED,
            "local_only_risk": "Loss of the entire location is not covered." if not OFFSITE_REQUIRED else None,
            "configured": bool(offsite),
            "state": offsite.get("state", "unavailable"),
            "last_attempt": offsite_last,
            "age_hours": offsite_age,
            "source_snapshot_confirmed": offsite_source_confirmed,
            "repository_sample_checked": offsite.get("repository_sample_checked") is True,
            "object_lock_verified": object_lock_verified,
            "offsite_restore_verified": offsite_restore_verified,
            "pruning_enabled": offsite.get("pruning_enabled"),
        },
    }, "" if state == "healthy" else "Check both backup destinations and timers.", code)


def temperature_card() -> dict:
    temp_ok, temp_raw = command(["vcgencmd", "measure_temp"])
    throttle_ok, throttle_raw = command(["vcgencmd", "get_throttled"])
    temperature = None
    if temp_ok and "=" in temp_raw:
        try:
            temperature = float(temp_raw.split("=")[1].split("'")[0])
        except ValueError:
            pass
    throttled = 0
    if throttle_ok and "=" in throttle_raw:
        try:
            throttled = int(throttle_raw.split("=")[1].strip(), 16)
        except ValueError:
            pass
    mem = {}
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        meminfo = ""
    for line in meminfo.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            mem[key] = int(value.strip().split()[0])
    try:
        load = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
    except OSError:
        load = ["0", "0", "0"]
    kernel_ok, kernel = command(["journalctl", "-k", "--since", "24 hours ago", "--no-pager"], timeout=8, limit=262144)
    lowered = kernel.lower() if kernel_ok else ""
    active_throttle = bool(throttled & 0x7)
    historical_throttle = bool(throttled & 0x70000)
    active_under = bool(throttled & 0x1)
    historical_under = bool(throttled & 0x10000)
    recent_peak = temperature
    metrics_db = DATA / "metrics.db"
    if metrics_db.is_file():
        try:
            with sqlite3.connect(f"file:{metrics_db}?mode=ro", uri=True, timeout=1) as connection:
                row = connection.execute(
                    "SELECT MAX(temperature) FROM system_metrics "
                    "WHERE recorded_at >= datetime('now', '-24 hours')"
                ).fetchone()
                if row and row[0] is not None:
                    recent_peak = max(temperature or float(row[0]), float(row[0]))
        except (sqlite3.Error, OSError):
            pass
    state = "critical" if active_throttle or (temperature is not None and temperature >= 80) else "warning" if historical_throttle or (temperature is not None and temperature >= 70) else "healthy"
    return card(state, "Temperature and power look normal." if state == "healthy" else "A temperature or power event needs review.", {
        "temperature_c": temperature, "recent_peak_c": recent_peak,
        "host_uptime_seconds": host_uptime_seconds(),
        "active_throttling": active_throttle, "historical_throttling": historical_throttle,
        "active_undervoltage": active_under, "historical_undervoltage": historical_under,
        "load_average": [float(value) for value in load],
        "ram_total_gb": round(mem.get("MemTotal", 0) / 1048576, 2),
        "ram_available_gb": round(mem.get("MemAvailable", 0) / 1048576, 2),
        "swap_total_gb": round(mem.get("SwapTotal", 0) / 1048576, 2),
        "swap_free_gb": round(mem.get("SwapFree", 0) / 1048576, 2),
        "oom_events": lowered.count("out of memory") + lowered.count("oom-kill"),
        "usb_resets_disconnects": lowered.count("reset superspeed usb") + lowered.count("usb disconnect"),
        "filesystem_io_errors": sum(lowered.count(term) for term in ("i/o error", "buffer i/o", "ext4-fs error")),
    }, "" if state == "healthy" else "Check cooling, power, and recent kernel events.", "THERMAL_POWER_" + state.upper())


def tailscale_card() -> dict:
    status_ok, raw = command(["tailscale", "status", "--json"], timeout=6, limit=262144)
    status = {}
    if status_ok:
        try:
            status = json.loads(raw)
        except json.JSONDecodeError:
            pass
    self_node = status.get("Self") or {}
    connected = self_node.get("Online") is True or status.get("BackendState") == "Running"
    addresses = self_node.get("TailscaleIPs") or []
    serve_ok, serve = command(["tailscale", "serve", "status"])
    funnel_ok, funnel = command(["tailscale", "funnel", "status"])
    listeners_ok, listeners = command(["ss", "-ltnH"])
    lan_violation = any(":80 " in line and ("0.0.0.0:80" in line or "[::]:80" in line or "*:80" in line) for line in listeners.splitlines()) if listeners_ok else True
    loopback = "127.0.0.1:8090" in listeners if listeners_ok else False
    serve_private = "127.0.0.1:8090" in serve and "tailnet only" in serve.lower()
    funnel_disabled = "funnel on" not in funnel.lower() and "public" not in funnel.lower()
    public_host = str(self_node.get("DNSName") or "").rstrip(".")
    resolve = f"{public_host}:443:{addresses[0]}" if addresses and public_host else ""
    https_arguments = ["curl", "-fsS", "-o", "/dev/null", "-w", "%{time_total}", "--max-time", "5"]
    if resolve:
        https_arguments.extend(["--resolve", resolve])
    https_arguments.append(f"https://{public_host}/health")
    https_ok, latency = command(https_arguments)
    state = "healthy"
    code = "TAILSCALE_PRIVATE_HEALTHY"
    if not connected or not serve_private or not funnel_disabled or lan_violation or not loopback:
        state, code = "critical", "TAILSCALE_SECURITY_INVARIANT_FAILED"
    elif not https_ok:
        state, code = "warning", "TAILSCALE_HTTPS_CHECK_FAILED"
    return card(state, "Private Tailscale access is configured correctly." if state == "healthy" else "Private access needs attention.", {
        "connected": connected, "ip": addresses[0] if addresses else "Unavailable",
        "hostname": self_node.get("HostName", "Unavailable"), "serve_private": serve_private,
        "serve_backend": "127.0.0.1:8090" if serve_private else "Unexpected",
        "private_https_healthy": https_ok, "https_latency_ms": round(float(latency or 0) * 1000, 1) if https_ok else None,
        "funnel_disabled": funnel_disabled, "lan_port_80_closed": not lan_violation,
        "loopback_mapping_present": loopback,
        "exit_node_advertised": bool((self_node.get("PrimaryRoutes") or [])),
        "connection_path": "Unavailable",
    }, "" if state == "healthy" else "Restore loopback-only Serve and keep Funnel disabled.", code)


def pihole_card() -> dict:
    info = safe_json(PIHOLE)
    age = age_hours(info.get("updated_at"))
    if not info or age is None:
        return card("unavailable", "Pi-hole totals are unavailable.", {}, "Check the aggregate collector.", "PIHOLE_UNAVAILABLE")
    stale = bool(info.get("stale")) or age > 0.25
    state = "warning" if stale else "healthy"
    return card(state, "Pi-hole totals are current." if not stale else "Pi-hole totals are stale; last values are preserved.", {
        "snapshot_time": info.get("updated_at"), "age_minutes": round(age * 60, 1),
        "total_queries": int(info.get("total", 0)), "blocked_queries": int(info.get("blocked", 0)),
        "blocked_percent": float(info.get("blocked_percent", 0)), "collector": "stale" if stale else "healthy",
    }, "" if not stale else "Check the Pi-hole summary timer.", "PIHOLE_STALE" if stale else "PIHOLE_HEALTHY")


def jobs_card() -> dict:
    jobs = {"active": 0, "pending": 0, "recent_completed": 0, "recent_failed": 0, "oldest_pending_age_seconds": None}
    database_available = False
    try:
        with sqlite3.connect(f"file:{DATA / 'photos.db'}?mode=ro", uri=True, timeout=2) as database:
            cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
            jobs["active"] = database.execute("SELECT COUNT(*) FROM slideshow_jobs WHERE status='working'").fetchone()[0]
            jobs["pending"] = database.execute("SELECT COUNT(*) FROM slideshow_jobs WHERE status='queued'").fetchone()[0]
            jobs["recent_completed"] = database.execute("SELECT COUNT(*) FROM slideshow_jobs WHERE status='completed' AND updated_at>=?", (cutoff,)).fetchone()[0]
            jobs["recent_failed"] = database.execute("SELECT COUNT(*) FROM slideshow_jobs WHERE status='failed' AND updated_at>=?", (cutoff,)).fetchone()[0]
            oldest = database.execute("SELECT MIN(created_at) FROM slideshow_jobs WHERE status='queued'").fetchone()[0]
            if oldest:
                jobs["oldest_pending_age_seconds"] = int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(oldest.replace("Z", "+00:00"))).total_seconds())
            database_available = True
    except (OSError, sqlite3.Error, ValueError):
        pass
    workers = {}
    for name in (
        "david-pi-audiobook-preparer",
        "david-pi-chat-notifier",
        "david-pi-maintenance",
        "david-pi-device-backup-worker",
        "david-pi-slideshow-worker",
    ):
        ok, raw = command([
            "docker", "inspect", name, "--format", "{{json .State}}|{{.RestartCount}}",
        ])
        worker = {
            "available": False, "running": False, "health": "unavailable",
            "restart_count": None, "started_at": None,
        }
        if ok and "|" in raw:
            state_raw, restart_raw = raw.strip().rsplit("|", 1)
            try:
                runtime = json.loads(state_raw)
                runtime_health = runtime.get("Health")
                health = (
                    runtime_health.get("Status", "unknown")
                    if isinstance(runtime_health, dict)
                    else "unknown"
                )
                worker.update({
                    "available": True,
                    "running": runtime.get("Running") is True,
                    "health": health,
                    "restart_count": int(restart_raw or 0),
                    "started_at": runtime.get("StartedAt") or None,
                })
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        workers[name] = worker
    spool = size_gb(DATA / "tmp" / "uploads")
    healthchecked_unhealthy = any(
        not workers[name]["available"]
        or not workers[name]["running"]
        or workers[name]["health"] != "healthy"
        for name in (
            "david-pi-maintenance",
            "david-pi-device-backup-worker",
            "david-pi-slideshow-worker",
        )
    )
    other_worker_unhealthy = any(
        not worker["available"]
        or not worker["running"]
        or worker["health"] in {"dead", "exited", "unhealthy"}
        for name, worker in workers.items()
        if name not in {
            "david-pi-maintenance",
            "david-pi-device-backup-worker",
            "david-pi-slideshow-worker",
        }
    )
    old_queue = jobs["oldest_pending_age_seconds"] is not None and jobs["oldest_pending_age_seconds"] >= 7200
    if not database_available:
        state = "unavailable"
    elif jobs["recent_failed"] >= 3:
        state = "critical"
    elif healthchecked_unhealthy or other_worker_unhealthy or jobs["pending"] >= 3 or jobs["recent_failed"] or old_queue:
        state = "warning"
    else:
        state = "healthy"
    return card(state, "Background work is operating normally." if state == "healthy" else "Some background work needs review.", {
        "slideshows": {**jobs, "database_available": database_available, "queue_capacity": 3, "recent_timeouts": 0, "queue_full": 0, "duplicates_suppressed": 0},
        "workers": workers,
        "pdf": {"active": 0, "waiting": 0, "recent_failures": 0, "recent_timeouts": 0, "cache_gb": size_gb(DATA / "files" / "cache")},
        "uploads": {"active": 0, "recent_successes": 0, "recent_failures": 0, "spool_gb": spool, "stale_temp_files": 0, "rejections": 0},
        "remote_work": {"successes": 0, "timeouts": 0, "safe_failures": 0},
    }, "" if state == "healthy" else "Review failed or queued jobs.", "JOBS_" + state.upper())


def unit_state(name: str) -> dict:
    ok, raw = command([
        "systemctl", "show", name, "--no-pager",
        "--property=ActiveState,SubState,Result,ExecMainStatus,LastTriggerUSec,NextElapseUSecRealtime",
    ])
    values = {}
    if ok:
        for line in raw.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
    return {
        "active": values.get("ActiveState", "unavailable"),
        "sub_state": values.get("SubState", "unavailable"),
        "result": values.get("Result", "unavailable"),
        "last_exit": values.get("ExecMainStatus", "unavailable"),
        "last_run": values.get("LastTriggerUSec") or None,
        "next_run": values.get("NextElapseUSecRealtime") or None,
    }


def access_control_card() -> dict:
    """Aggregate content-neutral denial evidence across every web worker.

    The deployment label declares the mode without exposing the container's
    environment. Only fixed-format counter lines are parsed from bounded
    container logs; all other log content is discarded rather than published.
    """
    label_template = f'{{{{index .Config.Labels "{ACCESS_MODE_LABEL}"}}}}'
    mode_ok, raw_mode = command(
        ["docker", "inspect", ACCESS_CONTAINER, "--format", label_template],
        limit=256,
    )
    if not mode_ok:
        return card(
            "unavailable",
            "The portal access-control mode and denial totals are unavailable.",
            {
                "configured_mode": "unknown",
                "effective_mode": "unknown",
                "configuration_valid": False,
                "enforcement_active": False,
                "counter_scope": "container_aggregate_rolling_window",
                "counter_window_hours": ACCESS_COUNTER_WINDOW_HOURS,
                "counter_line_limit": ACCESS_COUNTER_LINE_LIMIT,
                "counter_window_complete": False,
                "observed_denials_total": None,
                "blocked_denials_total": None,
                "shadow_denials_total": None,
                "unenforced_denials_total": None,
            },
            "Check the collector's Docker access and the portal deployment label.",
            "ACCESS_STATE_UNAVAILABLE",
        )

    mode = raw_mode.strip().casefold()
    configuration_valid = mode in ACCESS_MODES
    configured_mode = mode if configuration_valid else "unknown"
    # The application deliberately fails closed to enforcement when
    # configuration is invalid. Keep the invalid configuration visible as a
    # warning even though requests remain protected.
    effective_mode = mode if configuration_valid else "enforce"
    logs_ok, raw_logs = command(
        [
            "docker",
            "logs",
            "--since",
            f"{ACCESS_COUNTER_WINDOW_HOURS}h",
            "--tail",
            str(ACCESS_COUNTER_LINE_LIMIT),
            ACCESS_CONTAINER,
        ],
        timeout=10,
        limit=ACCESS_COUNTER_OUTPUT_LIMIT,
        include_stderr=True,
    )
    lines = raw_logs.splitlines() if logs_ok else []
    window_complete = bool(
        logs_ok
        and len(lines) < ACCESS_COUNTER_LINE_LIMIT
        and len(raw_logs) < ACCESS_COUNTER_OUTPUT_LIMIT
    )
    counters = {"blocked": 0, "shadow": 0, "unenforced": 0}
    valid_pair = {"enforce": "blocked", "shadow": "shadow", "off": "unenforced"}
    if logs_ok:
        for line in lines:
            match = ACCESS_COUNTER_PATTERN.search(line)
            if match and valid_pair[match.group(1)] == match.group(2):
                counters[match.group(2)] += 1

    details = {
        "configured_mode": configured_mode,
        "effective_mode": effective_mode,
        "configuration_valid": configuration_valid,
        "enforcement_active": configuration_valid and effective_mode == "enforce",
        "counter_scope": "container_aggregate_rolling_window",
        "counter_window_hours": ACCESS_COUNTER_WINDOW_HOURS,
        "counter_line_limit": ACCESS_COUNTER_LINE_LIMIT,
        "counter_window_complete": window_complete,
        "observed_denials_total": sum(counters.values()) if logs_ok else None,
        "blocked_denials_total": counters["blocked"] if logs_ok else None,
        "shadow_denials_total": counters["shadow"] if logs_ok else None,
        "unenforced_denials_total": counters["unenforced"] if logs_ok else None,
    }
    if not configuration_valid:
        return card(
            "warning",
            "The configured access mode is unknown; fail-closed enforcement is active.",
            details,
            "Set DAVID_PI_ACCESS_MODE to off, shadow, or enforce explicitly.",
            "ACCESS_MODE_UNKNOWN",
        )
    if not logs_ok:
        return card(
            "warning",
            "The access mode is known, but aggregate denial evidence is unavailable.",
            details,
            "Check access to the bounded portal container logs.",
            "ACCESS_COUNTERS_UNAVAILABLE",
        )
    if not window_complete:
        return card(
            "warning",
            "Access-denial totals are partial because the bounded window was capped.",
            details,
            "Review log volume before relying on the rolling denial totals.",
            "ACCESS_COUNTER_WINDOW_CAPPED",
        )
    if effective_mode == "enforce":
        return card(
            "healthy",
            "The reviewed private identity allowlist is enforced.",
            details,
            "",
            "ACCESS_ENFORCEMENT_ACTIVE",
        )
    if effective_mode == "shadow":
        return card(
            "warning",
            "Access decisions are observed, but denials are not enforced.",
            details,
            "Complete the identity canary before enabling enforcement.",
            "ACCESS_SHADOW_ACTIVE",
        )
    return card(
        "warning",
        "Central portal access enforcement is turned off.",
        details,
        "Use shadow for a canary or enforce after the access gate passes.",
        "ACCESS_ENFORCEMENT_OFF",
    )


def services_card() -> dict:
    units = {name: unit_state(name) for name in SERVICES}
    failed = [name for name, value in units.items() if value["active"] == "failed" or value["result"] == "failed"]
    state = "critical" if any(name in failed for name in ("david-pi-portal.service", "docker.service", "tailscaled.service")) else "warning" if failed else "healthy"
    return card(state, "Required services and timers are healthy." if not failed else f"{len(failed)} service or timer checks need attention.", {
        "units": units, "failed_count": len(failed),
    }, "" if not failed else "Review the failed unit details over SSH.", "SERVICES_" + state.upper())


def databases_summary() -> list[dict]:
    backup_sets = sorted((path for path in BACKUPS.glob("*-daily/databases") if path.is_dir()), reverse=True)
    latest = backup_sets[0] if backup_sets else None
    result = []
    backup_status = safe_json(BACKUP_STATUS)
    backup_verified = bool(
        backup_status.get("ok")
        and backup_status.get("quick_check") is True
        and age_hours(backup_status.get("last_success")) is not None
    )
    for path in expected_databases():
        try:
            stat = path.stat()
            backup = latest / path.name if latest else None
            result.append({
                "name": path.name, "present": True, "size_bytes": stat.st_size,
                "last_modified": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
                "last_backup": dt.datetime.fromtimestamp(backup.stat().st_mtime, dt.timezone.utc).isoformat() if backup and backup.is_file() else None,
                "last_integrity_check": "passed" if backup and backup.is_file() and backup_verified else "unavailable",
                "busy_locked_errors": 0, "unexpected_growth": False,
            })
        except OSError:
            result.append({"name": path.name, "present": False, "size_bytes": 0, "last_modified": None, "last_backup": None, "last_integrity_check": "unavailable", "busy_locked_errors": 0, "unexpected_growth": False})
    return result


def updates_card() -> dict:
    ok, raw = command(["apt", "list", "--upgradable"], timeout=12, limit=131072)
    pending = max(0, len([line for line in raw.splitlines() if "/" in line])) if ok else None
    reboot = REBOOT_REQUIRED.exists()
    image_ok, image = command(["docker", "inspect", "family-photo-portal", "--format", "{{.Config.Image}}"])
    deploy_time = None
    compose = Path("/srv/compose/photo-portal/compose.yaml")
    if compose.exists():
        deploy_time = dt.datetime.fromtimestamp(compose.stat().st_mtime, dt.timezone.utc).isoformat()
    # Repository file mtimes describe publication, not a successful local refresh.
    last_package_update = file_mtime(UPDATE_SUCCESS)
    check_age = age_hours(last_package_update)
    last_unattended_upgrade = file_mtime(Path("/var/log/unattended-upgrades/unattended-upgrades.log"))
    if not ok:
        state, code, summary = "unavailable", "UPDATES_CHECK_UNAVAILABLE", "Package update status is unavailable."
    elif check_age is None or check_age < 0 or check_age > 48:
        state, code, summary = "warning", "UPDATES_CHECK_STALE", "A successful recent package refresh has not been verified."
    elif reboot or pending:
        state, code, summary = "warning", "UPDATES_WAITING", "Updates or a reboot are waiting."
    else:
        state, code, summary = "healthy", "UPDATES_HEALTHY", "No pending package updates were reported."
    return card(state, summary, {
        "last_package_list_update": last_package_update, "last_unattended_upgrade": last_unattended_upgrade,
        "check_age_hours": check_age,
        "pending_packages": pending, "security_updates": None, "reboot_required": reboot,
        "last_portal_deployment": deploy_time, "application_version": image.strip() if image_ok else "Unavailable",
    }, "Check package metadata or schedule updates when convenient." if state != "healthy" else "", code)


def collect() -> dict:
    collectors = {
        "portal": portal_card, "external_drive": drive_card, "storage": storage_card,
        "backups": backups_card, "temperature_power": temperature_card,
        "tailscale": tailscale_card, "pihole": pihole_card,
        "background_jobs": jobs_card, "services": services_card, "updates": updates_card,
        "access_control": access_control_card,
    }
    subsystems = {}
    for name, function in collectors.items():
        try:
            subsystems[name] = function()
        except Exception:
            subsystems[name] = card("unavailable", "This health check is temporarily unavailable.", {}, "Check the collector service.", f"{name.upper()}_COLLECT_FAILED")
    overall = max((value["state"] for value in subsystems.values()), key=lambda value: STATE_RANK[value])
    return {
        "schema_version": 1, "generated_at": now_iso(), "state": overall,
        "subsystems": subsystems, "databases": databases_summary(),
        "orphan_audit": {"count": None, "size_bytes": None, "state": "not_run"},
        "privacy": {"contains_personal_filenames": False, "contains_domains": False, "contains_clients": False, "contains_secrets": False},
    }


def publish(payload: dict) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".server-status-", dir=OUTPUT.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    publish(collect())
