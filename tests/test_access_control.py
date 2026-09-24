import logging
from pathlib import Path
import unittest

from flask import Flask, g, jsonify, request

from modules.access_control import (
    DEFAULT_ACCESS_MODE,
    IDENTITY_ROLES,
    init_access_control,
    request_class,
    resolve_access_mode,
)


DAVID = "david@example.test"
DIANA = "diana@example.test"


def make_app(mode):
    app = Flask(__name__)
    app.config.update(TESTING=True)

    def request_identity():
        return {"owner_id": request.headers.get("Tailscale-User-Login")}

    init_access_control(app, mode=mode, identity_provider=request_identity)

    @app.get("/")
    def home():
        return jsonify(access=g.portal_access)

    @app.get("/api/private")
    def private_api():
        return jsonify(access=g.portal_access)

    @app.get("/health")
    def health():
        return "ok"

    @app.get("/static/example.js")
    def static_example():
        return "static"

    @app.get("/david-pi-icon-192.png")
    def icon():
        return "icon"

    @app.post("/api/v1/device-backup/uploads")
    def device_upload():
        if request.headers.get("Authorization") != "Bearer paired-device":
            return jsonify(error="device_unauthorized"), 401
        return jsonify(route="device")

    @app.post("/api/v1/device-backup/pair")
    def device_pair():
        return jsonify(route="pair")

    @app.post("/api/system/shutdown")
    def shutdown():
        return jsonify(route="shutdown")

    @app.get("/future-human-feature")
    def future_human_feature():
        return jsonify(route="future")

    @app.get("/device-backup/apk")
    def device_backup_apk():
        return b"release-fixture"

    return app


class AccessControlTests(unittest.TestCase):
    def test_compose_mirrors_access_mode_in_a_nonsecret_status_label(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'com.david-pi.access-mode: "${DAVID_PI_ACCESS_MODE:-enforce}"',
            compose,
        )

    def test_external_assistant_is_not_configurable_in_this_release(self):
        compose = (Path(__file__).resolve().parents[1] / "compose.yaml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("ASSISTANT_WINDOWS_ENABLED:", compose)
        self.assertNotIn(
            "ASSISTANT_WINDOWS_ENABLED: ${ASSISTANT_WINDOWS_ENABLED:-true}",
            compose,
        )

    def test_unconfigured_installation_has_no_embedded_household(self):
        self.assertEqual(
            IDENTITY_ROLES,
            {},
        )

    def test_mode_resolution_defaults_and_invalid_values_to_enforce(self):
        self.assertEqual(resolve_access_mode(None), (DEFAULT_ACCESS_MODE, True))
        self.assertEqual(resolve_access_mode(" ENFORCE "), ("enforce", True))
        self.assertEqual(resolve_access_mode("unexpected"), ("enforce", False))

    def test_invalid_mode_fails_closed_for_unknown_identity(self):
        response = make_app("unexpected").test_client().get("/api/private")
        self.assertEqual(response.status_code, 403)

    def test_enforce_allows_both_exact_normalized_identities(self):
        app = make_app("enforce")
        client = app.test_client()
        david = client.get(
            "/", headers={"Tailscale-User-Login": "  DAVID@EXAMPLE.TEST "}
        )
        diana = client.get(
            "/", headers={"Tailscale-User-Login": DIANA}
        )
        self.assertEqual(david.status_code, 200)
        self.assertEqual(david.get_json()["access"]["role"], "admin")
        self.assertEqual(diana.status_code, 200)
        self.assertEqual(diana.get_json()["access"]["role"], "household")

    def test_enforce_denies_missing_and_unknown_identities_uniformly(self):
        app = make_app("enforce")
        client = app.test_client()
        missing = client.get("/api/private")
        unknown = client.get(
            "/api/private",
            headers={"Tailscale-User-Login": "someone-else@example.com"},
        )
        self.assertEqual(missing.status_code, 403)
        self.assertEqual(unknown.status_code, 403)
        self.assertEqual(missing.get_json(), unknown.get_json())
        self.assertEqual(
            missing.get_json()["error"]["code"], "portal_access_denied"
        )

    def test_shutdown_is_admin_only_while_diana_keeps_household_access(self):
        app = make_app("enforce")
        client = app.test_client()
        self.assertEqual(
            client.post(
                "/api/system/shutdown",
                headers={"Tailscale-User-Login": DAVID},
            ).status_code,
            200,
        )
        self.assertEqual(
            client.post(
                "/api/system/shutdown",
                headers={"Tailscale-User-Login": DIANA},
            ).status_code,
            403,
        )
        household = client.get(
            "/api/private", headers={"Tailscale-User-Login": DIANA}
        )
        self.assertEqual(household.status_code, 200)
        self.assertEqual(household.get_json()["access"]["role"], "household")

    def test_shadow_and_off_never_block_unknown_identity(self):
        for mode in ("shadow", "off"):
            with self.subTest(mode=mode):
                response = make_app(mode).test_client().get("/api/private")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["access"]["decision"], "deny")

    def test_denial_counter_records_are_content_neutral_and_distinguish_enforcement(self):
        shadow = make_app("shadow")
        shadow_client = shadow.test_client()
        with self.assertLogs(shadow.logger, level=logging.WARNING) as captured:
            shadow_client.get("/api/private")
            shadow_client.get(
                "/api/private",
                headers={"Tailscale-User-Login": "outside@example.invalid"},
            )
        counter_logs = [
            line for line in captured.output if "david_pi_access_counter" in line
        ]
        self.assertEqual(len(counter_logs), 2)
        self.assertTrue(
            all("mode=shadow disposition=shadow" in line for line in counter_logs)
        )
        encoded = " ".join(counter_logs).casefold()
        for forbidden in (
            "principal",
            "login",
            "route",
            "method",
            "reason",
            "gmail.com",
            "example.invalid",
        ):
            self.assertNotIn(forbidden, encoded)

        enforce = make_app("enforce")
        enforce_client = enforce.test_client()
        with self.assertLogs(enforce.logger, level=logging.WARNING) as captured:
            enforce_client.get("/api/private")
            enforce_client.get(
                "/api/private", headers={"Tailscale-User-Login": DIANA}
            )
        counter_logs = [
            line for line in captured.output if "david_pi_access_counter" in line
        ]
        self.assertEqual(len(counter_logs), 1)
        self.assertIn("mode=enforce disposition=blocked", counter_logs[0])

    def test_unknown_and_off_modes_emit_truthful_unenforced_dispositions(self):
        unknown = make_app("not-a-mode")
        with self.assertLogs(unknown.logger, level=logging.WARNING) as captured:
            unknown.test_client().get("/api/private")
        self.assertTrue(
            any(
                "david_pi_access_counter mode=enforce disposition=blocked" in line
                for line in captured.output
            )
        )

        off = make_app("off")
        with self.assertLogs(off.logger, level=logging.WARNING) as captured:
            off.test_client().get("/api/private")
        self.assertTrue(
            any(
                "david_pi_access_counter mode=off disposition=unenforced" in line
                for line in captured.output
            )
        )

    def test_public_and_strictly_enumerated_bearer_routes_are_exempt(self):
        app = make_app("enforce")
        client = app.test_client()
        for path in ("/health", "/static/example.js", "/david-pi-icon-192.png"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)
        # The central human gate deliberately lets this request reach the
        # route, while the route itself still requires its device credential.
        self.assertEqual(client.post("/api/v1/device-backup/uploads").status_code, 401)
        self.assertEqual(
            client.post(
                "/api/v1/device-backup/uploads",
                headers={"Authorization": "Bearer paired-device"},
            ).status_code,
            200,
        )
        # Pairing is not bearer-authenticated and therefore still needs one of
        # the two approved human identities.
        self.assertEqual(client.post("/api/v1/device-backup/pair").status_code, 403)
        self.assertEqual(
            client.post(
                "/api/v1/device-backup/pair",
                headers={"Tailscale-User-Login": DAVID},
            ).status_code,
            200,
        )

    def test_principal_matrix_covers_missing_unknown_household_admin_and_device(self):
        client = make_app("enforce").test_client()
        cases = (
            ("missing", "/api/private", {}, 403),
            (
                "unknown",
                "/api/private",
                {"Tailscale-User-Login": "unknown@example.invalid"},
                403,
            ),
            ("david", "/api/private", {"Tailscale-User-Login": DAVID}, 200),
            ("diana", "/api/private", {"Tailscale-User-Login": DIANA}, 200),
            (
                "device",
                "/api/v1/device-backup/uploads",
                {"Authorization": "Bearer paired-device"},
                200,
            ),
        )
        for principal, path, headers, expected in cases:
            with self.subTest(principal=principal):
                self.assertEqual(
                    client.post(path, headers=headers).status_code
                    if path.endswith("uploads")
                    else client.get(path, headers=headers).status_code,
                    expected,
                )

    def test_bearer_route_classification_fails_closed_for_new_or_wrong_methods(self):
        self.assertEqual(
            request_class("/api/v1/device-backup/status", "GET"),
            "device_bearer",
        )
        self.assertEqual(
            request_class("/api/v1/device-backup/status", "POST"), "portal"
        )
        self.assertEqual(
            request_class("/api/v1/device-backup/future-route", "GET"), "portal"
        )
        self.assertEqual(
            request_class("/api/v1/ios-backup/pair", "POST"), "portal"
        )

    def test_human_route_families_remain_inside_the_default_portal_gate(self):
        protected = (
            ("GET", "/"),
            ("GET", "/photos"),
            ("GET", "/status"),
            ("GET", "/assistant"),
            ("GET", "/games"),
            ("GET", "/chat"),
            ("GET", "/notes"),
            ("GET", "/movies"),
            ("GET", "/recipes"),
            ("GET", "/places"),
            ("GET", "/files"),
            ("GET", "/audiobooks"),
            ("GET", "/device-backup"),
            ("GET", "/device-backup/apk"),
            ("GET", "/api/photos"),
            ("GET", "/api/chat/users"),
            ("POST", "/api/device-backup/pairing-token"),
            ("POST", "/api/system/shutdown"),
            ("GET", "/media/thumb/example-id"),
        )
        for method, path in protected:
            with self.subTest(method=method, path=path):
                self.assertEqual(request_class(path, method), "portal")

    def test_new_human_routes_are_protected_without_inventory_changes(self):
        app = make_app("enforce")
        client = app.test_client()
        self.assertEqual(client.get("/future-human-feature").status_code, 403)
        self.assertEqual(
            client.get(
                "/future-human-feature",
                headers={"Tailscale-User-Login": DIANA},
            ).status_code,
            200,
        )

    def test_android_download_requires_an_allowed_portal_identity(self):
        client = make_app("enforce").test_client()
        self.assertEqual(client.get("/device-backup/apk").status_code, 403)
        self.assertEqual(
            client.get(
                "/device-backup/apk",
                headers={"Tailscale-User-Login": DIANA},
            ).status_code,
            200,
        )

    def test_shadow_logging_is_deduplicated_and_contains_no_sensitive_values(self):
        app = make_app("shadow")
        client = app.test_client()
        secret_query = "private-file-name.jpg"
        secret_login = "outsider@example.com"
        with self.assertLogs(app.logger, level=logging.WARNING) as captured:
            for _ in range(2):
                response = client.get(
                    f"/api/private?name={secret_query}",
                    headers={
                        "Tailscale-User-Login": secret_login,
                        "Authorization": "Bearer never-log-this",
                        "Cookie": "private-cookie=never-log-this",
                    },
                )
                self.assertEqual(response.status_code, 200)
        access_logs = [line for line in captured.output if "portal_access" in line]
        self.assertEqual(len(access_logs), 1)
        logged = access_logs[0]
        self.assertIn("route=/api/private", logged)
        self.assertNotIn(secret_query, logged)
        self.assertNotIn(secret_login, logged)
        self.assertNotIn("never-log-this", logged)


if __name__ == "__main__":
    unittest.main()
