"""Portal presentation and authorization for a configured household."""
from __future__ import annotations

from flask import g, jsonify, render_template, request

from .installation import (
    MODULES, InstallationError, display_name, get_installation,
    module_catalog, module_enabled, module_mode, public_url,
)

MODULE_PREFIXES = {name: spec["routes"] for name, spec in MODULES.items()}


def _matches(path, prefix):
    return path == prefix or path.startswith(prefix + "/")


def public_metadata():
    config = get_installation()
    from .identity import current_device
    return {
        "instance_id": config["instance_id"] if config else None,
        "installation_id": config["instance_id"] if config else None,
        "member_id": current_device()["owner_id"],
        "display_name": display_name(), "public_url": public_url(),
        "timezone": config["timezone"] if config else "UTC",
        "country": config["country"] if config else "US",
        "modules": {name: module_mode(name) for name in MODULES},
        "is_admin": getattr(g, "portal_access", {}).get("role") == "admin",
        "web_push": bool(config and config["integrations"]["web_push"]),
    }


def init_portal_configuration(app):
    # Keep an explicit, closed retirement boundary for historical URLs and
    # their reviewed request policy. No pairing or upload code runs here.
    @app.post("/api/ios-backup/shortcut")
    @app.post("/api/ios-backup/credential-file")
    @app.post("/api/v1/ios-backup/pair")
    @app.post("/api/v1/ios-backup/upload")
    @app.post("/api/v1/ios-backup/upload-file")
    @app.post("/api/v1/ios-backup/checkpoint")
    def retired_ios_operation():
        return jsonify(error="iPhone support is not included in this release."), 404

    @app.before_request
    def configured_capabilities():
        path = request.path
        # Deferred features are unavailable even if stale credentials remain.
        if (path.startswith("/api/v1/ios-backup") or path.startswith("/ios-backup")
                or path.startswith("/api/chat/gifs/") or path == "/api/chat/push/android"):
            return jsonify(error="This feature is not included in this release."), 404
        try:
            config = get_installation()
            companion_control = path in {"/device-backup/apk", "/api/device-backup/pairing-token", "/api/v1/device-backup/pair", "/api/v1/device-backup/status"} or (path.startswith("/api/device-backup/devices/") and path.endswith("/revoke"))
            for name, prefixes in MODULE_PREFIXES.items():
                if name == "device_backup" and companion_control:
                    continue
                if any(_matches(path, prefix) for prefix in prefixes) and not module_enabled(name):
                    return jsonify(error="This module is disabled. Its saved data is preserved.", code="module_disabled"), 404
            if config and path.startswith("/api/chat/push/") and not config["integrations"]["web_push"]:
                return jsonify(error="Browser notifications are not configured.", code="integration_disabled"), 409
            if config and module_mode("recipes") != "connected" and (
                path.startswith("/api/recipes/discover") or path in ("/api/recipes/import", "/api/recipes/import-mealdb")
            ):
                return jsonify(error="Online recipes are off. An administrator can connect them in Settings.", code="integration_disabled"), 409
            if config and module_mode("movies") != "connected" and path in ("/api/movies/search", "/api/movies/check"):
                return jsonify(provider_mode="manual", results=[], error="Movie Night is in manual mode. Connect TMDB in Settings for online search and availability."), 409
            if path.endswith("/save-to-media") and not module_enabled("media"):
                return jsonify(error="Enable Media before saving this attachment to the library."), 409
        except InstallationError:
            return jsonify(error="Server configuration needs administrator recovery."), 503
        return None

    @app.context_processor
    def installation_context():
        config = get_installation()
        return {
            "server_name": display_name(), "public_installation": public_metadata(),
            "module_enabled": module_enabled, "module_mode": module_mode,
            "household_members": config["members"] if config else [],
            "audiobook_progress_scope": "",
        }

    @app.get("/api/installation")
    def installation_metadata():
        return jsonify(public_metadata())

    @app.get("/settings")
    def installation_settings():
        if getattr(g, "portal_access", {}).get("role") != "admin":
            return jsonify(error="Only a household administrator can manage this server."), 403
        return render_template("settings.html", catalog=module_catalog())

    @app.post("/api/admin/operations/<operation>")
    def installation_operation(operation):
        if getattr(g, "portal_access", {}).get("role") != "admin":
            return jsonify(error="Only a household administrator can manage this server."), 403
        if operation not in {"status", "settings", "test_integration", "update_check", "update", "backup", "restore_test", "job"}:
            return jsonify(error="Unknown server operation."), 404
        if request.content_length and request.content_length > 65536:
            return jsonify(error="Settings request is too large."), 413
        payload = request.get_json(silent=True)
        if payload is not None and not isinstance(payload, dict):
            return jsonify(error="Use a settings object."), 400
        from .identity import current_device
        from installer.client import HostError, request as host_request
        try:
            result = host_request(operation, payload or {}, identity=current_device()["owner_id"])
            return jsonify(result)
        except HostError as error:
            return jsonify(error=str(error)), 400
        except (OSError, RuntimeError, ValueError, ImportError):
            # Do not expose helper paths, provider credentials, or child output.
            return jsonify(error="Server management is unavailable. Check the local helper using the recovery instructions."), 503
