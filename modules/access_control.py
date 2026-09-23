"""Central, staged access policy for the private David-Pi portal.

Tailscale Serve remains the authentication authority.  This module only decides
which verified tailnet identities may use human-facing portal routes.  Existing
phone-backup credentials continue to be authenticated by ``device_backup``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from flask import current_app, g, jsonify, request

ACCESS_MODE_ENV = "DAVID_PI_ACCESS_MODE"
ACCESS_MODES = frozenset({"off", "shadow", "enforce"})
DEFAULT_ACCESS_MODE = "enforce"

from .installation import ConfiguredRoles, InstallationError, get_installation

# Membership is installation data. The mapping reads the current atomic config.
IDENTITY_ROLES = ConfiguredRoles()

PUBLIC_EXACT_PATHS = frozenset(
    {
        "/health",
        "/ready",
        "/manifest.webmanifest",
        "/sw.js",
    }
)
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_ICON = re.compile(r"/david-pi-icon-[0-9]+\.png\Z")
ADMIN_ROUTES = frozenset({("POST", "/api/system/shutdown"), ("POST", "/api/admin/operations/<operation>")})

# Only established API routes that perform their own bearer-token validation
# are exempt from the human identity allowlist.  Pairing is intentionally not
# here: exchanging a one-time pairing code still requires an allowed person.
BEARER_BACKUP_ROUTES = (
    (frozenset({"POST"}), re.compile(r"/api/v1/device-backup/uploads\Z")),
    (
        frozenset({"HEAD", "PATCH", "DELETE"}),
        re.compile(r"/api/v1/device-backup/uploads/[^/]+\Z"),
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/api/v1/device-backup/uploads/[^/]+/complete\Z"),
    ),
    (
        frozenset({"GET", "HEAD"}),
        re.compile(r"/api/v1/(?:device-backup|ios-backup)/status\Z"),
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/api/v1/ios-backup/(?:upload|upload-file|checkpoint)\Z"),
    ),
    (
        frozenset({"GET", "HEAD"}),
        re.compile(r"/api/v1/device-backup/manifest\Z"),
    ),
    (
        frozenset({"POST"}),
        re.compile(r"/api/v1/device-backup/reconcile\Z"),
    ),
)


def normalize_login(value: str | None) -> str:
    """Return the canonical comparison form for a Tailscale login."""
    return str(value or "").strip().casefold()[:320]


def resolve_access_mode(value: str | None) -> tuple[str, bool]:
    """Resolve a mode, failing closed to enforcement for unknown values.

    The boolean reports whether the supplied value was valid so startup can
    emit a useful operator warning while keeping the portal protected.
    """
    normalized = str(value or DEFAULT_ACCESS_MODE).strip().casefold()
    if normalized in ACCESS_MODES:
        return normalized, True
    return DEFAULT_ACCESS_MODE, False


def request_class(path: str, method: str) -> str:
    """Classify a request without examining credentials or user data."""
    if (
        path in PUBLIC_EXACT_PATHS
        or path.startswith(PUBLIC_PREFIXES)
        or PUBLIC_ICON.fullmatch(path)
    ):
        return "public"
    upper_method = method.upper()
    for methods, pattern in BEARER_BACKUP_ROUTES:
        if upper_method in methods and pattern.fullmatch(path):
            return "device_bearer"
    return "portal"


class _AuditDeduper:
    """Bounded, per-process suppression for metadata-only access logs."""

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 2048) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._seen: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()
        self._fingerprint_key = secrets.token_bytes(32)

    def principal_label(self, login: str, role: str | None) -> str:
        if role:
            return role
        if not login:
            return "missing"
        digest = hmac.new(
            self._fingerprint_key, login.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"unknown-{digest[:12]}"

    def should_log(self, key: tuple[str, ...], now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None and timestamp - previous < self.ttl_seconds:
                return False
            if len(self._seen) >= self.max_entries:
                cutoff = timestamp - self.ttl_seconds
                self._seen = {
                    item: seen_at
                    for item, seen_at in self._seen.items()
                    if seen_at >= cutoff
                }
            if len(self._seen) >= self.max_entries:
                self._seen.pop(next(iter(self._seen)))
            self._seen[key] = timestamp
        return True


def _denied_response():
    message = "Open David-Pi through an approved private Tailscale account."
    if request.path.startswith(("/api/", "/media/")):
        return jsonify(
            error={"code": "portal_access_denied", "message": message}
        ), 403
    return message, 403, {"Content-Type": "text/plain; charset=utf-8"}


def _current_portal_identity():
    # Import lazily so policy-only tests never initialize application storage.
    from .identity import current_device

    return current_device()


def init_access_control(app, mode: str | None = None, identity_provider=None) -> None:
    """Register the staged central allowlist before feature request hooks."""
    configured = mode if mode is not None else os.environ.get(ACCESS_MODE_ENV)
    resolved_mode, valid = resolve_access_mode(configured)
    app.config[ACCESS_MODE_ENV] = resolved_mode
    audit = _AuditDeduper()
    app.extensions["david_pi_access_control"] = {
        "mode": resolved_mode,
        "audit": audit,
    }
    if not valid:
        app.logger.error(
            "Invalid %s value; failing closed to enforce mode", ACCESS_MODE_ENV
        )
    provide_identity = identity_provider or _current_portal_identity

    @app.before_request
    def enforce_portal_access():
        route_class = request_class(request.path, request.method)
        try:
            identity = provide_identity()
            login = normalize_login(identity.get("owner_id"))
            role = IDENTITY_ROLES.get(login)
            configured = get_installation() is not None
        except InstallationError:
            return jsonify(error="Server configuration needs administrator recovery."), 503
        if current_app.testing and not configured:
            role = {"david@example.test": "admin", "diana@example.test": "household"}.get(login, role)
        effective_mode = "enforce" if configured else resolved_mode
        admin_required = (request.method.upper(), request.path) in ADMIN_ROUTES or request.path == "/settings" or request.path.startswith("/api/admin/")
        allowed = route_class != "portal" or (
            role is not None and (not admin_required or role == "admin")
        )
        decision = "allow" if allowed else "deny"
        reason = (
            "public"
            if route_class == "public"
            else "device_bearer"
            if route_class == "device_bearer"
            else "allowlisted"
            if role and not admin_required
            else "admin"
            if role == "admin"
            else "admin_required"
            if role and admin_required
            else "missing_identity"
            if not login
            else "identity_not_allowlisted"
        )
        g.portal_access = {
            "mode": effective_mode,
            "route_class": route_class,
            "decision": decision,
            "role": role,
            "admin_required": admin_required,
        }
        if not allowed:
            # This record intentionally carries no principal, route, method,
            # or reason. The host collector can therefore aggregate every web
            # worker without exposing request or identity metadata.
            disposition = (
                "blocked"
                if effective_mode == "enforce"
                else "shadow"
                if effective_mode == "shadow"
                else "unenforced"
            )
            current_app.logger.warning(
                "david_pi_access_counter mode=%s disposition=%s",
                resolved_mode,
                disposition,
            )

        if resolved_mode == "shadow" or (
            resolved_mode == "enforce" and not allowed
        ):
            principal = audit.principal_label(login, role)
            route = request.url_rule.rule if request.url_rule else "<unmatched>"
            key = (resolved_mode, decision, principal, request.method, route, reason)
            if audit.should_log(key):
                # Production intentionally runs at WARNING.  During the short
                # shadow rollout, promote only these bounded, metadata-only
                # decisions so the allowlist can be verified before enforcement.
                current_app.logger.warning(
                    "portal_access mode=%s decision=%s principal=%s method=%s "
                    "route=%s class=%s reason=%s",
                    resolved_mode,
                    decision,
                    principal,
                    request.method,
                    route,
                    route_class,
                    reason,
                )

        if effective_mode == "enforce" and not allowed:
            return _denied_response()
        return None
