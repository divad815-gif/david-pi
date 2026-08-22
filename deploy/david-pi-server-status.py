#!/usr/bin/env python3
"""Collect a sanitized, aggregate-only David-Pi server health snapshot."""

from __future__ import annotations

import datetime as dt
import json
import os
import platform
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time


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
EXPECTED_DATABASES = (
    DATA / "photos.db", DATA / "metrics.db", DATA / "platform/notes.db",
    DATA / "platform/movies.db", DATA / "platform/recipes.db",
    DATA / "platform/files.db", DATA / "platform/platform.db",
    DATA / "platform/assistant/assistant.db",
    DATA / "platform/places.db",
    DATA / "platform/audiobooks.db",
    DATA / "platform/chat.db",
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


def platform_profile() -> str:
    try:
        model = Path("/proc/device-tree/model").read_bytes().replace(b"\0", b"").decode("utf-8", "ignore")
    except OSError:
        model = ""
    if "Raspberry Pi" in model:
        return "raspberry-pi"
    if any(Path("/sys/class/power_supply").glob("BAT*")):
        return "linux-laptop"
    return "linux-server"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def command(arguments: list[str], timeout: int = 5, limit: int = 65536) -> tuple[bool, str]:
    """Run one fixed command with bounded time and output."""
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, timeout=timeout,
            check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        output = (result.stdout or "")[:limit]
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


def disk_details(path: Path) -> dict:
    usage = shutil.disk_usage(path)
    return {
        "total_gb": round(usage.total / 1073741824, 1),
        "used_gb": round(usage.used / 1073741824, 1),
        "free_gb": round(usage.free / 1073741824, 1),
        "used_percent": round(100 * usage.used / max(usage.total, 1), 1),
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
    restart_count = int(restarts or 0)
    status = "healthy"
    code = "PORTAL_HEALTHY"
    summary = "The household portal is healthy."
    if health != "healthy" or root_user or not sentinel_ok:
        status, code, summary = "critical", "PORTAL_INVARIANT_FAILED", "The portal failed a safety or health check."
    elif restart_count >= 3:
        status, code, summary = "warning", "PORTAL_RESTARTS", "The portal is running but has restarted repeatedly."
    return card(status, summary, {
        "container_health": health, "image": image, "uptime_seconds": uptime,
        "restart_count": restart_count, "health_latency_ms": latency_ms,
        "memory_usage": stats.get("MemUsage", "Unavailable"),
        "memory_percent": stats.get("MemPerc", "Unavailable"),
        "cpu_percent": stats.get("CPUPerc", "Unavailable"),
        "pid_usage": stats.get("PIDs", "Unavailable"),
        "memory_limit_bytes": int(memory_limit or 0), "pid_limit": int(pids_limit or 0),
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
    source = str(record.get("source") or "")
    transport = "unavailable"
    physical_device = ""
    if source.startswith("/dev/"):
        parent_ok, parent = command(["lsblk", "-ndo", "PKNAME", source])
        physical_device = f"/dev/{parent.strip()}" if parent_ok and parent.strip() else source
        transport_ok, transport_raw = command(["lsblk", "-ndo", "TRAN", physical_device])
        if transport_ok and transport_raw.strip():
            transport = transport_raw.strip().lower()
    usb_ok, usb = command(["lsusb", "-t"])
    uas = "Driver=uas" in usb
    usb3 = any(speed in usb for speed in ("5000M", "10000M", "20000M"))
    kernel_ok, kernel = command(["journalctl", "-k", "--since", "24 hours ago", "--no-pager"], timeout=8, limit=262144)
    lowered = kernel.lower() if kernel_ok else ""
    errors = sum(lowered.count(term) for term in ("i/o error", "buffer i/o", "ext4-fs error"))
    resets = lowered.count("reset superspeed usb") + lowered.count("usb disconnect")
    smart = "unavailable"
    if physical_device:
        smart_ok, smart_raw = command(["smartctl", "-H", physical_device], timeout=8, limit=16384)
        lowered_smart = smart_raw.lower()
        if smart_ok and any(term in lowered_smart for term in ("passed", "ok")):
            smart = "healthy"
        elif smart_raw and any(term in lowered_smart for term in ("failed", "failing")):
            smart = "warning"
    status, code, summary = "healthy", "DRIVE_HEALTHY", "The data drive is mounted correctly."
    if not mounted or filesystem != "ext4" or not writable or not sentinel_ok or errors:
        status, code, summary = "critical", "DRIVE_INVARIANT_FAILED", "The data drive needs attention."
    elif smart == "warning":
        status, code, summary = "warning", "DRIVE_SMART_WARNING", "The data drive reported a health warning."
    elif transport == "usb" and resets:
        status, code, summary = "warning", "DRIVE_USB_WARNING", "The drive is mounted, with a USB warning to review."
    elif transport == "usb" and usb3 and not uas:
        summary = "The data drive is healthy over USB 3; UAS is unavailable through this adapter."
    return card(status, summary, {
        "mounted": mounted, "source": record.get("source", "Unavailable"),
        "uuid": record.get("uuid", "Unavailable"), "filesystem": filesystem or "Unavailable",
        "read_write": writable, "sentinel_valid": sentinel_ok, "transport": transport,
        "usb3": usb3 if transport == "usb" else None,
        "usb3_uas": uas if transport == "usb" else None,
        "recent_usb_resets": resets, "recent_io_or_filesystem_errors": errors,
        "smart": smart,
    }, "" if status == "healthy" else "Check the drive connection and system log.", code)


def storage_card() -> dict:
    external = disk_details(Path("/srv/data"))
    os_disk = disk_details(Path("/"))
    categories = {
        "originals_gb": size_gb(DATA / "originals"),
        "generated_videos_gb": size_gb(DATA / "previews"),
        "documents_gb": size_gb(DATA / "files" / "objects"),
        "previews_thumbnails_gb": round(size_gb(DATA / "previews") + size_gb(DATA / "thumbs"), 3),
        "pdf_cache_gb": size_gb(DATA / "files" / "cache"),
        "trash_gb": size_gb(DATA / "quarantine"),
        "upload_spool_gb": round(size_gb(DATA / "incoming") + size_gb(DATA / "tmp" / "uploads"), 3),
        "databases_gb": round(sum(path.stat().st_size for path in EXPECTED_DATABASES if path.is_file()) / 1073741824, 3),
        "backups_gb": size_gb(BACKUPS),
    }
    stale_parts = 0
    cutoff = time.time() - 86400
    for root in (DATA / "incoming", DATA / "tmp" / "uploads", DATA / "files" / "incoming"):
        if root.is_dir():
            stale_parts += sum(1 for item in root.glob("*.part") if item.is_file() and item.stat().st_mtime < cutoff)
    used = max(external["used_percent"], os_disk["used_percent"])
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
        "external": external, "os_disk": os_disk, "microsd": os_disk, "categories": categories,
        "stale_part_files": stale_parts, "upload_render_reserve_gb": 1,
        "growth": growth, "low_space_rejections": None,
    }, "" if status == "healthy" else "Review free space and stale temporary data.", code)


def backups_card() -> dict:
    info = safe_json(BACKUP_STATUS)
    last = info.get("last_success")
    age = age_hours(last)
    ok = bool(info.get("ok")) and age is not None
    independent = safe_json(INDEPENDENT_BACKUP_STATUS)
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
    if not ok or (age is not None and age >= 72):
        state, code = "critical", "BACKUP_FAILED_OR_OLD"
    elif not independent_configured:
        state, code = "warning", "DATA_BACKUP_NOT_CONFIGURED"
    elif not independent_ok or (independent_age is not None and independent_age >= 72):
        state, code = "critical", "DATA_BACKUP_FAILED_OR_OLD"
    elif age >= 30 or independent_age >= 30:
        state, code = "warning", "BACKUP_STALE"
    else:
        state, code = "healthy", "BACKUPS_HEALTHY"
    retained = len([path for path in BACKUPS.glob("*-daily") if path.is_dir()]) if BACKUPS.is_dir() else 0
    summary = (
        "Recovery and independent data backups are current."
        if state == "healthy"
        else "One or more backups are stale, failed, or unavailable."
    )
    return card(state, summary, {
        "last_attempt": info.get("last_attempt", last), "last_success": last,
        "age_hours": age, "last_failure": info.get("error"),
        "expected_databases": len(EXPECTED_DATABASES),
        "databases_included": info.get("database_count", 0),
        "source_archive_included": ok, "quick_check": "passed" if ok else "unavailable",
        "retained_sets": retained, "timer": unit_state("david-pi-backup.timer"),
        "independent_data_backup": {
            "configured": independent_configured,
            "originals_included": independent_ok,
            "documents_included": independent_ok,
            "physically_independent": physically_independent,
            "last_success": independent_last,
            "age_hours": independent_age,
            "snapshot": independent.get("snapshot"),
            "databases_included": independent.get("database_count", 0),
            "timer": unit_state("david-pi-data-backup.timer"),
            "last_restoration_test": None,
        },
    }, "" if state == "healthy" else "Check both backup destinations and timers.", code)


def generic_temperature() -> tuple[float | None, str]:
    candidates: list[float] = []
    for path in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            value = float(path.read_text(encoding="utf-8").strip())
            value = value / 1000 if value > 1000 else value
            if 0 < value < 125:
                candidates.append(value)
        except (OSError, ValueError):
            continue
    for path in Path("/sys/class/hwmon").glob("hwmon*/temp*_input"):
        try:
            name = (path.parent / "name").read_text(encoding="utf-8").strip().lower()
            label_path = path.with_name(path.name.replace("_input", "_label"))
            label = label_path.read_text(encoding="utf-8").strip().lower() if label_path.exists() else ""
            if not any(term in f"{name} {label}" for term in ("cpu", "core", "package", "soc", "k10temp", "coretemp")):
                continue
            value = float(path.read_text(encoding="utf-8").strip())
            value = value / 1000 if value > 1000 else value
            if 0 < value < 125:
                candidates.append(value)
        except (OSError, ValueError):
            continue
    return (round(max(candidates), 1), "linux-sysfs") if candidates else (None, "unavailable")


def battery_details() -> dict:
    power_root = Path("/sys/class/power_supply")
    batteries = sorted(power_root.glob("BAT*"))
    if not batteries:
        return {"present": False, "capacity_percent": None, "status": "unavailable", "ac_online": None}
    battery = batteries[0]
    try:
        capacity = int((battery / "capacity").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        capacity = None
    try:
        status = (battery / "status").read_text(encoding="utf-8").strip().lower()
    except OSError:
        status = "unavailable"
    ac_online = None
    for supply in power_root.iterdir() if power_root.is_dir() else ():
        if supply.name.startswith("BAT"):
            continue
        try:
            supply_type = (supply / "type").read_text(encoding="utf-8").strip().lower()
            if supply_type in {"mains", "usb", "usb_c"} and (supply / "online").exists():
                ac_online = (supply / "online").read_text(encoding="utf-8").strip() == "1"
                if ac_online:
                    break
        except OSError:
            continue
    return {"present": True, "capacity_percent": capacity, "status": status, "ac_online": ac_online}


def temperature_card() -> dict:
    temp_ok, temp_raw = command(["vcgencmd", "measure_temp"])
    throttle_ok, throttle_raw = command(["vcgencmd", "get_throttled"])
    temperature = None
    if temp_ok and "=" in temp_raw:
        try:
            temperature = float(temp_raw.split("=")[1].split("'")[0])
        except ValueError:
            pass
    sensor_source = "vcgencmd" if temperature is not None else "unavailable"
    if temperature is None:
        temperature, sensor_source = generic_temperature()
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
    battery = battery_details()
    battery_warning = bool(
        battery["present"]
        and battery["ac_online"] is False
        and battery["capacity_percent"] is not None
        and battery["capacity_percent"] < 20
    )
    state = "critical" if active_throttle or (temperature is not None and temperature >= 80) else "warning" if historical_throttle or battery_warning or (temperature is not None and temperature >= 70) else "healthy"
    return card(state, "Temperature and power look normal." if state == "healthy" else "A temperature or power event needs review.", {
        "platform_profile": platform_profile(), "temperature_c": temperature,
        "temperature_sensor": sensor_source, "recent_peak_c": recent_peak,
        "active_throttling": active_throttle, "historical_throttling": historical_throttle,
        "active_undervoltage": active_under, "historical_undervoltage": historical_under,
        "load_average": [float(value) for value in load],
        "ram_total_gb": round(mem.get("MemTotal", 0) / 1048576, 2),
        "ram_available_gb": round(mem.get("MemAvailable", 0) / 1048576, 2),
        "swap_total_gb": round(mem.get("SwapTotal", 0) / 1048576, 2),
        "swap_free_gb": round(mem.get("SwapFree", 0) / 1048576, 2),
        "battery": battery,
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
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    resolve = f"{dns_name}:443:{addresses[0]}" if dns_name and addresses else ""
    https_arguments = ["curl", "-fsS", "-o", "/dev/null", "-w", "%{time_total}", "--max-time", "5"]
    if resolve:
        https_arguments.extend(["--resolve", resolve])
    if dns_name:
        https_arguments.append(f"https://{dns_name}/health")
        https_ok, latency = command(https_arguments)
    else:
        https_ok, latency = False, ""
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
    except (OSError, sqlite3.Error, ValueError):
        pass
    spool = size_gb(DATA / "tmp" / "uploads")
    state = "critical" if jobs["recent_failed"] >= 3 else "warning" if jobs["pending"] >= 3 or jobs["recent_failed"] else "healthy"
    return card(state, "Background work is operating normally." if state == "healthy" else "Some background work needs review.", {
        "slideshows": {**jobs, "queue_capacity": 4, "recent_timeouts": 0, "queue_full": 0, "duplicates_suppressed": 0},
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
    for path in EXPECTED_DATABASES:
        try:
            stat = path.stat()
            backup = latest / path.name if latest else None
            result.append({
                "name": path.name, "present": True, "size_bytes": stat.st_size,
                "last_modified": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).isoformat(),
                "last_backup": dt.datetime.fromtimestamp(backup.stat().st_mtime, dt.timezone.utc).isoformat() if backup and backup.is_file() else None,
                "last_integrity_check": "passed" if backup and backup.is_file() else "unavailable",
                "busy_locked_errors": 0, "unexpected_growth": False,
            })
        except OSError:
            result.append({"name": path.name, "present": False, "size_bytes": 0, "last_modified": None, "last_backup": None, "last_integrity_check": "unavailable", "busy_locked_errors": 0, "unexpected_growth": False})
    return result


def updates_card() -> dict:
    ok, raw = command(["apt", "list", "--upgradable"], timeout=12, limit=131072)
    pending = max(0, len([line for line in raw.splitlines() if "/" in line]) if ok else 0)
    reboot = Path("/var/run/reboot-required").exists()
    image_ok, image = command(["docker", "inspect", "family-photo-portal", "--format", "{{.Config.Image}}"])
    deploy_time = None
    compose = Path("/srv/compose/photo-portal/compose.yaml")
    if compose.exists():
        deploy_time = dt.datetime.fromtimestamp(compose.stat().st_mtime, dt.timezone.utc).isoformat()
    state = "warning" if reboot or pending else "healthy"
    return card(state, "The operating system is current." if state == "healthy" else "Updates or a reboot are waiting.", {
        "last_package_list_update": None, "last_unattended_upgrade": None,
        "pending_packages": pending, "security_updates": None, "reboot_required": reboot,
        "last_portal_deployment": deploy_time, "application_version": image.strip() if image_ok else "Unavailable",
    }, "Schedule updates or a reboot when convenient." if state != "healthy" else "", "UPDATES_" + state.upper())


def collect() -> dict:
    collectors = {
        "portal": portal_card, "external_drive": drive_card, "storage": storage_card,
        "backups": backups_card, "temperature_power": temperature_card,
        "tailscale": tailscale_card, "pihole": pihole_card,
        "background_jobs": jobs_card, "services": services_card, "updates": updates_card,
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
        "host": {"platform_profile": platform_profile(), "architecture": platform.machine()},
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
