import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask, jsonify, request

from modules.access_control import init_access_control, request_class
from modules.security import (
    EXACT_BEARER_EXEMPT_ROUTES,
    READ_ONLY_POSTS,
    SAFE_METHODS,
    init_security,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "route-policy.json"
DAVID = "david@example.test"
DIANA = "diana@example.test"
DEVICE_MEDIA_ROUTES = {
    "/api/v1/device-backup/uploads",
    "/api/v1/device-backup/uploads/<upload_id>",
    "/api/v1/device-backup/uploads/<upload_id>/complete",
    "/api/v1/ios-backup/upload",
    "/api/v1/ios-backup/upload-file",
}


def load_policy():
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def is_media_route(route):
    return (
        route.startswith("/photos")
        or route == "/api/upload"
        or route.startswith(("/api/photos", "/api/collections", "/api/slideshows", "/media/"))
        or route.startswith(("/mytube", "/api/mytube", "/api/files/uploads"))
        or route == "/api/chat/attachments/<attachment_id>/save-to-media"
        or route in DEVICE_MEDIA_ROUTES
    )


def concrete_path(route):
    replacements = {
        "<photo_id>": "photo-1",
        "<collection_id>": "collection-1",
        "<job_id>": "job-1",
        "<track_id>": "track-1",
        "<kind>": "preview",
        "<attachment_id>": "attachment-1",
        "<upload_id>": "upload-1",
        "<video_id>": "a" * 32,
        "<media_id>": "b" * 32,
        "<name>": "master.m3u8",
    }
    for marker, value in replacements.items():
        route = route.replace(marker, value)
    return route


def runtime_media_routes():
    script = """
import json
import app
print('__MEDIA_ROUTES__' + json.dumps(sorted(
    (method, rule.rule)
    for rule in app.app.url_map.iter_rules()
    for method in rule.methods
    if method in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}
    and (
        rule.rule.startswith('/photos')
        or rule.rule == '/api/upload'
        or rule.rule.startswith(('/api/photos', '/api/collections', '/api/slideshows', '/media/', '/mytube', '/api/mytube', '/api/files/uploads'))
        or rule.rule == '/api/chat/attachments/<attachment_id>/save-to-media'
        or rule.rule in %r
    )
)))
""" % sorted(DEVICE_MEDIA_ROUTES)
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment.update(
            {
                "PHOTO_DATA": directory,
                "DAVID_PI_PLATFORM_DATA": str(Path(directory) / "platform"),
                "DAVID_PI_FILES_DATA": str(Path(directory) / "files"),
                "DAVID_PI_CHAT_DATA": str(Path(directory) / "chat"),
                "DAVID_PI_DISABLE_METRICS": "1",
                "DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING": "1",
                "PIHOLE_SUMMARY": str(Path(directory) / "pihole-summary.json"),
                "DAVID_PI_CHAT_KEY_B64": base64.b64encode(
                    b"route-policy-test-key-32-bytes!!"
                ).decode("ascii"),
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    marker = "__MEDIA_ROUTES__"
    line = next(
        item for item in reversed(result.stdout.splitlines()) if item.startswith(marker)
    )
    return {tuple(item) for item in json.loads(line[len(marker):])}


def make_policy_app(entries):
    app = Flask(__name__)
    app.config["TESTING"] = True

    def identity():
        return {"owner_id": request.headers.get("Tailscale-User-Login")}

    init_security(app)
    init_access_control(app, mode="enforce", identity_provider=identity)

    for index, entry in enumerate(entries):
        def handler(access=entry["access"], **_route_values):
            if access == "device_bearer" and request.headers.get(
                "Authorization"
            ) != "Bearer paired-device":
                return jsonify(error="device_unauthorized"), 401
            return "", 204

        app.add_url_rule(
            entry["route"],
            endpoint=f"policy_route_{index}",
            view_func=handler,
            methods=entry["methods"],
        )
    return app


class MediaRoutePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = load_policy()
        cls.media_mutations = [
            entry for entry in cls.policy["mutations"] if entry.get("resource", "").startswith("media")
        ]
        cls.all_media_entries = [*cls.policy["media_read_routes"], *cls.media_mutations]

    def test_runtime_media_surface_is_fully_classified(self):
        declared = {
            (method, entry["route"])
            for entry in self.all_media_entries
            for method in entry["methods"]
        }
        runtime = runtime_media_routes()
        self.assertEqual(declared, runtime)
        self.assertTrue(all(is_media_route(route) for _, route in declared))

    def test_media_identity_matrix_denies_anonymous_and_preserves_household(self):
        matrix = self.policy["access_classes"]
        self.assertEqual(set(matrix["portal"]["allowed_principals"]), {"david", "diana"})
        self.assertEqual(set(matrix["device_bearer"]["allowed_principals"]), {"device"})
        for entry in self.all_media_entries:
            for method in entry["methods"]:
                path = concrete_path(entry["route"])
                expected_class = "device_bearer" if entry["access"] == "device_bearer" else "portal"
                self.assertEqual(request_class(path, method), expected_class, entry)

        app = make_policy_app(self.all_media_entries)
        csrf = "media-policy-csrf-token-with-adequate-length"
        for entry in self.all_media_entries:
            if entry["access"] != "portal":
                continue
            for method in entry["methods"]:
                path = concrete_path(entry["route"])
                for login, expected in ((None, 403), ("unknown@example.invalid", 403), (DAVID, 204), (DIANA, 204)):
                    client = app.test_client()
                    client.set_cookie("david_pi_csrf", csrf, domain="localhost")
                    headers = {"X-CSRF-Token": csrf}
                    if login:
                        headers["Tailscale-User-Login"] = login
                    response = client.open(path, method=method, headers=headers)
                    with self.subTest(route=entry["route"], method=method, login=login):
                        self.assertEqual(response.status_code, expected)

    def test_enforce_mode_covers_gallery_slideshow_and_music_reads(self):
        routes = {
            "/api/photos",
            "/media/original/<photo_id>",
            "/api/slideshows/options",
            "/api/slideshows/music/<track_id>",
        }
        entries = [
            entry
            for entry in self.policy["media_read_routes"]
            if entry["route"] in routes
        ]
        self.assertEqual({entry["route"] for entry in entries}, routes)
        app = make_policy_app(entries)
        for entry in entries:
            path = concrete_path(entry["route"])
            for login, expected in (
                (None, 403),
                ("unknown@example.invalid", 403),
                (DAVID, 204),
                (DIANA, 204),
            ):
                headers = {"Tailscale-User-Login": login} if login else {}
                response = app.test_client().get(path, headers=headers)
                with self.subTest(route=entry["route"], login=login):
                    self.assertEqual(response.status_code, expected)

    def test_destructive_media_and_collection_actions_are_owner_only(self):
        destructive = [
            entry for entry in self.media_mutations if entry["effect"] == "destructive"
        ]
        self.assertEqual(
            {(method, entry["route"]) for entry in destructive for method in entry["methods"]},
            {
                ("POST", "/api/photos/trash"),
                ("POST", "/api/photos/purge"),
                ("POST", "/api/photos/purge-all"),
                ("DELETE", "/api/photos/<photo_id>"),
                ("DELETE", "/api/collections/<collection_id>"),
                ("DELETE", "/api/mytube/videos/<video_id>"),
            },
        )
        self.assertTrue(all(entry["authorization"] == "owner_only" for entry in destructive))
        collection_change = next(
            entry for entry in self.media_mutations
            if entry["route"] == "/api/collections/<collection_id>" and entry["methods"] == ["PATCH"]
        )
        self.assertEqual(collection_change["authorization"], "owner_only")

    def test_csrf_and_http_method_contracts_are_explicit(self):
        self.assertEqual(SAFE_METHODS, {"GET", "HEAD", "OPTIONS"})
        self.assertEqual(
            READ_ONLY_POSTS,
            {"/api/collections/membership-state"},
        )
        self.assertTrue(
            all(entry["methods"] == ["GET"] for entry in self.policy["media_read_routes"])
        )
        for entry in self.media_mutations:
            for method in entry["methods"]:
                self.assertNotIn(method, SAFE_METHODS, entry)
            if entry["access"] == "device_bearer":
                self.assertEqual(entry["csrf"], "bearer_exempt", entry)
                self.assertIn((method, entry["route"]), EXACT_BEARER_EXEMPT_ROUTES)
            elif entry["route"] in READ_ONLY_POSTS:
                self.assertEqual(entry["csrf"], "read_only_exempt", entry)
                self.assertEqual(entry["authorization"], "visible_read_only", entry)
            else:
                self.assertEqual(entry["csrf"], "required", entry)

    def test_csrf_contract_is_enforced_for_every_media_mutation_class(self):
        app = make_policy_app(self.media_mutations)
        csrf = "media-policy-csrf-token-with-adequate-length"
        for entry in self.media_mutations:
            path = concrete_path(entry["route"])
            method = entry["methods"][0]
            client = app.test_client()
            if entry["csrf"] == "required":
                denied = client.open(
                    path, method=method, headers={"Tailscale-User-Login": DAVID}
                )
                client.set_cookie("david_pi_csrf", csrf, domain="localhost")
                allowed = client.open(
                    path,
                    method=method,
                    headers={"Tailscale-User-Login": DAVID, "X-CSRF-Token": csrf},
                )
                self.assertEqual((denied.status_code, allowed.status_code), (403, 204), entry)
            elif entry["csrf"] == "read_only_exempt":
                response = client.open(
                    path, method=method, headers={"Tailscale-User-Login": DIANA}
                )
                self.assertEqual(response.status_code, 204, entry)
            else:
                denied = client.open(path, method=method)
                allowed = client.open(
                    path,
                    method=method,
                    headers={"Authorization": "Bearer paired-device"},
                )
                self.assertEqual((denied.status_code, allowed.status_code), (401, 204), entry)


if __name__ == "__main__":
    unittest.main()
