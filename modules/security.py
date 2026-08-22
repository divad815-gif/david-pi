"""Central request and response protections for the private David-Pi portal."""

from __future__ import annotations

import hmac
import os
import secrets

from flask import g, jsonify, request


CSRF_COOKIE = "david_pi_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
READ_ONLY_POSTS = {"/api/collections/membership-state"}
DEVICE_BACKUP_PREFIX = "/api/v1/device-backup/"
IOS_BACKUP_PREFIX = "/api/v1/ios-backup/"
ALLOWED_HOSTS = {
    host.strip().lower().rstrip(".")
    for host in os.environ.get(
        "DAVID_PI_ALLOWED_HOSTS", "127.0.0.1,localhost,test.localhost"
    ).split(",")
    if host.strip()
}


def csrf_token() -> str:
    token = request.cookies.get(CSRF_COOKIE, "")
    if len(token) < 32:
        token = secrets.token_urlsafe(32)
    return token


def init_security(app) -> None:
    @app.before_request
    def protect_request():
        host = request.host.split(":", 1)[0].lower().rstrip(".")
        if host not in ALLOWED_HOSTS:
            return jsonify(error="This David-Pi address is not allowed."), 400
        g.csrf_token = csrf_token()
        if (
            request.method not in SAFE_METHODS
            and request.path not in READ_ONLY_POSTS
            and not request.path.startswith((DEVICE_BACKUP_PREFIX, IOS_BACKUP_PREFIX))
        ):
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
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self'; connect-src 'self'; worker-src 'self' blob:; "
            "img-src 'self' data: blob: https:; media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'"
        )
        if request.scheme == "https" or request.headers.get(
            "Tailscale-Forwarded-Proto"
        ) == "https" or request.headers.get("X-Forwarded-Proto") == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "private, max-age=86400"
        elif request.path.startswith("/media/thumb/"):
            response.headers["Cache-Control"] = "private, max-age=2592000, immutable"
        elif request.path.startswith("/media/preview/"):
            response.headers["Cache-Control"] = "private, max-age=604800"
        elif request.path.startswith("/media/view/"):
            if response.headers.pop("X-David-Pi-Preview-Fallback", None):
                response.headers["Cache-Control"] = "private, no-store"
            else:
                response.headers["Cache-Control"] = (
                    "private, max-age=2592000, immutable"
                )
        else:
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Pragma"] = "no-cache"
        return response
