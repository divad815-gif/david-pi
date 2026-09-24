#!/usr/bin/env python3
"""Restricted host controller. No shell execution or caller-selected commands.

The root service accepts a bounded protocol on a private Unix socket. Only the
portal's fixed uid and root can connect; portal requests carry the authenticated
Tailscale login, which is checked against admitted administrators. Files under
/etc/david-pi are root-owned. Public responses never include secret values.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import contextlib
import fcntl
import hashlib
import hmac
import http.cookies
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from zoneinfo import available_timezones
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = Path("/usr/local/lib/david-pi")
SYSTEMD_ROOT = Path("/etc/systemd/system")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from modules.installation import InstallationError, MODULES, selected_modules, selected_substrates, selected_workers, validate_installation, validate_origin
from installer import update_storage, recovery

IMAGE = re.compile(r"^ghcr\.io/[a-z0-9_.-]+/david-pi@sha256:[0-9a-f]{64}$")
VERSION = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.[1-9][0-9]*)?$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/david-pi$")
SECRET_NAMES = {"TMDB_API_READ_TOKEN", "THEMEALDB_API_KEY", "PIHOLE_API_PASSWORD"}


class HostError(ValueError):
    pass


class PrivateOriginChanged(HostError):
    """A strict origin check failed; only unclaimed fresh setup may retry."""


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".new-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def run(arguments, timeout=300):
    result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Subprocess output may include account links or provider secrets.
        raise HostError(f"{Path(arguments[0]).name} {arguments[1] if len(arguments) > 1 else ''} failed (exit {result.returncode}); inspect the local service log")
    return result.stdout


def safe_path(path, *, exists=True):
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or any(c in str(p) for c in "\r\n\x00"):
        raise HostError("Storage must be an absolute local path")
    if len(p.parts) < 2 or (len(p.parts) == 2 and not exists) or p.parts[1] not in {"srv", "mnt", "media", "home", "opt"}:
        raise HostError("Choose an existing storage folder under /srv, /mnt, /media, /home or /opt")
    for part in [p, *p.parents]:
        if part.is_symlink():
            raise HostError("Storage paths cannot contain symbolic links")
    if exists and not p.is_dir():
        raise HostError("The selected storage folder does not exist")
    return p


def saved_address(etc):
    """Read only the saved private origin; never print claim or session secrets."""
    root = Path(etc)
    try:
        config = read_json(root / "installation.json")
        setup = (read_json(root / "host-state/setup.json") or read_json(root / "host-state/recovery.json", {})) if config is None else {}
    except (OSError, ValueError):
        raise HostError("The saved private address cannot be read; inspect local configuration before continuing") from None
    if (config is not None and not isinstance(config, dict)) or not isinstance(setup, dict):
        raise HostError("The saved private address is invalid; inspect local configuration before continuing")
    value = config.get("public_url") if config is not None else setup.get("origin")
    if not value:
        raise HostError("No private address is saved yet. Run sudo david-pi setup on the server")
    try:
        return validate_origin(value)
    except InstallationError:
        raise HostError("The saved private address is invalid; inspect local configuration before continuing") from None


def print_address(etc):
    print(f"Your saved private home-server address:\n  {saved_address(etc)}/\nOpen it on a device connected to your household's Tailscale network.\nIf you deliberately renamed or moved the server, use sudo david-pi reconnect first.")


def saved_release_arguments(etc):
    """Validated values for a resumed terminal setup, without shell evaluation."""
    try:
        release = read_json(Path(etc) / "release.json", {})
    except (OSError, ValueError):
        raise HostError("Saved release metadata cannot be read. Resume using the verified stable release installer") from None
    if not isinstance(release, dict):
        raise HostError("No verified release is saved. Resume using the verified stable release installer")
    image, repository = release.get("image", ""), release.get("repository", "")
    if not isinstance(image, str) or not isinstance(repository, str) or not IMAGE.fullmatch(image) or not REPOSITORY.fullmatch(repository) or not image.startswith(f"ghcr.io/{repository.lower()}@"):
        raise HostError("No verified release is saved. Resume using the verified stable release installer")
    return image, repository


def validate_archive(archive, destination):
    """Reject links/devices/traversal, duplicate names and archive bombs."""
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = set()
        size = 0
        if len(members) > 100000:
            raise HostError("Archive has too many entries")
        for member in members:
            p = Path(member.name)
            if p.is_absolute() or ".." in p.parts or not (member.isfile() or member.isdir()) or member.name in names:
                raise HostError("Unsafe release archive")
            names.add(member.name)
            size += member.size
        if size > 4 * 1024**3:
            raise HostError("Release archive is too large")
        bundle.extractall(destination, members=members, filter="data")


def parse_manifest(text, repository, *, allow_prerelease=False):
    if not REPOSITORY.fullmatch(repository) or len(text) > 4096:
        raise HostError("Invalid release source")
    fields = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise HostError("Invalid release manifest")
        fields[key] = value
    if not VERSION.fullmatch(fields.get("VERSION", "")) or ("-" in fields["VERSION"] and not allow_prerelease):
        raise HostError("Only stable releases can be installed unless an exact testing release was explicitly selected")
    if fields.get("ARCHIVE") != f"david-pi-{fields['VERSION']}.tar.gz" or not re.fullmatch("[0-9a-f]{64}", fields.get("ARCHIVE_SHA256", "")):
        raise HostError("Invalid release checksum metadata")
    if not IMAGE.fullmatch(fields.get("IMAGE", "")) or not fields["IMAGE"].startswith(f"ghcr.io/{repository.lower()}@"):
        raise HostError("Release image does not match the configured repository")
    for field in ("DATA_SCHEMA_VERSION", "ROLLBACK_MIN_DATA_SCHEMA"):
        if not re.fullmatch("[1-9][0-9]{0,3}", fields.get(field, "")):
            raise HostError("Release is missing data compatibility metadata")
    return fields


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, request, fp, code, message, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in self.hosts or target.username or target.password:
            raise HostError("Provider redirected outside its approved HTTPS service")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def https_open(url, headers=None, timeout=20):
    target = urllib.parse.urlsplit(url)
    if target.scheme != "https" or target.username or target.password:
        raise HostError("HTTPS is required")
    hosts = {target.hostname}
    if target.hostname == "github.com":
        hosts.update({"release-assets.githubusercontent.com", "objects.githubusercontent.com"})
    request = urllib.request.Request(url, headers={"User-Agent": "David-Pi installer", **(headers or {})})
    return urllib.request.build_opener(RestrictedRedirect(hosts)).open(request, timeout=timeout)


def read_https(url, *, headers=None, limit=1048576):
    with https_open(url, headers=headers) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise HostError("Remote response is too large")
    return value


def assert_private_serve(state, dns, target=None):
    if any(state.get("AllowFunnel", {}).values()):
        raise HostError("Funnel is enabled on this machine. Disable it explicitly before setup; existing settings were not changed")
    tcp = state.get("TCP", {}).get("443", {})
    handlers = state.get("Web", {}).get(f"{dns}:443", {}).get("Handlers", {})
    existing = handlers.get("/", {})
    if tcp and not tcp.get("HTTPS"):
        raise HostError("Tailscale port 443 is already used by another service")
    approved = {"http://127.0.0.1:8090", "http://127.0.0.1:8091"}
    if target:
        approved.add(target)
    if existing and (existing.get("Proxy") not in approved or set(existing) != {"Proxy"}):
        raise HostError("Tailscale HTTPS root is already used by another service; choose another machine or free that route explicitly")


def unrelated_serve_settings(state, dns):
    """Compare everything except the application's own HTTPS root mapping."""
    value = copy.deepcopy(state)
    web = value.get("Web", {})
    site = web.get(f"{dns}:443", {})
    handlers = site.get("Handlers", {})
    handlers.pop("/", None)
    if not handlers:
        site.pop("Handlers", None)
    if not site:
        web.pop(f"{dns}:443", None)
    tcp = value.get("TCP", {})
    listener = tcp.get("443", {})
    listener.pop("HTTPS", None)
    if not listener:
        tcp.pop("443", None)
    # False entries are equivalent to omitted Funnel entries. True entries
    # are rejected separately, before and after each narrowly scoped change.
    value["AllowFunnel"] = {key: enabled for key, enabled in value.get("AllowFunnel", {}).items() if enabled}
    return {key: item for key, item in value.items() if item}


class Controller:
    def __init__(self, etc="/etc/david-pi", runner=run):
        self.etc = Path(etc)
        self.config_path = self.etc / "installation.json"
        self.state = self.etc / "host-state"
        self.jobs = self.state / "jobs"
        self.compose_path = self.etc / "compose.json"
        self.runner = runner
        self.lock = threading.Lock()
        self.operations_uid = 10002
        for folder in (self.etc, self.state, self.jobs):
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    def recover_jobs(self):
        # A crash never silently retries a migration or restores older content.
        for path in self.jobs.glob("*.json"):
            job = read_json(path)
            if job.get("state") in {"queued", "running"}:
                job.update(state="interrupted", error="Host restarted; review the recorded phase and use local recovery before retrying", finished_at=time.time())
                atomic_json(path, job)

    def config(self):
        value = read_json(self.config_path)
        if value is None:
            raise HostError("Installation has not been configured")
        return validate_installation(value)

    def release(self):
        return read_json(self.etc / "release.json", {})

    def private_origin(self, expected=None):
        status = json.loads(self.runner(["tailscale", "status", "--json"]))
        if status.get("BackendState") != "Running":
            raise HostError("Connect Tailscale on this server before changing private website access")
        dns = status.get("Self", {}).get("DNSName", "").rstrip(".").lower()
        try:
            origin = validate_origin("https://" + dns)
        except InstallationError:
            raise HostError("The connected node needs an assigned Tailscale MagicDNS HTTPS address") from None
        if expected is not None and expected != origin:
            if not self.config_path.exists():
                raise PrivateOriginChanged(f"Tailscale address changed to {origin}. Restore the saved Tailscale account and hostname before resuming setup; a new claim does not approve an address change")
            raise PrivateOriginChanged(f"Tailscale address changed. Explicitly accept the actual address using sudo david-pi reconnect --accept-origin {origin}")
        return origin

    def inspect_private_root(self, origin):
        self.private_origin(origin)
        state = json.loads(self.runner(["tailscale", "serve", "status", "--json"]) or "{}")
        assert_private_serve(state, urllib.parse.urlsplit(origin).hostname)
        return state

    def set_private_root(self, origin, target, before):
        """Recheck ownership immediately before a root-only Serve mutation."""
        if target not in {None, "http://127.0.0.1:8090", "http://127.0.0.1:8091"}:
            raise HostError("Unsupported private website target")
        dns = urllib.parse.urlsplit(origin).hostname
        assert_private_serve(before, dns)
        current = self.inspect_private_root(origin)
        if unrelated_serve_settings(current, dns) != unrelated_serve_settings(before, dns):
            raise HostError("Unrelated Tailscale Serve settings changed; review them locally before retrying")
        command = ["tailscale", "serve", "--bg", "--https=443", "--set-path=/", "--yes"]
        self.runner([*command, target] if target else [*command, "off"])
        after = self.inspect_private_root(origin)
        actual = after.get("Web", {}).get(f"{dns}:443", {}).get("Handlers", {}).get("/", {}).get("Proxy")
        if actual != target or unrelated_serve_settings(after, dns) != unrelated_serve_settings(before, dns):
            raise HostError("Private Serve mapping could not be verified; inspect its local configuration")

    def authorize(self, identity, allow_member=False):
        roles = {"admin", "household"} if allow_member else {"admin"}
        if not isinstance(identity, str) or not identity or not any(m["login"].casefold() == identity.casefold() and m["role"] in roles for m in self.config()["members"]):
            raise HostError("An admitted household member is required" if allow_member else "An admitted household administrator is required")

    def save_config(self, value):
        value = validate_installation(value)
        atomic_json(self.config_path, value, 0o640)
        if os.geteuid() == 0:
            os.chown(self.config_path, 0, 10001)
        return value

    def status(self):
        cfg = self.config()
        values = read_json(self.etc / "secrets/integrations.json", {})
        jobs = sorted((read_json(p) for p in self.jobs.glob("*.json")), key=lambda x: x["created_at"], reverse=True)[:20]
        return {"configuration": cfg, "release": self.release(), "integrations": {key: bool(values.get(key)) for key in SECRET_NAMES}, "jobs": jobs,
                "backup": read_json(self.state / "backup-status.json", {"state": "not_configured" if not cfg["storage"].get("backup_root") else "restore_unverified"}),
                "update_recovery": self.update_snapshot_status()}

    def dispatch(self, operation, payload, identity, local=False):
        if not local:
            self.authorize(identity, allow_member=operation == "metrics_history")
        if not isinstance(payload, dict):
            raise HostError("Expected an object")
        if operation == "metrics_history":
            return self.metrics_history(payload)
        if operation == "status":
            return self.status()
        if operation == "job":
            job_id = payload.get("id", "")
            if not re.fullmatch("[0-9a-f]{32}", job_id):
                raise HostError("Invalid job ID")
            job = read_json(self.jobs / f"{job_id}.json")
            if not job:
                raise HostError("Job not found")
            return job
        if operation == "update_check":
            return self.update_check()
        if operation == "test_integration":
            return self.test_integration(payload)
        if operation not in {"settings", "update", "backup", "restore_test", "repair"} or (operation == "repair" and not local):
            raise HostError("Unsupported management operation")
        if operation == "update" and "testing_version" in payload:
            version = payload["testing_version"]
            if (not local or not isinstance(version, str) or not VERSION.fullmatch(version)
                    or '-beta.' not in version or set(payload) != {"testing_version"}):
                raise HostError("A testing update requires the exact beta version selected locally with sudo david-pi update --version")
        return self.enqueue(operation, payload)

    def metrics_history(self, payload):
        """Expose only selected operational numbers, never the private DB itself."""
        metrics = {"cpu":"cpu", "memory":"memory", "temperature":"temperature", "load":"load1", "hdd":"disk_used", "microsd":"root_disk_used", "swap":"swap", "container_memory":"container_memory", "health_latency":"health_latency", "backup":"backup_state", "queue":"queue_depth"}
        ranges = {"24h":24, "7d":168, "30d":720}
        metric, selected_range = payload.get("metric", "temperature"), payload.get("range", "24h")
        if set(payload) - {"metric", "range"} or not isinstance(metric, str) or not isinstance(selected_range, str) or metric not in metrics or selected_range not in ranges:
            raise HostError("Choose a supported metric and time range")
        column = metrics[metric]
        now = int(time.time())
        descriptors, connection = [], None
        try:
            # Pin every ancestor instead of following an app-controlled symlink
            # between a path check and the privileged SQLite open.
            data = Path(self.config()["storage"]["data_root"])
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(parent)
            for component in (*data.parts[1:], ".david-pi-operations", "maintenance"):
                parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                descriptors.append(parent)
            metadata = os.fstat(parent)
            if metadata.st_uid != self.operations_uid or stat.S_IMODE(metadata.st_mode) & 0o077:
                raise HostError("System history is temporarily unavailable")
            descriptor = os.open("metrics.db", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != self.operations_uid:
                raise HostError("System history is temporarily unavailable")
            connection = sqlite3.connect(f"file:/proc/self/fd/{parent}/metrics.db?mode=ro", uri=True, timeout=2)
            current = os.stat("metrics.db", dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise HostError("System history is temporarily unavailable")
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            def authorizer(action, arg1, arg2, database, trigger):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and database == "main" and arg1 == "system_metrics" and arg2 in {"timestamp", column, ""}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and arg2 == "count":
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            # All identifiers are fixed allowlist values; only times bind as data.
            # Filter SQLite's dynamic types before exposing any value as JSON.
            where = f"timestamp BETWEEN ? AND ? AND {column} IS NOT NULL"
            bounds = (now - ranges[selected_range]*3600, now+60)
            connection.set_authorizer(authorizer)
            count = connection.execute(f"SELECT COUNT(*) FROM system_metrics WHERE {where}", bounds).fetchone()[0]
            step = max(1, (count + 599)//600)
            cursor = connection.execute(f"SELECT timestamp, {column} FROM system_metrics WHERE {where} ORDER BY timestamp", bounds)
            points, last, position = [], None, 0
            for timestamp, value in cursor:
                if isinstance(timestamp, bool) or not isinstance(timestamp, int) or isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value):
                    continue
                point = {"timestamp": timestamp, "value": value}
                if position % step == 0 and len(points) < 600:
                    points.append(point)
                last = point
                position += 1
            if last and (not points or points[-1]["timestamp"] != last["timestamp"]):
                points.append(last)
            return {"metric":metric, "range":selected_range, "points":points, "sampled":step>1}
        except (OSError, sqlite3.Error, ValueError, OverflowError):
            raise HostError("System history is temporarily unavailable") from None
        finally:
            if connection is not None:
                connection.close()
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def acquire_operation(self):
        descriptor = os.open(self.state / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise HostError("Another host operation is already running") from None
        return descriptor

    @contextlib.contextmanager
    def external_operation(self):
        descriptor = self.acquire_operation()
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextlib.contextmanager
    def setup_claim_lock(self):
        # Shared by terminal renewal and browser writes, including install
        # submission. A stale browser session cannot race a renewed token.
        descriptor = os.open(self.state / "setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostError("Setup is being renewed or submitted. Wait for the terminal command to finish, then refresh the wizard") from None
            yield
        finally:
            os.close(descriptor)

    def enqueue(self, operation, payload):
        if not self.lock.acquire(blocking=False):
            raise HostError("Another host operation is already running")
        try:
            operation_fd = self.acquire_operation()
        except Exception:
            self.lock.release()
            raise
        job = {"id": uuid.uuid4().hex, "operation": operation, "state": "queued", "phase": "waiting", "created_at": time.time()}
        try:
            atomic_json(self.jobs / f"{job['id']}.json", job)
        except Exception:
            fcntl.flock(operation_fd, fcntl.LOCK_UN)
            os.close(operation_fd)
            self.lock.release()
            raise
        def work():
            try:
                self.phase(job, "starting")
                result = getattr(self, operation)(payload, job)
                job.update(state="complete", phase="complete", result=result, finished_at=time.time())
            except Exception as error:
                job.update(state="failed", error=str(error) if isinstance(error, (HostError, InstallationError)) else f"{type(error).__name__}: operation failed; inspect local service logs", finished_at=time.time())
            finally:
                try:
                    atomic_json(self.jobs / f"{job['id']}.json", job)
                finally:
                    fcntl.flock(operation_fd, fcntl.LOCK_UN)
                    os.close(operation_fd)
                    self.lock.release()
            if job.get("state") == "complete" and job.get("result", {}).get("restart_helper"):
                # The journal is durable before the process running this code exits.
                self.runner(["systemctl", "--no-block", "restart", "david-pi-helper.service"])
        threading.Thread(target=work, daemon=True).start()
        return {"job_id": job["id"]}

    def phase(self, job, phase):
        job.update(state="running", phase=phase, updated_at=time.time())
        atomic_json(self.jobs / f"{job['id']}.json", job)

    def prepare_secrets(self, values):
        if not isinstance(values, dict) or set(values) - SECRET_NAMES:
            raise HostError("Unsupported secret setting")
        current = read_json(self.etc / "secrets/integrations.json", {})
        for key, value in values.items():
            if not isinstance(value, str) or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
                raise HostError("Invalid provider credential")
            current[key] = value
        return current

    def secret_update(self, values):
        current = self.prepare_secrets(values)
        atomic_json(self.etc / "secrets/integrations.json", current)
        return current

    def test_integration(self, payload):
        name = payload.get("name")
        current = read_json(self.etc / "secrets/integrations.json", {})
        supplied = payload.get("credential")
        if supplied is not None and (not isinstance(supplied, str) or len(supplied) > 4096 or any(ord(c) < 32 for c in supplied)):
            raise HostError("Invalid provider credential")
        try:
            if name == "movies":
                token = supplied if supplied is not None else current.get("TMDB_API_READ_TOKEN", "")
                if not token:
                    raise HostError("Enter a TMDB API Read Access Token, or choose manual watchlist")
                data = json.loads(read_https("https://api.themoviedb.org/3/authentication", headers={"Authorization": f"Bearer {token}"}))
                if not data.get("success"):
                    raise HostError("TMDB did not accept this credential")
            elif name == "recipes":
                token = supplied if supplied is not None else current.get("THEMEALDB_API_KEY", "")
                if not token or token == "1" or not re.fullmatch("[A-Za-z0-9_-]{1,256}", token):
                    raise HostError("Enter your licensed TheMealDB key; the demonstration key cannot be distributed")
                data = json.loads(read_https(f"https://www.themealdb.com/api/json/v1/{token}/search.php?s=chicken"))
                if not isinstance(data.get("meals"), list):
                    raise HostError("TheMealDB did not accept this credential")
            else:
                raise HostError("This integration is configured locally; no remote credential test is available")
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise HostError("Provider rejected the credential; check the account and key") from None
            if error.code == 429:
                raise HostError("Provider rate limit reached; retry later. Local content remains available") from None
            raise HostError("Provider is temporarily unavailable. Local content remains available") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise HostError("Could not reach the provider. Check connectivity or configure it later") from None
        return {"connected": True}

    def inspect_storage(self, path):
        folder = safe_path(path)
        info = json.loads(self.runner(["findmnt", "--json", "--target", str(folder), "--output", "SOURCE,FSTYPE,UUID,TARGET,FSROOT"]))["filesystems"][0]
        if info["fstype"] != "ext4":
            raise HostError("Initial releases support local ext4 storage only")
        if not info.get("uuid") or not info.get("source", "").startswith("/dev/"):
            raise HostError("Storage must be a mounted local ext4 filesystem")
        # systemd's ReadWritePaths exposes /srv (and other writable folders)
        # as same-path subdirectory mounts, e.g. /dev/vda1[/srv] at /srv.
        # Resolve only that identity-preserving view of the system filesystem;
        # a bind from another folder or device must not become a drive choice.
        source = re.fullmatch(r"(/dev/[^\[\]]+)\[(/[^\[\]]*)\]", info["source"])
        if source and info.get("fsroot") == info.get("target") == source.group(2):
            root = json.loads(self.runner(["findmnt", "--json", "--target", "/", "--output", "SOURCE,FSTYPE,UUID,TARGET,FSROOT"]))["filesystems"][0]
            if (root.get("target") == root.get("fsroot") == "/" and root.get("fstype") == "ext4"
                    and root.get("source") == source.group(1) and root.get("uuid") == info["uuid"]):
                info = {**info, "source": root["source"], "target": "/", "fsroot": "/"}
        return info

    def storage_devices(self):
        """Identify ordinary disks and partitions; omit ambiguous stacked devices."""
        value = json.loads(self.runner(["lsblk", "--json", "--paths", "--output", "NAME,TYPE,PKNAME,MAJ:MIN,SERIAL,WWN"]))
        nodes = {}
        def collect(items):
            for item in items:
                nodes[item["name"]] = item
                collect(item.get("children", []))
        collect(value.get("blockdevices", []))
        result = {}
        for name, item in nodes.items():
            if item.get("type") == "disk":
                disk = item
            elif item.get("type") == "part" and nodes.get(item.get("pkname"), {}).get("type") == "disk":
                disk = nodes[item["pkname"]]
            else:
                continue
            identity = {key: disk.get(key) for key in ("name", "maj:min", "serial", "wwn")}
            result[name] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return result

    def storage_choices(self):
        """Read current mounted storage without creating folders or changing disks."""
        result = {"choices": []}
        try:
            mounts = json.loads(self.runner(["findmnt", "--json", "--list", "--output", "SOURCE,FSTYPE,UUID,TARGET,OPTIONS,FSROOT,LABEL"]))["filesystems"]
            devices = self.storage_devices()
            roots = {(mount.get("source"), mount.get("uuid")) for mount in mounts if mount.get("target") == "/"}
            system_device = devices.get(next(iter(roots))[0]) if len(roots) == 1 else None
            # ReadWritePaths may stack whole-filesystem views at the SAME
            # target. Count distinct paths: genuine aliases at different paths
            # remain ambiguous and must not appear as separate drive choices.
            targets = {}
            for mount in mounts:
                key = (mount.get("source"), mount.get("uuid"))
                if mount.get("fsroot") == "/":
                    targets.setdefault(key, set()).add(mount.get("target"))
            seen = set()
            for mount in mounts:
                source, target = mount.get("source", ""), mount.get("target", "")
                options = set(mount.get("options", "").split(","))
                if (mount.get("fstype") != "ext4" or not mount.get("uuid") or source not in devices
                        or mount.get("fsroot") != "/" or "rw" not in options or options & {"ro", "bind", "rbind"}
                        or len(targets[(source, mount["uuid"])]) != 1):
                    continue
                parent, mode = ("/srv", "folder") if target == "/" else (target, "drive")
                if mode == "drive" and any(c.isspace() for c in parent):
                    continue
                try:
                    folder = safe_path(parent)
                    if not os.access(folder, os.W_OK | os.X_OK) or os.statvfs(folder).f_flag & os.ST_RDONLY:
                        continue
                    current = self.inspect_storage(folder)
                    if any(current.get(key) != mount.get(key) for key in ("source", "uuid", "target")):
                        continue
                    capacity = shutil.disk_usage(folder)
                except (HostError, OSError):
                    continue
                if parent in seen:
                    continue
                seen.add(parent)
                device_id = devices[source]
                identity = [source, mount["uuid"], target, parent, device_id]
                label = re.sub(r"[\x00-\x1f\x7f]", "", str(mount.get("label") or "")).strip()[:100]
                label = ("System drive folder" if target == "/" else label or "Prepared ext4 drive") + f" — {parent}"
                result["choices"].append({"id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(), "label": label,
                    "parent": parent, "mode": mode, "free_bytes": capacity.free, "total_bytes": capacity.total,
                    "device_id": device_id, "system_disk": device_id == system_device})
            result["choices"].sort(key=lambda choice: (choice["system_disk"], choice["parent"]))
            if not result["choices"]:
                result["warning"] = "No supported writable ext4 storage was detected. Mount a prepared local ext4 drive, refresh this page, or choose an existing folder under Advanced."
        except (HostError, OSError, ValueError, KeyError, TypeError):
            result = {"choices": [], "warning": "Storage discovery is unavailable. Check local storage, refresh this page, or enter an existing folder under Advanced."}
        return result

    def validate_storage_selection(self, cfg, selection):
        if selection is None:
            return
        if not isinstance(selection, dict) or set(selection) - {"data", "backup"}:
            raise HostError("Invalid storage selection; refresh the available storage choices")
        if not selection:
            return
        choices = {choice["id"]: choice for choice in self.storage_choices()["choices"]}
        selected = {}
        for kind, identifier in selection.items():
            if not isinstance(identifier, str) or identifier not in choices:
                raise HostError("A selected drive is missing or has changed. Refresh storage choices and select it again")
            choice = choices[identifier]
            expected = str(Path(choice["parent"]) / ("david-pi-data" if kind == "data" else "david-pi-backups"))
            field = "data_root" if kind == "data" else "backup_root"
            if cfg["storage"].get(field) != expected or (kind == "data" and cfg["storage"]["mode"] != choice["mode"]):
                raise HostError("The selected drive and storage folder do not match. Select the drive again")
            if choice["free_bytes"] < 1024**3:
                raise HostError("Selected storage needs at least 1 GiB free. Free space or choose another drive")
            selected[kind] = choice
        if len(selected) == 2 and selected["data"]["device_id"] == selected["backup"]["device_id"]:
            raise HostError("Backup and primary storage use the same physical drive")

    def latest_install_job(self):
        jobs = [read_json(path, {}) for path in self.jobs.glob("*.json")]
        return max((job for job in jobs if job.get("operation") == "install"), key=lambda job: job.get("created_at", 0), default=None)

    def provision_storage(self, cfg):
        data = safe_path(cfg["storage"]["data_root"], exists=False)
        parent = data.parent
        info = self.inspect_storage(parent)
        if data.exists() and any(data.iterdir()) and not (data / ".david-pi-storage").is_file():
            raise HostError("Selected data folder contains unrelated files; choose a dedicated empty folder")
        # Reject an unsafe backup before any fstab or application folder changes.
        self.validate_backup_storage(cfg)
        if cfg["storage"]["mode"] == "drive":
            if info["target"] == "/" or parent != Path(info["target"]):
                raise HostError("Drive storage must be a prepared, mounted ext4 filesystem; choose its mount point")
            fstab = Path("/etc/fstab").read_text()
            line = f"UUID={info['uuid']} {parent} ext4 defaults,nofail,x-systemd.device-timeout=15s 0 2"
            if re.search(r"\s" + re.escape(str(parent)) + r"\s", fstab) is None:
                if any(c.isspace() for c in str(parent)):
                    raise HostError("Prepared drive mount points cannot contain spaces")
                shutil.copy2("/etc/fstab", self.state / "fstab.before")
                with open("/etc/fstab", "a") as stream:
                    stream.write("\n# David-Pi prepared filesystem (no formatting)\n" + line + "\n")
        self.provision_backup(cfg)
        existing_sentinel = data / ".david-pi-storage"
        if existing_sentinel.exists() and existing_sentinel.read_text().strip() != cfg["instance_id"]:
            raise HostError("Existing storage belongs to another installation; use explicit recovery")
        self.create_data_directories(cfg)
        existing_sentinel.write_text(cfg["instance_id"] + "\n")
        os.chmod(existing_sentinel, 0o640)
        if os.geteuid() == 0:
            os.chown(existing_sentinel, 0, 10001)
        atomic_json(self.state / "storage.json", {"uuid": info["uuid"], "data_root": str(data)})

    def validate_backup_storage(self, cfg):
        info = self.inspect_storage(Path(cfg["storage"]["data_root"]).parent)
        backup = cfg["storage"].get("backup_root")
        if backup:
            backup_path = safe_path(backup, exists=False)
            backup_info = self.inspect_storage(backup_path.parent)
            if backup_info["uuid"] == info["uuid"]:
                raise HostError("Independent backup storage must use a different filesystem")
            # Distinct partitions on one device are not independent backups.
            primary_parent = (self.runner(["lsblk", "-ndo", "PKNAME", info["source"]]).strip() or info["source"]).removeprefix("/dev/")
            backup_parent = (self.runner(["lsblk", "-ndo", "PKNAME", backup_info["source"]]).strip() or backup_info["source"]).removeprefix("/dev/")
            if primary_parent == backup_parent:
                raise HostError("Backup and primary storage use the same physical drive")
            if backup_path.exists() and any(backup_path.iterdir()) and read_json(backup_path / ".david-pi-backup", {}).get("instance_id") != cfg["instance_id"]:
                raise HostError("Backup folder contains unrelated files or another installation")
            return backup_path

    def provision_backup(self, cfg):
        backup_path = self.validate_backup_storage(cfg)
        if backup_path is not None:
            backup_path.mkdir(mode=0o700, exist_ok=True)
            atomic_json(backup_path / ".david-pi-backup", {"instance_id": cfg["instance_id"]})

    def create_data_directories(self, cfg):
        data = Path(cfg["storage"]["data_root"])
        marker = data / ".david-pi-storage"
        established = marker.exists() or marker.is_symlink()
        if established and (marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != cfg["instance_id"]):
            raise HostError("Existing storage belongs to another installation; use explicit recovery")

        def managed_directory(path, uid, mode):
            if path.is_symlink():
                raise HostError("Managed storage contains a symbolic link")
            created = not path.exists()
            if created:
                path.mkdir(mode=mode)
            if not path.is_dir():
                raise HostError("Managed storage directory is not a directory")
            if not created and not established:
                return
            descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if created and os.geteuid() == 0:
                    os.fchown(descriptor, uid, 10001)
                metadata = os.fstat(descriptor)
                expected = (uid, 10001) if os.geteuid() == 0 else (os.geteuid(), os.getegid())
                if (metadata.st_uid, metadata.st_gid) != expected:
                    raise HostError("Managed storage directory has unexpected ownership; use explicit recovery")
                # The helper runs with UMask=0077. mkdir's mode is filtered by
                # that mask; workers still need the intended group traversal.
                # Repair only named directories belonging to this installation.
                os.fchmod(descriptor, mode)
            finally:
                os.close(descriptor)

        folders = ["", "incoming", "tmp/uploads", "tmp/runtime", "quarantine", "platform", ".david-pi-operations"]
        for spec in selected_modules(cfg).values():
            folders.extend(spec.get("storage", ()))
        for spec in selected_substrates(cfg).values():
            folders.extend(spec["directories"])
        for name in folders:
            path = data / name
            # Touch only owned application directories, never a selected parent's ownership.
            for component in reversed([path, *path.parents]):
                if component != data and data not in component.parents:
                    continue
                managed_directory(component, 10001, 0o750)
        for spec in selected_substrates(cfg).values():
            for name in spec.get("databases", ()):
                database = data / name
                if not database.exists():
                    with sqlite3.connect(database):
                        pass
                    os.chmod(database, 0o640)
                    if os.geteuid() == 0:
                        os.chown(database, 10001, 10001)
        maintenance = data / ".david-pi-operations/maintenance"
        managed_directory(maintenance, 10002, 0o700)

    def storage_guard(self):
        cfg = self.config()
        path = safe_path(cfg["storage"]["data_root"])
        marker = path / ".david-pi-storage"
        record = read_json(self.state / "storage.json", {})
        if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != cfg["instance_id"]:
            raise HostError("Required application storage is missing or has the wrong identity")
        info = self.inspect_storage(path)
        if record.get("uuid") != info["uuid"]:
            raise HostError("Storage filesystem changed; refusing to start against a replacement directory")

    def provision_secrets(self, cfg):
        directory = self.etc / "secrets"
        directory.mkdir(mode=0o700, exist_ok=True)
        chat = directory / "chat-master.key"
        if not chat.exists():
            chat.write_text(base64.b64encode(secrets.token_bytes(32)).decode() + "\n")
        os.chmod(chat, 0o440)
        if os.geteuid() == 0:
            os.chown(chat, 0, 10001)
        if cfg["integrations"].get("web_push"):
            key = directory / "chat-vapid-private.pem"
            if not key.exists():
                self.runner(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)])
            os.chmod(key, 0o440)
            if os.geteuid() == 0:
                os.chown(key, 0, 10001)
            # OpenSSL DER is binary: use a direct checked binary invocation.
            result = subprocess.run(["openssl", "ec", "-in", str(key), "-pubout", "-outform", "DER"], check=True, capture_output=True)
            atomic_json(self.etc / "vapid-public.json", {"key": base64.urlsafe_b64encode(result.stdout[-65:]).rstrip(b"=").decode()}, 0o644)

    def compose(self, cfg, image):
        if not IMAGE.fullmatch(image):
            raise HostError("An immutable released container image is required")
        data = cfg["storage"]["data_root"]
        workers = selected_workers(cfg)
        def bind(source, target, ro=False):
            return {"type": "bind", "source": str(source), "target": target, "read_only": ro, "bind": {"create_host_path": False}}
        config = bind(self.config_path, "/etc/david-pi/installation.json", True)
        environment = {"DAVID_PI_CONFIG_FILE": "/etc/david-pi/installation.json", "DAVID_PI_ACCESS_MODE": "enforce", "DAVID_PI_DATA_SENTINEL": "/data/.david-pi-storage", "DAVID_PI_DATA_ID": cfg["instance_id"], "DAVID_PI_PLATFORM_DATA": "/data/platform", "DAVID_PI_FILES_DATA": "/data/files", "DAVID_PI_AUDIOBOOKS_DATA": "/data/audiobooks", "DAVID_PI_MYTUBE_DATA": "/data/mytube", "DAVID_PI_CHAT_DATA": "/data/chat", "DAVID_PI_ASSISTANT_DATA": "/data/platform/assistant", "DAVID_PI_PUBLIC_URL": cfg["public_url"], "DAVID_PI_INSTANCE_NAME": cfg["display_name"], "DAVID_PI_ALLOWED_HOSTS": urllib.parse.urlsplit(cfg["public_url"]).hostname + ",127.0.0.1,localhost", "MOVIE_REGION": cfg["country"], "TZ": cfg["timezone"], "DAVID_PI_DISABLE_METRICS": "1", "DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING": "1", "ASSISTANT_WINDOWS_ENABLED": "false", "ASSISTANT_UBUNTU_BROKER_ENABLED": "false", "ASSISTANT_OPENCODE_ENABLED": "false", "DAVID_PI_SLIDESHOW_EXECUTOR_MODE": "queue", "TMPDIR": "/data/tmp/uploads"}
        # Upload admission and derivative preparation must budget the same reserve.
        environment["DAVID_PI_AUDIOBOOK_PREPARE_MIN_FREE"] = "1073741824"
        def service(memory=256, cpus="0.5"):
            return {"image": image, "restart": "unless-stopped", "user": "10001:10001", "volumes": [bind(data, "/data"), config], "environment": dict(environment), "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "read_only": True, "tmpfs": ["/tmp:size=64m,mode=1777", "/run:size=16m,mode=0755"], "mem_limit": f"{memory}m", "pids_limit": 128, "cpus": cpus, "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}, "stop_grace_period": "45s"}
        portal = service(1536, "2.0")
        portal.update(ports=["127.0.0.1:8090:8000"], env_file=[str(self.etc / "runtime.env")], healthcheck={"test": ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=10).read()"], "interval": "15s", "timeout": "20s", "retries": 5, "start_period": "90s"})
        portal["volumes"] += [bind("/run/david-pi-helper", "/run/david-pi-helper", True), bind("/run/david-pi", "/run/david-pi", True), bind(self.etc / "secrets/chat-master.key", "/run/secrets/chat-master.key", True)]
        portal["environment"]["DAVID_PI_CHAT_KEY_FILE"] = "/run/secrets/chat-master.key"
        services = {"portal": portal}
        if "chat-notifier" in workers:
            worker = service(128, "0.25")
            worker["volumes"] += [bind(self.etc / "secrets/chat-vapid-private.pem", "/run/secrets/chat-vapid-private.pem", True)]
            worker["environment"].update(DAVID_PI_VAPID_PRIVATE_KEY_FILE="/run/secrets/chat-vapid-private.pem", DAVID_PI_VAPID_SUBJECT=cfg["public_url"])
            services["chat-notifier"] = worker
            portal["environment"]["DAVID_PI_VAPID_PUBLIC_KEY"] = read_json(self.etc / "vapid-public.json", {}).get("key", "")
        for name in ("device-backup", "slideshow"):
            if name not in workers:
                continue
            worker = service(768 if name == "slideshow" else 192, "1.0" if name == "slideshow" else "0.35")
            worker.update(network_mode="none", depends_on={"portal": {"condition": "service_healthy"}})
            worker["environment"].update(DAVID_PI_WORKER_MODE=name, DAVID_PI_SLIDESHOW_EXECUTOR_MODE="worker", DAVID_PI_SLIDESHOW_RUNTIME="/run/david-pi-slideshow", DAVID_PI_DEVICE_BACKUP_RUNTIME="/run/david-pi-device-backup")
            worker["tmpfs"].append(f"/run/david-pi-{name}:size=8m,mode=0700,uid=10001,gid=10001")
            services[name] = worker
        if "audiobook-preparer" in workers:
            worker = service()
            worker.update(network_mode="none")
            worker["volumes"] = [config, bind(Path(data)/"audiobooks/originals", "/data/audiobooks/originals", True), bind(Path(data)/"audiobooks/streaming", "/data/audiobooks/streaming"), bind(Path(data)/"audiobooks/incoming/streaming", "/data/audiobooks/incoming/streaming"), bind(Path(data)/".david-pi-operations/audiobook", "/audiobook-state"), bind(Path(data)/".david-pi-storage", "/run/david-pi-storage-sentinel", True)]
            worker["environment"].update(DAVID_PI_WORKER_MODE="audiobook", DAVID_PI_DATA_SENTINEL="/run/david-pi-storage-sentinel", DAVID_PI_AUDIOBOOK_DERIVATIVE_STATE="/audiobook-state")
            services["audiobook-preparer"] = worker
        if "mytube-preparer" in workers:
            worker = service(1024, "1.0")
            worker.update(network_mode="none", depends_on={"portal": {"condition": "service_healthy"}})
            worker["volumes"] = [config, bind(Path(data)/"mytube", "/data/mytube"), bind(Path(data)/"platform", "/data/platform"), bind(Path(data)/"originals", "/data/originals", True), bind(Path(data)/"photos.db", "/data/photos.db", True), bind(Path(data)/".david-pi-storage", "/run/david-pi-storage-sentinel", True), bind("/proc", "/host/proc", True), bind("/sys", "/host/sys", True)]
            worker["environment"].update(DAVID_PI_WORKER_MODE="mytube", DAVID_PI_DATA_SENTINEL="/run/david-pi-storage-sentinel", DAVID_PI_MYTUBE_MEDIA_DB="/data/photos.db", DAVID_PI_MYTUBE_MEDIA_ORIGINALS="/data/originals", DAVID_PI_MYTUBE_PREPARE_MIN_STORAGE="1073741824")
            services["mytube-preparer"] = worker
        # Retention remains off. The separate uid owns only its operational state.
        worker = service(96, "0.20")
        worker.update(user="10002:10001", network_mode="none")
        worker["volumes"] = [config, bind(data, "/data", True), bind(Path(data)/".david-pi-operations/maintenance", "/maintenance-state"), bind("/run/david-pi", "/run/david-pi", True)]
        worker["environment"].update(DAVID_PI_WORKER_MODE="maintenance", DAVID_PI_MAINTENANCE_UID="10002", DAVID_PI_MAINTENANCE_STATE="/maintenance-state", DAVID_PI_MAINTENANCE_ANCHOR="/data/.david-pi-operations/maintenance", DAVID_PI_MAINTENANCE_RUNTIME="/run/david-pi-maintenance", DAVID_PI_MAINTENANCE_PHOTO_RETENTION_MODE="off", DAVID_PI_MAINTENANCE_NOTE_RETENTION_MODE="off", DAVID_PI_MAINTENANCE_UPLOAD_PARTS_MODE="off")
        worker["tmpfs"].append("/run/david-pi-maintenance:size=2m,mode=0700,uid=10002,gid=10001")
        services["maintenance"] = worker
        for name, spec in workers.items():
            services[name]["command"] = ["python", "-m", spec["entrypoint"]]
            if spec["healthcheck"]:
                services[name]["healthcheck"] = {"test": ["CMD", "python", "-m", spec["entrypoint"], "--check-health"], "interval":"15s", "timeout":"20s", "retries":5, "start_period":"90s"}
        return {"name": "david-pi", "services": services}

    def write_runtime(self, cfg, image):
        # Never create bind sources under an unmounted or replaced drive.
        self.storage_guard()
        self.create_data_directories(cfg)
        self.provision_secrets(cfg)
        credentials = read_json(self.etc / "secrets/integrations.json", {})
        # Single-quoted Compose env values are literal, including dollars.
        lines = []
        for key, value in credentials.items():
            if key in SECRET_NAMES:
                lines.append(key + "='" + value.replace("'", "\\'") + "'")
        (self.etc / "runtime.env").write_text("\n".join(lines) + "\n")
        os.chmod(self.etc / "runtime.env", 0o600)
        atomic_json(self.compose_path, self.compose(cfg, image))
        self.runner(["python3", str(ROOT / "scripts/collect_server_status.py"), "--etc", str(self.etc)])

    def docker(self, *args):
        return self.runner(["docker", "compose", "--project-name", "david-pi", "-f", str(self.compose_path), *args], timeout=900)

    def readiness(self):
        self.storage_guard()
        rows = self.docker("ps", "--format", "json")
        try:
            services = json.loads(rows)
            if isinstance(services, dict):
                services = [services]
        except json.JSONDecodeError:
            services = [json.loads(line) for line in rows.splitlines() if line]
        expected = set(read_json(self.compose_path)["services"])
        running = {s["Service"] for s in services if s.get("State") == "running" and s.get("Health", "") in {"", "healthy"}}
        if not expected <= running:
            raise HostError("Application readiness failed; one or more selected services are unavailable")
        self.runner(["python3", str(ROOT / "scripts/collect_server_status.py"), "--etc", str(self.etc)])
        try:
            with urllib.request.urlopen("http://127.0.0.1:8090/ready", timeout=10) as response:
                state = json.load(response)
            if not state.get("ok"):
                raise HostError("Application readiness checks did not pass")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            raise HostError("Application readiness checks did not pass; review local service status") from None
        return {"ready": True, "services": sorted(running)}

    def settings(self, payload, job):
        old = self.config()
        updated = copy.deepcopy(old)
        changes = payload.get("configuration", {})
        if not isinstance(changes, dict) or set(changes) - {"display_name", "timezone", "country", "members", "modules", "integrations", "storage"}:
            raise HostError("Only names, household members, locale, modules and integrations can change online. Storage/address changes require local recovery")
        if "storage" in changes:
            incoming_storage = changes["storage"]
            if not isinstance(incoming_storage, dict) or set(incoming_storage) - {"backup_root", "data_root", "mode", "update_snapshot_root"}:
                raise HostError("Invalid backup storage setting")
            if incoming_storage.get("data_root", old["storage"]["data_root"]) != old["storage"]["data_root"] or incoming_storage.get("mode", old["storage"]["mode"]) != old["storage"]["mode"]:
                raise HostError("Application storage changes require local recovery")
            changes = {**changes, "storage": {**old["storage"], **incoming_storage}}
        updated.update(changes)
        updated = validate_installation(updated)
        supplied = payload.get("secrets", {})
        # Validate credentials before stopping the running stack.
        credentials = self.prepare_secrets(supplied)
        if updated["modules"].get("pihole") == "enabled" and old["modules"].get("pihole") != "enabled":
            raise HostError("Pi-hole changes require the deliberate local DNS setup command")
        self.storage_guard()
        backup_changed = updated["storage"].get("backup_root") != old["storage"].get("backup_root")
        if backup_changed:
            self.provision_backup(updated)
        if updated["storage"].get("update_snapshot_root") != old["storage"].get("update_snapshot_root"):
            self.update_snapshot_storage(updated, create=True)
        credentials_path = self.etc / "secrets/integrations.json"
        previous_credentials = read_json(credentials_path)
        backup_status_path = self.state / "backup-status.json"
        previous_backup_status = read_json(backup_status_path)
        pihole_disabled = old["modules"].get("pihole") == "enabled" and updated["modules"].get("pihole") == "disabled"
        self.phase(job, "saving configuration")
        try:
            self.docker("stop")
            atomic_json(credentials_path, credentials)
            self.save_config(updated)
            if pihole_disabled:
                self.runner(["systemctl", "disable", "--now", "david-pi-pihole-summary.timer"])
            self.write_runtime(updated, self.release()["image"])
            self.phase(job, "checking selected services")
            self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
            result = self.readiness()
            if backup_changed:
                atomic_json(backup_status_path, {"state": "restore_unverified" if updated["storage"].get("backup_root") else "not_configured"})
            return result
        except Exception:
            # Keep all content and newly generated local keys. Revert the
            # configuration and provider credentials as one failed operation.
            self.docker("stop")
            self.save_config(old)
            for path, value in ((credentials_path, previous_credentials), (backup_status_path, previous_backup_status)):
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_json(path, value)
            self.write_runtime(old, self.release()["image"])
            if pihole_disabled:
                self.runner(["systemctl", "enable", "--now", "david-pi-pihole-summary.timer"])
            self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
            raise

    def update_snapshot_storage(self, cfg=None, *, create=False):
        cfg = cfg or self.config()
        data = Path(cfg["storage"]["data_root"])
        root = Path(cfg["storage"].get("update_snapshot_root") or data.parent / ("david-pi-recovery-" + cfg["instance_id"]))
        root = safe_path(root, exists=False)
        for parent in (root.parent, *root.parent.parents):
            if parent.stat().st_uid in {10001, 10002}:
                raise HostError("Update recovery storage cannot be inside a directory controlled by an application worker")
        if stat.S_IMODE(root.parent.stat().st_mode) & 0o022:
            raise HostError("Choose an update recovery parent folder that is not writable by a group or other users")
        for other in (data, cfg["storage"].get("backup_root")):
            if other and (root.is_relative_to(other) or Path(other).is_relative_to(root)):
                raise HostError("Update recovery storage must be outside the library and independent backup folders")
        info = self.inspect_storage(root.parent)
        binding_path = self.state / "update-storage.json"
        bindings = read_json(binding_path, {})
        if str(root) in bindings and bindings[str(root)] != info["uuid"]:
            raise HostError("The configured update recovery drive is missing or has changed; reconnect its original filesystem")
        marker = root / ".david-pi-update-recovery"
        expected = {"instance_id": cfg["instance_id"], "uuid": info["uuid"]}
        if root.exists():
            try:
                update_storage.private_directory(root)
            except update_storage.SnapshotError as error:
                raise HostError(str(error)) from None
            if marker.is_symlink() or read_json(marker, {}) != expected:
                raise HostError("Update recovery folder belongs to another installation or its drive has changed")
        elif create:
            root.mkdir(mode=0o700)
            atomic_json(marker, expected)
        if create:
            atomic_json(binding_path, {**bindings, str(root): info["uuid"]})
        return root

    def update_snapshot_status(self):
        cfg = self.config()
        data = Path(cfg["storage"]["data_root"])
        path = Path(cfg["storage"].get("update_snapshot_root") or data.parent / ("david-pi-recovery-" + cfg["instance_id"]))
        result = {"path": str(path), "default": not cfg["storage"].get("update_snapshot_root"),
                  "independent_backup": False, "snapshots": [], "retention": "Latest successful update plus failed or interrupted attempts"}
        try:
            root = self.update_snapshot_storage()
            result["free_bytes"] = shutil.disk_usage(root if root.exists() else root.parent).free
            if root.exists():
                result["snapshots"] = [{"id": item.name, "created_at": value["created_at"], "complete": True,
                    "version": value.get("release", {}).get("version"), "copied_bytes": value.get("copied_bytes"),
                    "reused_bytes": value.get("reused_bytes"),
                    "job_state": read_json(self.jobs / (item.name + ".json"), {}).get("state", "unknown")}
                    for item, value in update_storage.records(root, cfg["instance_id"])]
                result["snapshots"].extend({"id": item.name, "complete": False,
                    "created_at": job.get("created_at"), "job_state": job["state"]}
                    for item, job in update_storage.incomplete_records(root, self.jobs))
        except (HostError, update_storage.SnapshotError, OSError, ValueError) as error:
            result["error"] = str(error)
        return result

    def remove_update_snapshot(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch("[0-9a-f]{32}", identifier):
            raise HostError("Choose the exact update snapshot ID shown by update-snapshots")
        root = self.update_snapshot_storage()
        try:
            choices = {p.name: p for p, _ in update_storage.records(root, self.config()["instance_id"])}
            choices.update({p.name: p for p, _ in update_storage.incomplete_records(root, self.jobs)})
        except update_storage.SnapshotError as error:
            raise HostError(str(error)) from None
        target = choices.get(identifier)
        if target is None:
            raise HostError("Snapshot not found or its incomplete copy is not a recorded failed/interrupted update; review it locally")
        if any(p.is_symlink() for p in target.rglob("*")):
            raise HostError("Update snapshot contains links; review it locally")
        shutil.rmtree(target)
        return {"removed": identifier, "content_preserved": True, "message": "Only the selected local update snapshot was removed"}

    def snapshot(self, destination, job, independent=False):
        cfg = self.config()
        self.storage_guard()
        source = Path(cfg["storage"]["data_root"])
        destination = Path(destination)
        if destination.is_relative_to(source) or source.is_relative_to(destination):
            raise HostError("A recovery snapshot must be outside the live application tree")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(destination.parent).free < 1024**3:
            raise HostError("Recovery storage needs at least 1 GiB free before snapshot preparation")
        self.phase(job, "stopping writes for consistent snapshot")
        try:
            self.docker("stop")
            self.phase(job, "checking recovery space and reusable snapshot files")
            previous = None
            if not independent:
                candidates = update_storage.records(destination.parent, cfg["instance_id"])
                previous = candidates[0] if candidates else None
            reusable, copied_bytes, reused_bytes = update_storage.copy_plan(source, previous)
            metadata_bytes = sum(path.stat().st_size for path in (self.etc / "secrets").rglob("*") if path.is_file())
            metadata_bytes += sum((self.etc / name).stat().st_size for name in ("installation.json", "compose.json", "release.json", "runtime.env"))
            # Reserve another database-sized copy for SQLite's consistent backup
            # and 1 GiB for manifests, journal state and the filesystem.
            database_bytes = 0
            for path in source.rglob("*"):
                if path.is_file() and path.suffix in {".db", ".sqlite", ".sqlite3"}:
                    database_bytes += path.stat().st_size
                    wal = path.with_name(path.name + "-wal")
                    # Committed pages can exist only in WAL after an unclean
                    # stop. SQLite's temporary backup includes those pages while
                    # the copied WAL still occupies space beside it.
                    if wal.is_file():
                        database_bytes += wal.stat().st_size
            required = copied_bytes + metadata_bytes + database_bytes + 1024**3
            if shutil.disk_usage(destination.parent).free < required:
                raise HostError(f"Update/backup recovery storage needs {required} free bytes for new snapshot content and a 1 GiB reserve. Free space or choose another prepared local update recovery folder in Settings; existing snapshots were preserved")
            self.phase(job, "copying application data and recovery keys")
            destination.mkdir(mode=0o700)
            # Refuse symlinks: backups must not read outside managed storage.
            if any(p.is_symlink() for p in source.rglob("*")):
                raise HostError("Managed storage contains symbolic links; review before backup")
            ownership = {}
            for original in [source, *source.rglob("*")]:
                metadata = original.stat()
                ownership[str(original.relative_to(source))] = [metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)]
            shutil.copytree(source, destination / "data", copy_function=update_storage.copy_function(reusable), symlinks=True)
            if any(p.is_symlink() for p in (destination / "data").rglob("*")):
                raise HostError("Storage changed during backup; no symlinks are accepted")
            # SQLite's backup API incorporates any committed WAL frames into
            # a self-contained database even after an unclean prior shutdown.
            for original in source.rglob("*"):
                if original.is_file() and original.suffix in {".db", ".sqlite", ".sqlite3"} and original.stat().st_size:
                    target = destination / "data" / original.relative_to(source)
                    temporary = target.with_name(target.name + ".snapshot")
                    with sqlite3.connect(f"file:{urllib.parse.quote(str(original))}?mode=ro", uri=True) as database, sqlite3.connect(temporary) as copied:
                        database.backup(copied)
                    os.replace(temporary, target)
                    for suffix in ("-wal", "-shm"):
                        target.with_name(target.name+suffix).unlink(missing_ok=True)
            shutil.copytree(self.etc / "secrets", destination / "secrets")
            shutil.copy2(self.state / "storage.json", destination / "storage.json")
            for name in ("installation.json", "compose.json", "release.json", "runtime.env"):
                shutil.copy2(self.etc / name, destination / name)
            atomic_json(destination / "host-job.json", job)
            hashes = {}
            for path in destination.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        hashes[str(path.relative_to(destination))] = hashlib.file_digest(stream, "sha256").hexdigest()
            atomic_json(destination / "snapshot.json", {"instance_id": cfg["instance_id"], "created_at": time.time(), "release": self.release(), "independent": independent, "files": hashes, "data_ownership": ownership, "complete": True, "copied_bytes": copied_bytes, "reused_bytes": reused_bytes})
        except Exception as error:
            self.docker("up", "-d")
            if isinstance(error, update_storage.SnapshotError):
                raise HostError(str(error)) from None
            raise
        return destination

    def backup(self, payload, job):
        cfg = self.config()
        destination = cfg["storage"].get("backup_root")
        if not destination:
            raise HostError("Independent backup is not configured; choose prepared backup storage first")
        root = safe_path(destination)
        marker = read_json(root / ".david-pi-backup", {})
        if marker.get("instance_id") != cfg["instance_id"]:
            raise HostError("Independent backup drive is missing or has the wrong identity")
        if self.inspect_storage(root)["uuid"] == self.inspect_storage(cfg["storage"]["data_root"])["uuid"]:
            raise HostError("Backup storage is not independent")
        path = root / job["id"]
        try:
            self.snapshot(path, job, independent=True)
        finally:
            self.docker("up", "-d")
        atomic_json(self.state / "backup-status.json", {"state": "restore_unverified", "snapshot_id": path.name, "completed_at": time.time()})
        return {"snapshot_id": path.name, "restore_verified": False}

    def restore_test(self, payload, job):
        cfg = self.config()
        root = cfg["storage"].get("backup_root")
        if not root:
            raise HostError("Independent backup is not configured")
        identifier = payload.get("snapshot_id") or read_json(self.state / "backup-status.json", {}).get("snapshot_id", "")
        if not re.fullmatch("[0-9a-f]{32}", identifier):
            raise HostError("Choose a completed snapshot")
        source = Path(root) / identifier
        manifest = read_json(source / "snapshot.json", {})
        if not manifest.get("complete") or manifest.get("instance_id") != cfg["instance_id"]:
            raise HostError("Snapshot is incomplete or belongs to another installation")
        self.phase(job, "verifying snapshot hashes and SQLite contents")
        actual_files = {str(p.relative_to(source)) for p in source.rglob("*") if p.is_file() and p != source / "snapshot.json"}
        if actual_files != set(manifest["files"]):
            raise HostError("Snapshot file inventory does not match its manifest")
        for name, expected in manifest["files"].items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or (source/path).is_symlink():
                raise HostError("Unsafe snapshot path")
            if any(p.is_symlink() for p in (source/path).parents):
                raise HostError("Unsafe snapshot parent")
            with (source/path).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise HostError("Snapshot content checksum mismatch")
            if name.startswith("data/") and path.suffix in {".db", ".sqlite", ".sqlite3"}:
                with sqlite3.connect(f"file:{urllib.parse.quote(str(source/path))}?mode=ro&immutable=1", uri=True) as db:
                    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise HostError("Snapshot database integrity check failed")
        # Honest evidence: application boot in a clean host is a separate gate.
        result = {"state": "integrity_verified", "snapshot_id": identifier, "checked_at": time.time(), "files_verified": len(manifest["files"]), "clean_host_restore_verified": False}
        atomic_json(self.state / "backup-status.json", result)
        return result

    def update_check(self, testing_version=None):
        release = self.release()
        repository = release.get("repository", "")
        if not REPOSITORY.fullmatch(repository):
            raise HostError("No verified release repository is configured")
        base = f"https://github.com/{repository}/releases/latest/download"
        if testing_version is not None:
            if not isinstance(testing_version, str) or not VERSION.fullmatch(testing_version) or '-beta.' not in testing_version:
                raise HostError("Choose an exact published testing version such as 10.0.0-beta.1")
            base = f"https://github.com/{repository}/releases/download/v{testing_version}"
        installed = release.get("version", "")
        if not VERSION.fullmatch(installed):
            raise HostError("Installed release version is invalid; review saved release metadata locally")
        try:
            manifest = parse_manifest(read_https(base + "/release-manifest.txt", limit=4096).decode(), repository, allow_prerelease=testing_version is not None)
        except HostError:
            if '-beta.' not in installed or testing_version is not None:
                raise
            return {"current": installed, "available": None, "update_available": False,
                    "message": "The latest stable release metadata is not compatible with this testing build. No update was applied. Use an explicitly selected beta's published instructions, or check again after the portable stable release is published."}
        if testing_version is not None and manifest["VERSION"] != testing_version:
            raise HostError("Testing release manifest does not match the explicitly selected version")
        def ordering(version):
            core, _, beta = version.partition('-beta.')
            return (*map(int, core.split('.')), 0 if beta else 1, int(beta or '0'))
        available = ordering(manifest["VERSION"]) > ordering(installed)
        result = {"current": installed, "available": manifest["VERSION"], "update_available": available, "manifest": manifest}
        if testing_version is None and '-beta.' in installed and not available:
            result["message"] = "You are using a testing release. No newer stable release is available. Another beta must be explicitly selected using its published testing instructions."
        return result

    def update(self, payload, job):
        cfg = self.config()
        serve_state = self.inspect_private_root(cfg["public_url"])
        candidate = self.update_check(payload["testing_version"]) if "testing_version" in payload else self.update_check()
        if not candidate["update_available"]:
            raise HostError("The selected release is not newer than the installed version; downgrades require local recovery review")
        manifest = candidate["manifest"]
        old = self.release()
        next_schema = int(manifest["DATA_SCHEMA_VERSION"])
        minimum = int(manifest["ROLLBACK_MIN_DATA_SCHEMA"])
        if old["data_schema_version"] < minimum or next_schema < old["data_schema_version"]:
            raise HostError("Release requires a separately rehearsed data migration")
        self.phase(job, "downloading explicitly selected testing release" if "testing_version" in payload else "downloading verified stable release")
        stage = self.state / "staging" / job["id"]
        stage.mkdir(parents=True, mode=0o700)
        archive = stage / manifest["ARCHIVE"]
        base = f"https://github.com/{old['repository']}/releases/download/v{manifest['VERSION']}"
        # Stream and cap download rather than keeping an archive in memory.
        with https_open(base + "/" + manifest["ARCHIVE"], timeout=60) as response, archive.open("wb") as output:
            if not response.url.startswith("https://"):
                raise HostError("Insecure release redirect rejected")
            copied = 0
            while chunk := response.read(1024*1024):
                copied += len(chunk)
                if copied > 2 * 1024**3:
                    raise HostError("Release archive is too large")
                output.write(chunk)
        with archive.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != manifest["ARCHIVE_SHA256"]:
                raise HostError("Release archive checksum mismatch")
        validate_archive(archive, stage / "source")
        candidate_root = stage / "source" / f"david-pi-{manifest['VERSION']}"
        if (candidate_root / "VERSION").read_text().strip() != manifest["VERSION"]:
            raise HostError("Release archive version mismatch")
        self.runner(["docker", "pull", manifest["IMAGE"]], timeout=900)
        self.storage_guard()
        snapshot_root = self.update_snapshot_storage(create=True)
        snapshot_path = snapshot_root / job["id"]
        self.snapshot(snapshot_path, job)
        candidate_release = {"version": manifest["VERSION"], "image": manifest["IMAGE"], "repository": old["repository"], "data_schema_version": next_schema, "rollback_min_data_schema": minimum}
        activated = None
        runtime_changed = False
        try:
            self.phase(job, "applying release with writes paused")
            runtime_changed = True
            self.write_runtime(cfg, manifest["IMAGE"])
            atomic_json(self.etc / "release.json", candidate_release)
            # Do not reconnect Serve until all services pass readiness. Tailscale
            # continues to the maintenance page while startup migrations run.
            self.set_private_root(cfg["public_url"], "http://127.0.0.1:8091", serve_state)
            self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
            self.readiness()
            self.phase(job, "activating verified host management code")
            activated = self.activate_release(candidate_root, job["id"])
            self.phase(job, "reopening private website")
            self.set_private_root(cfg["public_url"], "http://127.0.0.1:8090", serve_state)
            atomic_json(self.state / "last-update.json", {"snapshot": str(snapshot_path), "previous": old, "current": candidate_release, "reopened_at": time.time(), "rollback_requires_local_review": True})
            # Cleanup failure cannot roll back a healthy reopened installation.
            cleanup_warning = None
            try:
                update_storage.prune_successful(snapshot_root, cfg["instance_id"], job["id"], self.jobs)
            except (OSError, ValueError) as error:
                cleanup_warning = f"Update succeeded; older recovery snapshots need local review: {error}"
            return {"version": manifest["VERSION"], "recovery_snapshot": job["id"], "restart_helper": True,
                    **({"warning": cleanup_warning} if cleanup_warning else {})}
        except Exception:
            self.docker("stop")
            if activated:
                self.rollback_activation(activated)
            if not runtime_changed or next_schema == old["data_schema_version"]:
                # Same data schema: restart the old image on current data. Never
                # restore a snapshot over potentially newer data automatically.
                if runtime_changed:
                    self.write_runtime(cfg, old["image"])
                    atomic_json(self.etc / "release.json", old)
                self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
                self.set_private_root(cfg["public_url"], "http://127.0.0.1:8090", serve_state)
            raise HostError("Update failed; snapshot retained. Compatible previous image restarted when possible; use local recovery if the website remains unavailable") from None

    def activate_release(self, candidate, identifier):
        # A single atomic symlink replacement leaves executable host code at
        # the fixed service path even if power fails during activation.
        installed = INSTALL_ROOT
        releases = installed.parent / "david-pi-releases"
        if not installed.is_symlink() or not installed.resolve().is_relative_to(releases):
            raise HostError("Host release layout requires a separately rehearsed migration")
        for required in ("david-pi", "installer/host.py", "installer/systemd/david-pi-helper.service", "installer/systemd/david-pi-portal.service"):
            if not (candidate / required).is_file():
                raise HostError("Release is missing host management files")
        staged = releases / identifier
        previous = installed.resolve()
        shutil.copytree(candidate, staged)
        os.chmod(staged / "david-pi", 0o755)
        atomic_json(self.state / ("activation-"+identifier+".json"), {"previous":str(previous), "current":str(staged)})
        pointer = installed.parent / (".david-pi-next-" + identifier)
        pointer.symlink_to(staged)
        os.replace(pointer, installed)
        try:
            self.install_current_units()
        except Exception:
            self.rollback_activation(identifier)
            raise
        return identifier

    def install_current_units(self):
        for name in ("david-pi-helper.service", "david-pi-portal.service", "david-pi-status.service", "david-pi-status.timer"):
            shutil.copy2(INSTALL_ROOT / "installer/systemd" / name, SYSTEMD_ROOT / name)
        self.runner(["systemctl", "daemon-reload"])

    def rollback_activation(self, identifier):
        record = read_json(self.state / ("activation-"+identifier+".json"), {})
        previous = Path(record.get("previous", "/nonexistent"))
        if not previous.is_relative_to(INSTALL_ROOT.parent / "david-pi-releases") or not previous.is_dir():
            raise HostError("Previous host release is unavailable; local recovery is required")
        pointer = INSTALL_ROOT.parent / (".david-pi-rollback-" + identifier)
        pointer.symlink_to(previous)
        os.replace(pointer, INSTALL_ROOT)
        self.install_current_units()

    def restore(self, snapshot, data_root):
        """Local-only clean-host recovery from the verified release bootstrap."""
        return recovery.restore(self, sys.modules[__name__], snapshot, data_root)

    def pihole_connect(self, database):
        cfg = self.config()
        self.storage_guard()
        path = Path(database)
        if not path.is_absolute() or path.name != "pihole-FTL.db" or any(p.is_symlink() for p in [path,*path.parents]) or not path.is_file():
            raise HostError("Choose the existing local Pi-hole pihole-FTL.db database without symbolic links")
        with sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=3) as connection:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(queries)")}
            if not {"timestamp", "status", "client"} <= columns:
                raise HostError("This Pi-hole query database schema is not supported")
        atomic_json(self.etc / "pihole.json", {"database":str(path),"integration":"existing-local"})
        cfg["modules"]["pihole"] = "enabled"
        self.save_config(cfg)
        self.pihole_summary()
        self.write_runtime(cfg, self.release()["image"])
        self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
        return {"connected": True, "dns_changed": False}

    def pihole_summary(self):
        cfg = self.config()
        summary = {"enabled": False, "total":0, "blocked":0, "blocked_percent":0, "clients":0, "window":"Unavailable", "updated_at":dt.datetime.now(dt.timezone.utc).isoformat(), "stale":True}
        if cfg["modules"].get("pihole") == "enabled":
            path = Path(read_json(self.etc / "pihole.json", {}).get("database", "/nonexistent"))
            if path.is_file() and not path.is_symlink():
                with sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True, timeout=3) as connection:
                    since = int(time.time())-86400
                    total = connection.execute("SELECT COUNT(*) FROM queries WHERE timestamp>=?",(since,)).fetchone()[0]
                    blocked = connection.execute("SELECT COUNT(*) FROM queries WHERE timestamp>=? AND status IN (1,4,5,6,7,8,9,10,11,15,16,18)",(since,)).fetchone()[0]
                    clients = connection.execute("SELECT COUNT(DISTINCT client) FROM queries WHERE timestamp>=?",(since,)).fetchone()[0]
                summary.update(enabled=True,total=total,blocked=blocked,blocked_percent=round(100*blocked/total,1) if total else 0,clients=clients,window="Last 24 hours",stale=False)
        atomic_json(Path("/run/david-pi/pihole-summary.json"), summary, 0o644)
        return summary

    def repair(self, payload, job):
        self.storage_guard()
        cfg = self.config()
        serve_state = self.inspect_private_root(cfg["public_url"])
        self.phase(job, "recreating selected services")
        self.write_runtime(cfg, self.release()["image"])
        self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
        # A repaired stack must also start its previously failed oneshot unit.
        # Its normal `up --wait` rechecks startup without recreating containers.
        self.runner(["systemctl", "enable", "--now", "david-pi-portal.service"])
        result = self.readiness()
        self.runner(["systemctl", "enable", "--now", "david-pi-status.timer"])
        self.set_private_root(cfg["public_url"], "http://127.0.0.1:8090", serve_state)
        atomic_json(self.state / "installed.json", {"completed_at": time.time(), "instance_id": cfg["instance_id"]})
        (self.state / "pending-install.json").unlink(missing_ok=True)
        return result

    def reconnect(self, accepted_origin):
        """Local terminal only: accept the node's existing HTTPS origin."""
        old = self.config()
        origin = self.private_origin()
        dns = urllib.parse.urlsplit(origin).hostname
        if accepted_origin != origin:
            raise HostError(f"Explicitly accept this node's exact current address with --accept-origin {origin}")
        before = self.inspect_private_root(origin)
        self.storage_guard()
        updated = copy.deepcopy(old)
        # Record the already assigned name; never rename the Tailscale node.
        updated.update(public_url=origin, hostname=dns.split(".")[0])
        updated = validate_installation(updated)
        previous_target = before.get("Web", {}).get(f"{dns}:443", {}).get("Handlers", {}).get("/", {}).get("Proxy")
        job = {"id": uuid.uuid4().hex, "operation": "reconnect", "state": "running", "phase": "checking private address", "created_at": time.time()}
        journal = self.jobs / f"{job['id']}.json"
        atomic_json(journal, job)
        atomic_json(self.state / f"reconnect-{job['id']}.json", {"previous_configuration": old, "accepted_configuration": updated, "previous_serve": before})

        changed = False
        try:
            self.phase(job, "pausing the website for address reconnection")
            changed = True
            self.set_private_root(origin, "http://127.0.0.1:8091", before)
            self.docker("stop")
            self.save_config(updated)
            self.write_runtime(updated, self.release()["image"])
            self.phase(job, "checking services at the accepted address")
            self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
            self.readiness()
            self.set_private_root(origin, "http://127.0.0.1:8090", before)
            self.runner(["python3", str(ROOT / "scripts/collect_server_status.py"), "--etc", str(self.etc)])
            result = {"public_url": origin, "instance_id": old["instance_id"], "reconnected": True, "next": "Open the new private address. On each Android device, approve this address and reconnect to the same installation."}
            job.update(state="complete", phase="complete", result=result, finished_at=time.time())
            atomic_json(journal, job)
            return result
        except Exception:
            recovered = False
            if changed:
                try:
                    self.phase(job, "restoring the previous address configuration")
                    self.docker("stop")
                    self.save_config(old)
                    self.write_runtime(old, self.release()["image"])
                    self.docker("up", "-d", "--remove-orphans", "--force-recreate", "--wait", "--wait-timeout", "180")
                    self.readiness()
                    self.set_private_root(origin, previous_target, before)
                    recovered = True
                except Exception:
                    pass
            message = ("Reconnect failed; previous application configuration and root mapping restored. The old address may no longer resolve; inspect the saved job and retry with the current address."
                       if recovered else "Reconnect failed and recovery needs local review. Inspect the saved job and Tailscale Serve settings before retrying; household data was not restored or deleted.")
            job.update(state="failed", error=message, finished_at=time.time(), configuration_recovered=recovered)
            atomic_json(journal, job)
            raise HostError(message) from None

    def install(self, payload, job):
        if self.config_path.exists():
            raise HostError("Already configured; use administrator settings or local recovery")
        cfg = validate_installation(payload["configuration"])
        setup = read_json(self.state / "setup.json", {})
        if not any(m["login"].casefold() == setup.get("admin", "").casefold() and m["role"] == "admin" for m in cfg["members"]):
            raise HostError("Initial administrator must match the verified claimant")
        serve_state = self.inspect_private_root(cfg["public_url"])
        atomic_json(self.state / "pending-install.json", cfg)
        self.phase(job, "preparing selected storage")
        self.validate_storage_selection(cfg, payload.get("storage_selection"))
        self.provision_storage(cfg)
        self.phase(job, "saving local keys and module settings")
        self.secret_update(payload.get("secrets", {}))
        self.save_config(cfg)
        self.write_runtime(cfg, self.release()["image"])
        self.phase(job, "starting selected services")
        self.runner(["systemctl", "enable", "--now", "david-pi-status.timer"])
        # The oneshot unit starts Compose and waits for health. Recreating the
        # same services here would discard that successful startup and repeat it.
        self.runner(["systemctl", "enable", "--now", "david-pi-portal.service"])
        self.phase(job, "checking selected services")
        self.readiness()
        self.phase(job, "opening your home server")
        self.set_private_root(cfg["public_url"], "http://127.0.0.1:8090", serve_state)
        atomic_json(self.state / "installed.json", {"completed_at": time.time(), "instance_id": cfg["instance_id"]})
        (self.state / "pending-install.json").unlink(missing_ok=True)
        return {"public_url": cfg["public_url"], "backup": "restore_unverified" if cfg["storage"].get("backup_root") else "not_configured"}


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        try:
            if uid not in {0, 10001}:
                raise HostError("Peer is not the application service")
            line = self.rfile.readline(65537)
            if len(line) > 65536 or not line.endswith(b"\n"):
                raise HostError("Management request is too large")
            message = json.loads(line)
            result = self.server.controller.dispatch(message.get("operation"), message.get("payload", {}), message.get("identity", ""), local=uid == 0)
            response = {"ok": True, "result": result}
        except (HostError, ValueError, KeyError) as error:
            response = {"ok": False, "error": str(error)}
        except Exception:
            response = {"ok": False, "error": "Host management is unavailable; inspect the local service log"}
        self.wfile.write(json.dumps(response).encode() + b"\n")


class SetupHandler(BaseHTTPRequestHandler):
    """Loopback only; Serve removes caller-provided identity headers."""
    server_version = "DavidPiSetup"
    def log_message(self, format, *args):
        # Tokens, credentials and private identity headers never enter logs.
        pass

    @property
    def controller(self):
        return self.server.controller

    def send(self, code, value, content_type="application/json", cookie=None):
        data = json.dumps(value).encode() if content_type == "application/json" else value
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def state(self):
        value = read_json(self.controller.state / "setup.json") or read_json(self.controller.state / "recovery.json", {})
        origin = value.get("origin")
        if self.headers.get("Host") != urllib.parse.urlsplit(origin or "").netloc:
            raise HostError("Invalid private setup address")
        identity = self.headers.get("Tailscale-User-Login", "").casefold()
        if not identity or identity != value.get("admin", "").casefold():
            correction = "" if value.get("mode") == "recovery" else ". If the initial login was mistyped, run sudo david-pi renew-claim --admin LOGIN on the server before installation starts"
            raise HostError("Use the Tailscale account chosen in the terminal to open setup" + correction)
        return value

    def session(self, state):
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        provided = cookie.get("dp_setup")
        raw = provided.value if provided else ""
        if not state.get("session_hash") or state.get("session_expires", 0) < time.time() or not hmac.compare_digest(hashlib.sha256(raw.encode()).hexdigest(), state["session_hash"]):
            raise HostError("Setup session expired. Run sudo david-pi setup to claim a new session")
        return raw

    def do_GET(self):
        try:
            state = self.state()
            if state.get("mode") == "recovery":
                if self.controller.config_path.exists():
                    return self.send(503, b"<!doctype html><meta name=viewport content='width=device-width, initial-scale=1'><title>Home maintenance</title><h1>Home maintenance</h1><p>Your saved household is undergoing setup or maintenance. In the server terminal, use <code>sudo david-pi status</code> to follow progress. If setup stopped, use <code>sudo david-pi repair</code>.</p><p>Do not restore an older backup over this installation. The home page returns after service verification.</p>", "text/html; charset=utf-8")
                return self.send(503, b"<!doctype html><meta name=viewport content='width=device-width, initial-scale=1'><title>Home recovery</title><h1>Home recovery</h1><p>This replacement server is prepared for recovery. Continue in its terminal with <code>sudo david-pi restore</code>, then <code>sudo david-pi repair</code>.</p><p>Your private home page opens after restored services pass verification. Recovery keeps the saved household identity and keys.</p>", "text/html; charset=utf-8")
            # After installation this endpoint is a maintenance page only.
            path = urllib.parse.urlsplit(self.path).path
            if self.controller.config_path.exists() and (self.controller.state / "installed.json").exists() and path == "/":
                return self.send(503, b"<!doctype html><title>Server maintenance</title><h1>Server maintenance</h1><p>An administrator is applying an update. Please try again shortly.</p>", "text/html; charset=utf-8")
            if path in {"/", "/setup.js", "/setup.css"}:
                name = {"/": "index.html", "/setup.js": "setup.js", "/setup.css": "setup.css"}[path]
                mime = {"/": "text/html; charset=utf-8", "/setup.js": "text/javascript", "/setup.css": "text/css"}[path]
                return self.send(200, (ROOT / "installer/web" / name).read_bytes(), mime)
            self.session(state)
            if path == "/api/setup":
                return self.send(200, {"admin": state["admin"], "origin": state["origin"], "hostname": state["hostname"], "csrf": state["csrf"], "instance_id": state["instance_id"], "modules": list(MODULES), "timezone": state.get("timezone", "UTC"),
                    "timezones": sorted(available_timezones()), "storage": self.controller.storage_choices(), "active_job": self.controller.latest_install_job()})
            if path == "/api/job":
                identifier = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [""])[0]
                return self.send(200, self.controller.dispatch("job", {"id": identifier}, state["admin"], local=True))
            self.send(404, {"error": "Not found"})
        except HostError as error:
            self.send(403, {"error": str(error)})

    def do_POST(self):
        try:
            with self.controller.setup_claim_lock():
                self.setup_post()
        except (HostError, ValueError, KeyError) as error:
            self.send(400, {"error": str(error)})
        except Exception:
            self.send(500, {"error": "Setup could not finish this step; inspect the local helper log"})

    def setup_post(self):
        try:
            state = self.state()
            if state.get("mode") == "recovery":
                raise HostError("Recovery is controlled from the server terminal; a new-home claim is unavailable")
            if self.headers.get("Origin") != state["origin"]:
                raise HostError("Same-origin setup requests are required")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise HostError("JSON is required")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise HostError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise HostError("Expected an object")
            if self.path == "/api/claim":
                # Serialize claim consumption so concurrent requests cannot reuse it.
                with self.server.claim_lock:
                    state = self.state()
                    token = payload.get("token", "")
                    if not isinstance(token, str) or state.get("expires", 0) < time.time() or state.get("claimed") or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), state.get("token_hash", "")):
                        raise HostError("Claim token expired, already used, or incorrect. Run sudo david-pi setup for a new token")
                    session = secrets.token_urlsafe(32)
                    state.update(claimed=True, session_hash=hashlib.sha256(session.encode()).hexdigest(), session_expires=time.time()+3600, csrf=secrets.token_urlsafe(32))
                    state.pop("token_hash", None)
                    atomic_json(self.controller.state / "setup.json", state)
                return self.send(200, {"claimed": True}, cookie=f"dp_setup={session}; Secure; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600")
            self.session(state)
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), state.get("csrf", "")):
                raise HostError("Refresh setup before submitting this request")
            if self.controller.config_path.exists():
                raise HostError("Setup has completed; use administrator settings")
            if self.path == "/api/test-integration":
                return self.send(200, self.controller.test_integration(payload))
            if self.path == "/api/install":
                cfg = validate_installation(payload.get("configuration", {}))
                if cfg["instance_id"] != state["instance_id"] or cfg["public_url"] != state["origin"] or cfg["hostname"] != state["hostname"]:
                    raise HostError("Installation identity and address cannot be supplied by the browser")
                if cfg["modules"].get("pihole") == "enabled":
                    raise HostError("Finish setup, then run sudo david-pi pihole-setup for deliberate DNS configuration")
                self.controller.validate_storage_selection(cfg, payload.get("storage_selection"))
                return self.send(202, self.controller.enqueue("install", payload))
            self.send(404, {"error": "Not found"})
        except (HostError, ValueError, KeyError) as error:
            self.send(400, {"error": str(error)})
        except Exception:
            self.send(500, {"error": "Setup could not finish this step; inspect the local helper log"})


def serve(controller):
    controller.recover_jobs()
    run_dir = Path("/run/david-pi-helper")
    run_dir.mkdir(mode=0o750, exist_ok=True)
    os.chown(run_dir, 0, 10001)
    path = run_dir / "control.sock"
    path.unlink(missing_ok=True)
    control = ControlServer(str(path), ControlHandler)
    control.controller = controller
    os.chown(path, 0, 10001)
    os.chmod(path, 0o660)
    web = ThreadingHTTPServer(("127.0.0.1", 8091), SetupHandler)
    web.controller = controller
    web.claim_lock = threading.Lock()
    threading.Thread(target=control.serve_forever, daemon=True).start()
    web.serve_forever()


def print_setup_claim(origin, admin, token, *, renewed=False, previous_admin=None):
    if renewed:
        if previous_admin is not None and previous_admin != admin:
            print(f"\nThe intended administrator is now {admin}. They must still sign in to Tailscale with that account and claim this server.\nThe server's Tailscale account, private address and installation identity are unchanged.")
        else:
            print("\nYour setup claim has been renewed. Your saved account, hostname and installation identity are unchanged.")
        print("The previous code and browser session no longer work. Refresh the wizard and use this new code.\nIf you had not submitted the form, enter those browser choices again.")
    print(f"\n3. Open your private setup wizard\n   {origin}/\n   Connect this browser's computer or phone to Tailscale using {admin}.\n   The link opens this server's setup wizard; it is different from the Tailscale sign-in link.\n\n4. Claim your home server\n   One-use claim token (valid 15 minutes):\n   {token}\n   Paste the token into the wizard. Keep it private; it never belongs in a URL.\n\nBookmark {origin}/ — this same link opens your home page after setup.\nFind this link again: sudo david-pi address\nIf the claim token expires: sudo david-pi setup\nIf the initial login was mistyped (before installation starts):\n   sudo david-pi renew-claim --admin LOGIN\nKeep the server powered on while the wizard shows its installation progress.")


def setup_node_account(status):
    """Use the local daemon's node owner, never a browser-supplied identity."""
    user_id = status.get("Self", {}).get("UserID")
    user = status.get("User", {}).get(str(user_id), {})
    login = user.get("LoginName", "")
    return login.casefold() if isinstance(login, str) else ""


def renew_claim(controller, admin=None):
    corrected_admin = admin
    if corrected_admin is not None:
        if not isinstance(corrected_admin, str) or len(corrected_admin) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+", corrected_admin):
            raise HostError("Enter the intended administrator's exact Tailscale login with sudo david-pi renew-claim --admin LOGIN")
        corrected_admin = corrected_admin.casefold()
    with controller.external_operation(), controller.setup_claim_lock():
        if controller.config_path.exists() or (controller.state / "installed.json").exists():
            raise HostError("Installation has already begun. Run sudo david-pi setup to resume its saved installation, or sudo david-pi status to review progress")
        for path in controller.jobs.glob("*.json"):
            if read_json(path, {}).get("state") in {"queued", "running"}:
                raise HostError("An installation operation is still recorded as running. Use sudo david-pi status and wait for it to finish; no claim was changed")
        try:
            previous = read_json(controller.state / "setup.json")
            if not isinstance(previous, dict):
                raise ValueError()
            admin, origin = previous["admin"], validate_origin(previous["origin"])
            expected_account = previous.get("node_account", admin)
            if not isinstance(expected_account, str) or not expected_account:
                raise ValueError()
            expected_account = expected_account.casefold()
            for field in ("node_id", "tailnet"):
                if field in previous and (not isinstance(previous[field], str) or not previous[field]):
                    raise ValueError()
            if not isinstance(admin, str) or len(admin) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+", admin):
                raise ValueError()
            if previous["hostname"] != urllib.parse.urlsplit(origin).hostname.split(".")[0]:
                raise ValueError()
            if str(uuid.UUID(previous["instance_id"])) != previous["instance_id"] or previous.get("timezone", "UTC") not in available_timezones():
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HostError("Saved setup choices cannot be read safely. Inspect /etc/david-pi/host-state/setup.json locally or seek support; do not delete configuration to generate a new code") from None
        saved_release_arguments(controller.etc)
        actual_origin = controller.private_origin()
        if actual_origin != origin:
            raise HostError(f"The server's Tailscale address changed. The saved setup address is {origin}. Restore its original Tailscale account and hostname before retrying sudo david-pi setup. A deliberate move needs a reviewed setup recovery; renewal does not approve a replacement address")
        status = json.loads(controller.runner(["tailscale", "status", "--json"]))
        if not expected_account or setup_node_account(status) != expected_account:
            raise HostError("The server's connected Tailscale account does not match saved setup. Switch the server back to its original account, then run sudo david-pi setup; no claim was changed")
        def same_node(current):
            return (
                ("node_id" not in previous or current.get("Self", {}).get("ID") == previous["node_id"])
                and ("tailnet" not in previous or current.get("CurrentTailnet", {}).get("Name") == previous["tailnet"])
            )
        if not same_node(status):
            raise HostError("The server's Tailscale node or network changed. Restore the original connection before renewing setup; no claim was changed")
        serve_state = controller.inspect_private_root(origin)
        # Keep a running helper and its sessions until all connection checks
        # pass. Renewal does not restart it or reinstall prerequisites.
        controller.runner(["systemctl", "enable", "--now", "david-pi-helper.service"])
        controller.runner(["systemctl", "is-active", "--quiet", "david-pi-helper.service"])
        try:
            controller.set_private_root(origin, "http://127.0.0.1:8091", serve_state)
        except HostError as error:
            raise HostError(f"{error}. Confirm HTTPS Certificates are enabled at https://console.tailscale.com/admin/dns, then run sudo david-pi setup again; no new claim was issued") from None
        current = json.loads(controller.runner(["tailscale", "status", "--json"]))
        if setup_node_account(current) != expected_account or controller.private_origin() != origin or not same_node(current):
            raise HostError("Tailscale changed while renewing setup. Restore the saved account and address, then retry; no new claim was issued")
        token = secrets.token_urlsafe(32)
        renewed = {key: value for key, value in previous.items() if key not in {"token_hash", "expires", "claimed", "session_hash", "session_expires", "csrf"}}
        renewed.update(token_hash=hashlib.sha256(token.encode()).hexdigest(), expires=time.time()+900, claimed=False, node_account=expected_account)
        if corrected_admin is not None:
            renewed["admin"] = corrected_admin
        atomic_json(controller.state / "setup.json", renewed)
        print_setup_claim(origin, renewed["admin"], token, renewed=True, previous_admin=admin)


def fresh_setup_private_root(controller, hostname, initial_status):
    """Discover a completed rename before binding any installation identity.

    `tailscale set` updates local preferences before the control plane assigns
    DNS (including collision suffixes). Retrying that handoff is allowed only
    inside fresh initialization, for the same node, owner and tailnet.
    """
    def context(status):
        own = status.get("Self", {})
        dns = own.get("DNSName", "").rstrip(".").lower()
        return (own.get("ID"), own.get("UserID"), setup_node_account(status),
                dns.partition(".")[2], status.get("CurrentTailnet", {}).get("Name"))

    expected = context(initial_status)
    for attempt in range(30):
        if controller.config_path.exists() or (controller.state / "setup.json").exists():
            raise HostError("Setup identity is already saved. Use sudo david-pi setup; fresh address discovery cannot replace an existing claim")
        status = json.loads(controller.runner(["tailscale", "status", "--json"]))
        if status.get("BackendState") != "Running" or context(status) != expected:
            raise HostError("The Tailscale node, account or network changed during setup. Restore the original connection before retrying; no claim was issued")
        origin = validate_origin("https://" + status.get("Self", {}).get("DNSName", "").rstrip(".").lower())
        label = urllib.parse.urlsplit(origin).hostname.split(".")[0]
        stem, separator, suffix = label.rpartition("-")
        # Tailscale may truncate a maximal-length label to append its numeric
        # collision suffix. Never accept the still-cached original OS hostname.
        renamed = label == hostname or (separator and suffix.isdigit() and stem == hostname[:63-len(suffix)-1])
        if renamed:
            try:
                before = controller.inspect_private_root(origin)
                controller.set_private_root(origin, "http://127.0.0.1:8091", before)
                final_status = json.loads(controller.runner(["tailscale", "status", "--json"]))
                if final_status.get("BackendState") != "Running" or context(final_status) != expected:
                    raise HostError("The Tailscale node, account or network changed during setup. Restore the original connection before retrying; no claim was issued")
                controller.private_origin(origin)
                return origin, final_status, before
            except PrivateOriginChanged:
                # No setup identity or token exists yet. Inspect and preserve
                # Serve again using the newly assigned name on the next pass.
                pass
        if attempt < 29:
            time.sleep(1)
    raise HostError("Tailscale has not finished assigning the server's hostname. Wait for it to connect, then run sudo david-pi setup again; no claim was issued")


def initialize(controller, admin, hostname, image, repository):
    if controller.config_path.exists():
        print("Already configured. Use the private website's administrator settings or sudo david-pi status.")
        print_address(controller.etc)
        return
    if (controller.state / "setup.json").exists():
        raise HostError("Setup choices are already saved. Run sudo david-pi setup without options to renew the existing claim")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+", admin) or len(admin) > 254:
        raise HostError("Enter the exact Tailscale account login (usually an email address)")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
        raise HostError("Hostname must be a lowercase DNS label")
    if not IMAGE.fullmatch(image) or not REPOSITORY.fullmatch(repository) or not image.startswith(f"ghcr.io/{repository.lower()}@"):
        raise HostError("Setup requires a verified immutable release image and repository")
    status = json.loads(controller.runner(["tailscale", "status", "--json"]))
    if status.get("BackendState") != "Running":
        raise HostError("Complete tailscale up in the terminal before opening setup")
    dns = status.get("Self", {}).get("DNSName", "").rstrip(".")
    state = json.loads(controller.runner(["tailscale", "serve", "status", "--json"]) or "{}")
    assert_private_serve(state, dns)
    if not dns.endswith(".ts.net"):
        raise HostError("Tailscale MagicDNS and HTTPS are required")
    if hostname != dns.split(".")[0] and state.get("Web"):
        raise HostError("This Tailscale node already serves other content. Use its existing hostname to preserve those addresses")
    # Preserve all unrelated options by using `set`, never `up --reset`.
    controller.runner(["tailscale", "set", "--hostname", hostname])
    print(f"\n2. Enable private HTTPS in your Tailscale network\n   Open https://console.tailscale.com/admin/dns using account {admin}.\n   Under HTTPS Certificates, choose Enable HTTPS and review the confirmation.\n   If certificates are already enabled, continue. Keep this terminal open.", flush=True)
    if sys.stdin.isatty():
        input("   Press Enter after HTTPS Certificates is enabled: ")
    # The HTTPS checkpoint can take minutes. Resolve the assigned name only
    # after it, and verify private Serve before saving an immutable claim.
    print("\nChecking the assigned private address...", flush=True)
    controller.runner(["systemctl", "enable", "david-pi-helper.service"])
    controller.runner(["systemctl", "restart", "david-pi-helper.service"])
    try:
        origin, assigned_status, serve_state = fresh_setup_private_root(controller, hostname, status)
    except HostError as error:
        raise HostError(f"{error}. If HTTPS Certificates are not enabled, enable them at https://console.tailscale.com/admin/dns using {admin}. Then resume with sudo david-pi setup") from None
    dns = urllib.parse.urlsplit(origin).hostname
    actual_hostname = dns.split(".")[0]
    try:
        timezone = Path("/etc/timezone").read_text().strip()
    except FileNotFoundError:
        timezone = "UTC"
    setup = {"admin": admin.casefold(), "hostname": actual_hostname, "origin": f"https://{dns}", "instance_id": str(uuid.uuid4()), "timezone": timezone, "expires": 0, "claimed": False}
    if account := setup_node_account(assigned_status):
        setup["node_account"] = account
    if node_id := assigned_status.get("Self", {}).get("ID"):
        setup["node_id"] = node_id
    if tailnet := assigned_status.get("CurrentTailnet", {}).get("Name"):
        setup["tailnet"] = tailnet
    atomic_json(controller.state / "setup.json", setup)
    atomic_json(controller.state / "tailscale-serve.before.json", serve_state)
    atomic_json(controller.etc / "release.json", {"version": (ROOT / "VERSION").read_text().strip(), "image": image, "repository": repository, "data_schema_version": 1, "rollback_min_data_schema": 1})
    token = secrets.token_urlsafe(32)
    setup.update(expires=time.time()+900, token_hash=hashlib.sha256(token.encode()).hexdigest())
    atomic_json(controller.state / "setup.json", setup)
    print_setup_claim(origin, admin, token)


def main():
    parser = argparse.ArgumentParser(description="David-Pi local management")
    parser.add_argument("--etc", default="/etc/david-pi")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("storage-guard")
    sub.add_parser("status")
    sub.add_parser("address")
    sub.add_parser("setup-release")
    renew = sub.add_parser("renew-claim")
    renew.add_argument("--admin")
    sub.add_parser("recovery-preflight")
    preparation = sub.add_parser("prepare-recovery")
    preparation.add_argument("--admin", required=True)
    preparation.add_argument("--hostname", required=True)
    snapshots = sub.add_parser("update-snapshots")
    snapshots.add_argument("--remove")
    sub.add_parser("verify")
    sub.add_parser("pihole-summary")
    pihole = sub.add_parser("pihole-connect")
    pihole.add_argument("database")
    init = sub.add_parser("initialize")
    for name in ("admin", "hostname", "image", "repository"):
        init.add_argument("--"+name, required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--snapshot")
    restore.add_argument("--data-root")
    recover = sub.add_parser("recover-admin")
    recover.add_argument("login")
    recover.add_argument("--name", default="Household administrator")
    reconnect = sub.add_parser("reconnect")
    reconnect.add_argument("--accept-origin", required=True)
    operation = sub.add_parser("operation")
    operation.add_argument("operation", choices=["update", "backup", "restore_test", "update_check", "repair"])
    operation.add_argument("--version", dest="testing_version")
    args = parser.parse_args()
    if args.command == "operation" and args.testing_version and args.operation != "update":
        parser.error("--version is available only for an explicitly selected testing update")
    if args.command == "restore" and bool(args.snapshot) != bool(args.data_root):
        parser.error("Supply both --snapshot and --data-root, or omit both for guided choices")
    if os.geteuid() != 0:
        parser.error("Run with sudo on the server")
    if args.command == "address":
        print_address(args.etc)
        return 0
    if args.command == "setup-release":
        print("\n".join(saved_release_arguments(args.etc)))
        return 0
    controller = Controller(args.etc)
    if args.command == "serve":
        serve(controller)
    elif args.command == "initialize":
        with controller.external_operation():
            initialize(controller, args.admin, args.hostname, args.image, args.repository)
    elif args.command == "recovery-preflight":
        print(json.dumps(recovery.preflight(controller, sys.modules[__name__]), indent=2))
    elif args.command == "prepare-recovery":
        with controller.external_operation():
            recovery.prepare(controller, sys.modules[__name__], args.admin, args.hostname)
    elif args.command == "update-snapshots":
        if args.remove:
            if not sys.stdin.isatty():
                raise HostError("Snapshot removal requires an interactive server terminal")
            expected = "REMOVE SNAPSHOT " + args.remove
            if input(f"Type {expected} to remove this recovery point: ").strip() != expected:
                raise HostError("Snapshot removal cancelled")
            with controller.external_operation():
                print(json.dumps(controller.remove_update_snapshot(args.remove), indent=2))
        else:
            print(json.dumps(controller.update_snapshot_status(), indent=2))
    elif args.command == "renew-claim":
        renew_claim(controller, admin=args.admin)
    elif args.command == "storage-guard":
        controller.storage_guard()
    elif args.command == "verify":
        print(json.dumps(controller.readiness(), indent=2))
    elif args.command == "status":
        print(json.dumps(controller.status(), indent=2))
    elif args.command == "pihole-summary":
        controller.pihole_summary()
    elif args.command == "pihole-connect":
        with controller.external_operation():
            print(json.dumps(controller.pihole_connect(args.database), indent=2))
    elif args.command == "restore":
        with controller.external_operation():
            recovery.preflight(controller, sys.modules[__name__])
            if args.snapshot:
                snapshot, data_root = args.snapshot, args.data_root
            else:
                if not sys.stdin.isatty():
                    raise HostError("Guided recovery requires an interactive server terminal")
                snapshot, data_root = recovery.guided_paths(controller, sys.modules[__name__])
            print(json.dumps(controller.restore(snapshot, data_root), indent=2))
    elif args.command == "reconnect":
        with controller.external_operation():
            print(json.dumps(controller.reconnect(args.accept_origin), indent=2))
    elif args.command == "recover-admin":
        with controller.external_operation():
            cfg = controller.config()
            cfg["members"] = [m for m in cfg["members"] if m["login"].casefold() != args.login.casefold()]
            cfg["members"].append({"login": args.login.casefold(), "name": args.name, "role": "admin"})
            controller.save_config(cfg)
            controller.docker("up", "-d", "--force-recreate")
            print("Administrator restored. Tailscale authentication is still required.")
    elif args.command == "operation":
        if args.operation == "update_check":
            print(json.dumps(controller.update_check(), indent=2))
        else:
            # CLI forwards to the running helper to share its serialization lock.
            from installer.client import request
            result = request(args.operation, {"testing_version": args.testing_version} if args.testing_version else {})
            print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except HostError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
