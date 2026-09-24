"""Central request and response protections for the private David-Pi portal."""

from __future__ import annotations

import gzip
import hmac
import re
import secrets

from flask import g, jsonify, request

from .content_policy import (
    UnclassifiedMutation,
    derive_csrf_contract,
    load_route_policy,
)


CSRF_COOKIE = "david_pi_csrf"
CSRF_CONTRACT = derive_csrf_contract(load_route_policy())
SAFE_METHODS = CSRF_CONTRACT.safe_methods
READ_ONLY_POSTS = frozenset(
    route
    for method, route in CSRF_CONTRACT.read_only_exempt
    if method == "POST"
)
EXACT_BEARER_EXEMPT_ROUTES = CSRF_CONTRACT.bearer_exempt
EXACT_PAIRING_CODE_EXEMPT_ROUTES = CSRF_CONTRACT.pairing_code_exempt
from urllib.parse import urlsplit
from .installation import InstallationError, public_url

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "test.localhost"}

COMPRESSIBLE_MIMETYPES = {
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/xml",
    "image/svg+xml",
}
MINIMUM_COMPRESSION_BYTES = 1024
MAXIMUM_DYNAMIC_COMPRESSION_BYTES = 2 * 1024 * 1024
STATIC_VERSION = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MEDIA_CACHE_SCOPE_HEADER = "X-David-Pi-Media-Cache-Scope"
PRIVATE_REVALIDATE_SCOPE_HEADER = "X-David-Pi-Private-Revalidate-Scope"


def _accepts_gzip() -> bool:
    """Return whether gzip is an allowed response representation."""
    return (request.accept_encodings.quality("gzip") or 0) > 0


def _compress_response(response):
    """Compress safe, complete text responses without touching ranged media."""
    direct_static = response.direct_passthrough and request.path.startswith("/static/")
    if (
        request.method == "HEAD"
        or response.status_code != 200
        or (response.direct_passthrough and not direct_static)
        or (response.is_streamed and not direct_static)
        or response.headers.get("Content-Encoding")
        or response.headers.get("Content-Range")
        or "no-transform" in response.headers.get("Cache-Control", "").lower()
    ):
        return response
    mimetype = (response.mimetype or "").lower()
    if not (mimetype.startswith("text/") or mimetype in COMPRESSIBLE_MIMETYPES):
        return response
    declared_length = response.content_length
    if declared_length is not None and declared_length > MAXIMUM_DYNAMIC_COMPRESSION_BYTES:
        return response
    if direct_static:
        response.direct_passthrough = False
    payload = response.get_data()
    if not (MINIMUM_COMPRESSION_BYTES <= len(payload) <= MAXIMUM_DYNAMIC_COMPRESSION_BYTES):
        return response
    response.headers["Vary"] = response.headers.get("Vary", "Accept-Encoding")
    if "accept-encoding" not in response.headers["Vary"].lower():
        response.headers["Vary"] += ", Accept-Encoding"
    if not _accepts_gzip():
        return response
    compressed = gzip.compress(payload, compresslevel=6, mtime=0)
    if len(compressed) >= len(payload):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    # A strong ETag produced for the identity representation cannot describe
    # the compressed bytes. Versioned private assets do not depend on it.
    response.headers.pop("ETag", None)
    return response


def csrf_token() -> str:
    token = request.cookies.get(CSRF_COOKIE, "")
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
    return token


def _csrf_class_for_request() -> str:
    """Classify the matched Flask rule, requiring CSRF on every unknown write."""
    if request.method in SAFE_METHODS:
        return "safe"
    route = request.url_rule.rule if request.url_rule is not None else request.path
    try:
        return CSRF_CONTRACT.classify(request.method, route)
    except UnclassifiedMutation:
        # A new route cannot inherit an exemption merely by sharing a prefix
        # with a reviewed device endpoint. Runtime policy validation will flag
        # the drift at release time; the request middleware fails closed now.
        return "required"


def init_security(app) -> None:
    @app.before_request
    def protect_request():
        host = request.host.split(":", 1)[0].lower().rstrip(".")
        try:
            configured_host = urlsplit(public_url()).hostname
        except InstallationError:
            return jsonify(error="Server configuration needs administrator recovery."), 503
        if host not in ALLOWED_HOSTS | ({configured_host} if configured_host else set()):
            return jsonify(error="This David-Pi address is not allowed."), 400
        g.csrf_token = csrf_token()
        csrf_class = _csrf_class_for_request()
        if csrf_class == "required":
            supplied = request.headers.get("X-CSRF-Token", "") or request.form.get(
                "_csrf", ""
            )
            if not supplied or not hmac.compare_digest(supplied, g.csrf_token):
                return jsonify(error="Reload this page and try again."), 403
        return None

    @app.context_processor
    def security_context():
        return {"csrf_token": getattr(g, "csrf_token", csrf_token())}

    @app.after_request
    def protect_response(response):
        token = getattr(g, "csrf_token", None)
        if token and request.cookies.get(CSRF_COOKIE) != token:
            response.set_cookie(
                CSRF_COOKIE,
                token,
                secure=True,
                httponly=False,
                samesite="Strict",
                path="/",
                max_age=60 * 60 * 24 * 30,
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Origin-Agent-Cluster"] = "?1"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
            "img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'"
        )
        if request.scheme == "https" or request.headers.get(
            "Tailscale-Forwarded-Proto"
        ) == "https" or request.headers.get("X-Forwarded-Proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/static/"):
            version = request.args.get("v", "")
            response.headers["Cache-Control"] = (
                "private, max-age=31536000, immutable"
                if STATIC_VERSION.fullmatch(version)
                else "private, max-age=86400"
            )
        elif request.path.startswith("/media/"):
            # Media authorization is revocable without changing its route.  A
            # previously authorized household member must therefore never be
            # allowed to use a fresh cached response after the owner changes a
            # row from shared to private.  Shared media remains conditionally
            # cacheable, but every use must revalidate authorization; private,
            # deleted, missing, and otherwise unmarked media is never stored.
            cache_scope = response.headers.pop(MEDIA_CACHE_SCOPE_HEADER, "")
            response.headers.pop("X-David-Pi-Preview-Fallback", None)
            if cache_scope == "shared" and response.status_code in {200, 206, 304}:
                response.headers["Cache-Control"] = (
                    "private, no-cache, max-age=0, must-revalidate"
                )
            else:
                response.headers["Cache-Control"] = "private, no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        elif response.headers.pop(PRIVATE_REVALIDATE_SCOPE_HEADER, "") == "shared" and response.status_code in {200, 206, 304}:
            # Shared derived previews are still authorization gated. Revalidate
            # every use so a later shared-to-private transition cannot be
            # bypassed, while allowing an authorized 304 to reuse the bytes.
            response.headers["Cache-Control"] = "private, no-cache, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
        return _compress_response(response)
