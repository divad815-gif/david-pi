from flask import Flask

from modules.security import init_security


def app_with_security():
    app = Flask(__name__, static_folder=None)
    app.config.update(TESTING=True)
    init_security(app)

    @app.get("/")
    def index():
        return "David-Pi"

    @app.get("/static/example.js")
    def static_example():
        return "console.log('David-Pi');", {"Content-Type": "application/javascript"}

    return app


def test_responses_isolate_the_portal_from_cross_origin_contexts():
    response = app_with_security().test_client().get(
        "/", base_url="https://test.localhost"
    )

    assert response.status_code == 200
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response.headers["Origin-Agent-Cluster"] == "?1"
    assert response.headers["X-Permitted-Cross-Domain-Policies"] == "none"


def test_existing_private_and_transport_headers_remain_present():
    response = app_with_security().test_client().get(
        "/", base_url="https://test.localhost"
    )

    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    assert "camera=()" in response.headers["Permissions-Policy"]
    policy = response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy
    assert "img-src 'self' data: blob:" in policy
    assert "img-src 'self' data: blob: https:" not in policy


def test_only_explicitly_versioned_static_assets_are_long_lived():
    client = app_with_security().test_client()

    versioned = client.get(
        "/static/example.js?v=assistant.6", base_url="https://test.localhost"
    )
    unversioned = client.get(
        "/static/example.js", base_url="https://test.localhost"
    )
    malformed = client.get(
        "/static/example.js?v=not%20a%20version", base_url="https://test.localhost"
    )

    assert versioned.headers["Cache-Control"] == (
        "private, max-age=31536000, immutable"
    )
    assert unversioned.headers["Cache-Control"] == "private, max-age=86400"
    assert malformed.headers["Cache-Control"] == "private, max-age=86400"


def test_device_csrf_exemptions_match_only_the_compiled_route_contract():
    app = app_with_security()

    @app.post("/api/v1/device-backup/uploads/<upload_id>/complete")
    def bearer_complete(upload_id):
        return f"completed {upload_id}"

    @app.post("/api/v1/device-backup/pair")
    def pairing_code():
        return "paired"

    @app.post("/api/v1/device-backup/future-dangerous-mutation")
    def unreviewed_mutation():
        return "mutated"

    client = app.test_client()
    assert client.post(
        "/api/v1/device-backup/uploads/upload-1/complete"
    ).status_code == 200
    assert client.post("/api/v1/device-backup/pair").status_code == 200

    # Sharing the approved prefix does not grant a future mutation an
    # exemption. It is blocked unless this browser proves the CSRF secret.
    denied = client.post("/api/v1/device-backup/future-dangerous-mutation")
    assert denied.status_code == 403
    assert denied.get_json()["error"] == "Reload this page and try again."

    csrf = "future-route-csrf-token-with-more-than-32-characters"
    client.set_cookie("david_pi_csrf", csrf, domain="localhost")
    allowed = client.post(
        "/api/v1/device-backup/future-dangerous-mutation",
        headers={"X-CSRF-Token": csrf},
    )
    assert allowed.status_code == 200
