"""Validated, versioned household configuration. This module has no app side effects.

Secrets belong in separately protected files, never in this document. A missing
configuration is permitted for developer fixtures; it never admits a real user.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
MODULES = {
    "media": {"label": "Media", "path": "/photos", "modes": ("enabled", "disabled")},
    "mytube": {"label": "MyTube", "path": "/mytube", "modes": ("enabled", "disabled")},
    "audiobooks": {"label": "Audiobooks", "path": "/audiobooks", "modes": ("enabled", "disabled")},
    "files": {"label": "Files", "path": "/files", "modes": ("enabled", "disabled")},
    "notes": {"label": "Notes", "path": "/notes", "modes": ("enabled", "disabled")},
    "recipes": {"label": "Recipes", "path": "/recipes", "modes": ("manual", "connected", "disabled")},
    "chat": {"label": "Chat", "path": "/chat", "modes": ("enabled", "disabled")},
    "movies": {"label": "Movie Night", "path": "/movies", "modes": ("manual", "connected", "disabled")},
    "places": {"label": "Date Night", "path": "/places", "modes": ("enabled", "disabled")},
    "games": {"label": "Games", "path": "/games", "modes": ("enabled", "disabled")},
    "assistant": {"label": "Assistant", "path": "/assistant", "modes": ("enabled", "disabled")},
    "device_backup": {"label": "Phone backup", "path": "/device-backup", "modes": ("enabled", "disabled"), "requires": ("media",)},
    "pihole": {"label": "Pi-hole", "path": "/api/pihole", "modes": ("enabled", "disabled")},
}

# One registry owns the boundaries shared by the website and the host helper.
# Storage names are relative to the dedicated application root. Shared storage
# is infrastructure, not a dependency on another user-visible feature.
_LIFECYCLE = {
    "media": {"routes": ("/photos", "/api/photos", "/api/collections", "/api/slideshows", "/api/upload", "/media"), "substrates": ("media",), "workers": ("slideshow",)},
    "mytube": {"routes": ("/mytube", "/api/mytube"), "storage": ("mytube",), "substrates": ("media",), "workers": ("mytube-preparer",)},
    "audiobooks": {"routes": ("/audiobooks", "/api/audiobooks"), "storage": ("audiobooks/originals", "audiobooks/streaming", "audiobooks/incoming/streaming", ".david-pi-operations/audiobook"), "workers": ("audiobook-preparer",)},
    "files": {"routes": ("/files", "/api/files"), "storage": ("files",)},
    "notes": {"routes": ("/notes", "/api/notes")},
    "recipes": {"routes": ("/recipes", "/api/recipes", "/api/kitchen")},
    "chat": {"routes": ("/chat", "/api/chat"), "storage": ("chat",), "workers": ("chat-notifier",)},
    "movies": {"routes": ("/movies", "/api/movies")},
    "places": {"routes": ("/places", "/api/places", "/api/date-night")},
    "games": {"routes": ("/games", "/api/games")},
    "assistant": {"routes": ("/assistant", "/api/assistant"), "storage": ("platform/assistant",)},
    "device_backup": {"routes": ("/device-backup", "/api/device-backup", "/api/v1/device-backup"), "storage": ("incoming/device-backup",), "substrates": ("media",), "workers": ("device-backup",)},
    "pihole": {"routes": ("/api/pihole",)},
}
for _name, _spec in MODULES.items():
    _spec.update(_LIFECYCLE[_name])
del _name, _spec

STORAGE_SUBSTRATES = {
    "media": {"directories": ("originals", "previews", "viewer-previews", "thumbs"), "databases": ("photos.db",)},
}
WORKERS = {
    "slideshow": {"entrypoint": "modules.slideshow_worker", "healthcheck": True},
    "device-backup": {"entrypoint": "modules.device_backup_worker", "healthcheck": True},
    "audiobook-preparer": {"entrypoint": "modules.audiobook_prepare_worker", "healthcheck": False},
    "mytube-preparer": {"entrypoint": "modules.mytube_prepare_worker", "healthcheck": False},
    "chat-notifier": {"entrypoint": "modules.chat_notify_worker", "healthcheck": False, "integration": "web_push"},
    "maintenance": {"entrypoint": "modules.maintenance_worker", "healthcheck": True, "always": True},
}


def selected_modules(config):
    return {name: spec for name, spec in MODULES.items() if config["modules"].get(name, "disabled") != "disabled"}


def selected_substrates(config):
    names = {name for spec in selected_modules(config).values() for name in spec.get("substrates", ())}
    return {name: STORAGE_SUBSTRATES[name] for name in names}


def selected_workers(config):
    names = {name for spec in selected_modules(config).values() for name in spec.get("workers", ())}
    names.update(name for name, spec in WORKERS.items() if spec.get("always"))
    return {name: spec for name, spec in WORKERS.items() if name in names and (
        not spec.get("integration") or config["integrations"].get(spec["integration"], False))}


class InstallationError(ValueError):
    """Configuration cannot safely be used."""


def config_path() -> Path:
    return Path(os.environ.get("DAVID_PI_CONFIG_FILE", "/etc/david-pi/installation.json"))


def _text(value, label, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise InstallationError(f"Choose a valid {label}.")
    return value.strip()


def _absolute_path(value, label):
    value = _text(value, label, 4096)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) == "/":
        raise InstallationError(f"Choose an absolute, dedicated {label}.")
    return str(path)


def validate_origin(value: str) -> str:
    value = _text(value, "private HTTPS address", 512)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise InstallationError("Choose a valid private HTTPS address.") from exc
    host = (parsed.hostname or "").lower()
    labels = host.split(".")
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in (None, 443) or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or len(labels) != 4
            or labels[-2:] != ["ts", "net"]
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels[:2])):
        raise InstallationError("Use the server's exact Tailscale HTTPS address, without a path or port.")
    return f"https://{host}"


def validate_installation(data: Mapping) -> dict:
    if not isinstance(data, Mapping) or type(data.get("schema_version")) is not int or data.get("schema_version") != SCHEMA_VERSION:
        raise InstallationError("Unsupported installation configuration version.")
    allowed = {"schema_version", "instance_id", "display_name", "hostname", "public_url", "timezone", "country", "members", "storage", "modules", "integrations"}
    if set(data) - allowed:
        raise InstallationError("Installation configuration contains unsupported fields; keep secrets in separate files.")
    try:
        instance_id = str(uuid.UUID(str(data.get("instance_id", ""))))
    except ValueError as exc:
        raise InstallationError("A valid installation identity is required.") from exc
    hostname = _text(data.get("hostname"), "hostname", 63).lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", hostname):
        raise InstallationError("The hostname can contain letters, numbers, and internal hyphens.")
    timezone = _text(data.get("timezone"), "timezone", 100)
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InstallationError("Choose a recognized timezone.") from exc
    country = _text(data.get("country"), "country", 2).upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise InstallationError("Choose a two-letter country code.")
    incoming_members = data.get("members")
    if not isinstance(incoming_members, list) or not 1 <= len(incoming_members) <= 100:
        raise InstallationError("Add at least one household administrator.")
    members, logins = [], set()
    for member in incoming_members:
        if not isinstance(member, Mapping) or set(member) - {"login", "name", "role"}:
            raise InstallationError("Invalid household member.")
        login = _text(member.get("login"), "Tailscale login", 320).casefold()
        if any(c.isspace() for c in login) or "@" not in login or login in logins:
            raise InstallationError("Each member needs a unique, individual Tailscale login.")
        if member.get("role") not in {"admin", "household"}:
            raise InstallationError("Choose administrator or household member.")
        members.append({"login": login, "name": _text(member.get("name"), "member name", 100), "role": member["role"]})
        logins.add(login)
    if not any(m["role"] == "admin" for m in members):
        raise InstallationError("Keep at least one household administrator.")
    storage = data.get("storage")
    if not isinstance(storage, Mapping) or storage.get("mode") not in {"folder", "drive"} or set(storage) - {"mode", "data_root", "backup_root"}:
        raise InstallationError("Choose folder or drive storage.")
    data_root = _absolute_path(storage.get("data_root"), "data directory")
    backup_root = _absolute_path(storage["backup_root"], "backup directory") if storage.get("backup_root") else None
    if backup_root and (Path(backup_root) == Path(data_root) or Path(backup_root).is_relative_to(data_root) or Path(data_root).is_relative_to(backup_root)):
        raise InstallationError("Choose a backup location separate from application data.")
    modes = data.get("modules", {})
    if not isinstance(modes, Mapping) or set(modes) - MODULES.keys():
        raise InstallationError("Unknown application module.")
    modes = {name: modes.get(name, "disabled") for name in MODULES}
    for name, mode in modes.items():
        if mode not in MODULES[name]["modes"]:
            raise InstallationError(f"Choose a supported mode for {MODULES[name]['label']}.")
        if mode != "disabled":
            for dependency in MODULES[name].get("requires", ()):
                if modes[dependency] == "disabled":
                    raise InstallationError(f"{MODULES[name]['label']} requires {MODULES[dependency]['label']}.")
    integrations = data.get("integrations", {})
    if not isinstance(integrations, Mapping) or set(integrations) - {"web_push"} or not isinstance(integrations.get("web_push", False), bool):
        raise InstallationError("Unknown optional integration.")
    if integrations.get("web_push") and modes["chat"] == "disabled":
        raise InstallationError("Browser notifications require Chat.")
    return {"schema_version": SCHEMA_VERSION, "instance_id": instance_id,
            "display_name": _text(data.get("display_name"), "website name", 80), "hostname": hostname,
            "public_url": validate_origin(data.get("public_url")), "timezone": timezone, "country": country,
            "members": members, "storage": {"mode": storage["mode"], "data_root": data_root, "backup_root": backup_root},
            "modules": modes, "integrations": {"web_push": integrations.get("web_push", False)}}


def load_installation(path=None, required=False) -> dict | None:
    source = Path(path) if path is not None else config_path()
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024:
                raise InstallationError("The installation configuration is not a regular supported document.")
            contents = handle.read(256 * 1024 + 1)
            if len(contents) > 256 * 1024:
                raise InstallationError("The installation configuration is too large.")
        return validate_installation(json.loads(contents))
    except FileNotFoundError:
        if required:
            raise InstallationError("Finish server setup before starting the portal.") from None
        return None
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise InstallationError("The installation configuration cannot be read safely.") from exc


def save_installation(data, path=None) -> dict:
    validated = validate_installation(data)
    destination = Path(path) if path is not None else config_path()
    previous = load_installation(destination)
    if previous and previous["instance_id"] != validated["instance_id"]:
        raise InstallationError("An existing installation identity cannot be changed.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    descriptor, temporary = tempfile.mkstemp(prefix=".installation-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o640)
            json.dump(validated, output, indent=2, ensure_ascii=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return validated


def get_installation():
    return load_installation()


def module_mode(name):
    config = get_installation()
    if config:
        return config["modules"].get(name, "disabled")
    return "enabled" if name in MODULES else "disabled"


def module_enabled(name):
    return module_mode(name) != "disabled"


def substrate_enabled(name):
    config = get_installation()
    return name in (selected_substrates(config) if config else STORAGE_SUBSTRATES)


def display_name():
    config = get_installation()
    return config["display_name"] if config else "David-Pi"


def public_url():
    config = get_installation()
    if config:
        return config["public_url"]
    value = os.environ.get("DAVID_PI_PUBLIC_URL", "")
    return validate_origin(value) if value else ""


def identity_roles():
    config = get_installation()
    return {member["login"]: member["role"] for member in config["members"]} if config else {}


class ConfiguredRoles(Mapping):
    """Legacy call sites see current membership rather than a copied allowlist."""
    def __getitem__(self, key):
        return identity_roles()[key]

    def __iter__(self):
        return iter(identity_roles())

    def __len__(self):
        return len(identity_roles())


def module_catalog():
    return [{"id": name, **spec, "mode": module_mode(name)} for name, spec in MODULES.items()]
