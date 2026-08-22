"""Verified Tailscale Serve identity for ownership and private records."""

from email.header import decode_header, make_header
from functools import wraps

from flask import current_app, g, request

from .platform import PLATFORM_DATA, connect, migrate


DB_PATH = PLATFORM_DATA / "platform.db"


def initialize_identity(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS platform_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


migrate(DB_PATH, initialize_identity)


def current_device():
    login = request.headers.get("Tailscale-User-Login", "").strip().casefold()
    name = request.headers.get("Tailscale-User-Name", "").strip()
    if current_app.testing:
        login = request.headers.get("X-Test-Tailscale-Login", login).strip().casefold()
        name = request.headers.get("X-Test-Tailscale-Name", name).strip()
    try:
        name = str(make_header(decode_header(name))) if name else ""
    except (LookupError, UnicodeError):
        name = ""
    return {
        # Keep the legacy profile neutral for modules where identity is only a label.
        "profile": "home",
        "name": (name[:100] or login.split("@", 1)[0][:100] or "Home"),
        "owner_id": login[:320] or None,
        "verified": bool(login),
        "csrf_token": getattr(g, "csrf_token", ""),
    }


def require_profile(api=True, david_only=False):
    """Compatibility wrapper; central middleware enforces mutation protection."""
    def decorator(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            return function(*args, **kwargs)
        return wrapped
    return decorator


def init_identity(app):
    @app.context_processor
    def shared_access_context():
        identity = current_device()
        return {
            "paired_profile": "tailscale" if identity["verified"] else None,
            "paired_name": identity["name"] if identity["verified"] else None,
            "current_person": identity,
            "csrf_token": getattr(g, "csrf_token", ""),
        }
