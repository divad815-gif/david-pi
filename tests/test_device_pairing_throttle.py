import io
import logging
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from flask import Flask


IMPORT_DATA = tempfile.TemporaryDirectory()
os.environ["DAVID_PI_PLATFORM_DATA"] = str(Path(IMPORT_DATA.name) / "platform")
from modules.device_backup import init_device_backup, initialize_device_backup
from modules.access_control import init_access_control


def tearDownModule():
    IMPORT_DATA.cleanup()


class DevicePairingThrottleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "portal.db"
        os.environ["DAVID_PI_DISABLE_DEVICE_HOUSEKEEPING"] = "1"

    def tearDown(self):
        self.temporary.cleanup()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def make_app(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        init_device_backup(
            app,
            self.connect,
            lambda *args, **kwargs: {"id": "unused"},
            lambda *args, **kwargs: None,
            self.root,
        )
        return app

    def make_enforced_app(self):
        app = Flask(__name__)
        app.config.update(TESTING=True)
        init_access_control(app, mode="enforce")
        init_device_backup(
            app,
            self.connect,
            lambda *args, **kwargs: {"id": "unused"},
            lambda *args, **kwargs: None,
            self.root,
        )
        return app

    @staticmethod
    def identity_headers():
        return {
            "X-Test-Tailscale-Login": "david@example.test",
            "X-Test-Tailscale-Name": "David",
        }

    def create_token(self, client):
        response = client.post(
            "/api/device-backup/pairing-token",
            headers=self.identity_headers(),
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["pairing_token"]

    def invalid_attempt(self, client, supplied):
        return client.post(
            "/api/v1/device-backup/pair",
            headers=self.identity_headers(),
            json={"pairing_token": supplied, "device_name": "Test phone"},
        )

    @staticmethod
    def invalid_attempt_from(client, supplied, login):
        return client.post(
            "/api/v1/device-backup/pair",
            headers={
                "X-Test-Tailscale-Login": login,
                "X-Test-Tailscale-Name": "Unrecognized caller",
            },
            json={"pairing_token": supplied, "device_name": "Test phone"},
        )

    def test_sixth_failure_is_rate_limited_across_app_restart(self):
        first_app = self.make_app()
        token = self.create_token(first_app.test_client())
        for attempt in range(5):
            response = self.invalid_attempt(
                first_app.test_client(), f"invalid-before-restart-{attempt}"
            )
            self.assertEqual(response.status_code, 401)

        restarted_app = self.make_app()
        sixth = self.invalid_attempt(
            restarted_app.test_client(), "invalid-after-restart"
        )
        self.assertEqual(sixth.status_code, 429)
        self.assertEqual(sixth.headers["Retry-After"], "900")
        self.assertEqual(
            sixth.get_json()["error"]["code"], "pairing_rate_limited"
        )

        with self.connect() as connection:
            row = connection.execute(
                "SELECT failed_attempts,invalidated_at FROM device_pairing_tokens "
                "WHERE token_hash IS NOT NULL"
            ).fetchone()
            self.assertEqual(row["failed_attempts"], 6)
            self.assertIsNone(row["invalidated_at"])
        self.assertTrue(token)

    def test_tenth_failure_invalidates_active_token(self):
        app = self.make_app()
        client = app.test_client()
        token = self.create_token(client)
        for attempt in range(10):
            response = self.invalid_attempt(client, f"wrong-code-{attempt}")
            self.assertEqual(response.status_code, 401 if attempt < 5 else 429)

        self.assertEqual(
            self.invalid_attempt(client, "blocked-extra-attempt").status_code, 429
        )

        with self.connect() as connection:
            row = connection.execute(
                "SELECT failed_attempts,invalidated_at FROM device_pairing_tokens"
            ).fetchone()
            self.assertEqual(row["failed_attempts"], 10)
            self.assertIsNotNone(row["invalidated_at"])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_pairing_failures"
                ).fetchone()[0],
                10,
            )
            connection.execute(
                "UPDATE device_pairing_failures "
                "SET attempted_at='2000-01-01T00:00:00+00:00'"
            )

        rejected = self.invalid_attempt(client, token)
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.get_json()["error"]["code"], "pairing_invalid")

    def test_success_clears_prior_source_failures(self):
        app = self.make_app()
        client = app.test_client()
        token = self.create_token(client)
        self.assertEqual(self.invalid_attempt(client, "wrong-one").status_code, 401)
        self.assertEqual(self.invalid_attempt(client, "wrong-two").status_code, 401)
        paired = client.post(
            "/api/v1/device-backup/pair",
            headers=self.identity_headers(),
            json={"pairing_token": token, "device_name": "Test phone"},
        )
        self.assertEqual(paired.status_code, 201)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_pairing_failures"
                ).fetchone()[0],
                0,
            )

    def test_missing_identity_failures_still_invalidate_active_tokens(self):
        app = self.make_app()
        client = app.test_client()
        self.create_token(client)
        for attempt in range(10):
            response = client.post(
                "/api/v1/device-backup/pair",
                json={"pairing_token": f"anonymous-guess-{attempt}"},
            )
            self.assertEqual(response.status_code, 401 if attempt < 5 else 429)
        with self.connect() as connection:
            token = connection.execute(
                "SELECT failed_attempts,invalidated_at FROM device_pairing_tokens"
            ).fetchone()
        self.assertEqual(token["failed_attempts"], 10)
        self.assertIsNotNone(token["invalidated_at"])

    def test_global_limit_blocks_rotating_sources_and_is_bounded(self):
        first_app = self.make_app()
        client = first_app.test_client()
        self.create_token(client)
        for attempt in range(50):
            response = self.invalid_attempt_from(
                client,
                f"global-wrong-code-{attempt}",
                f"rotating-source-{attempt}@example.invalid",
            )
            self.assertEqual(response.status_code, 401)

        # Recreate the app against the same SQLite database to prove the
        # global window is shared across workers and process restarts.
        restarted_client = self.make_app().test_client()
        blocked = self.invalid_attempt_from(
            restarted_client,
            "global-wrong-code-50",
            "fresh-source@example.invalid",
        )
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "900")
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_pairing_failures"
                ).fetchone()[0],
                50,
            )
            token = connection.execute(
                "SELECT failed_attempts,invalidated_at FROM device_pairing_tokens"
            ).fetchone()
        self.assertEqual(token["failed_attempts"], 10)
        self.assertIsNotNone(token["invalidated_at"])

    def test_global_window_expires_without_resetting_token_failure_history(self):
        app = self.make_app()
        client = app.test_client()
        self.create_token(client)
        for attempt in range(50):
            self.invalid_attempt_from(
                client,
                f"expiring-wrong-code-{attempt}",
                f"expiring-source-{attempt}@example.invalid",
            )
        with self.connect() as connection:
            connection.execute(
                "UPDATE device_pairing_failures "
                "SET attempted_at='2000-01-01T00:00:00+00:00'"
            )
        fresh = self.invalid_attempt_from(
            client,
            "fresh-window-wrong-code",
            "fresh-window-source@example.invalid",
        )
        self.assertEqual(fresh.status_code, 401)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM device_pairing_failures"
                ).fetchone()[0],
                1,
            )

    def test_codes_are_never_logged_or_stored_in_plaintext(self):
        app = self.make_app()
        secret_code = "do-not-log-this-pairing-code"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        app.logger.addHandler(handler)
        try:
            response = self.invalid_attempt(app.test_client(), secret_code)
        finally:
            app.logger.removeHandler(handler)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(secret_code, stream.getvalue())
        self.assertNotIn(secret_code, response.get_data(as_text=True))
        with self.connect() as connection:
            stored = " ".join(
                str(value)
                for table in ("device_pairing_tokens", "device_pairing_failures")
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in row
            )
        self.assertNotIn(secret_code, stored)

    def test_member_can_create_only_her_own_android_credentials(self):
        client = self.make_enforced_app().test_client()
        diana_headers = {
            "X-Test-Tailscale-Login": "diana@example.test",
            "X-Test-Tailscale-Name": "Diana",
        }

        pairing = client.post(
            "/api/device-backup/pairing-token", headers=diana_headers
        )
        self.assertEqual(pairing.status_code, 200)
        paired = client.post(
            "/api/v1/device-backup/pair", headers=diana_headers,
            json={"pairing_token": pairing.get_json()["pairing_token"], "device_name": "Member Android"},
        )
        self.assertEqual(paired.status_code, 201)
        self.assertEqual(paired.get_json()["member_id"], "diana@example.test")
        for retired in ("/api/ios-backup/credential-file", "/api/ios-backup/shortcut"):
            self.assertEqual(client.post(retired, headers=diana_headers).status_code, 404)

        with self.connect() as connection:
            pairing_owner = connection.execute(
                "SELECT owner_user_id FROM device_pairing_tokens"
            ).fetchone()[0]
            device_owner = connection.execute(
                "SELECT owner_user_id FROM backup_devices"
            ).fetchone()[0]
        self.assertEqual(pairing_owner, "diana@example.test")
        self.assertEqual(device_owner, "diana@example.test")

        unknown_headers = {
            "X-Test-Tailscale-Login": "unknown@example.invalid",
            "X-Test-Tailscale-Name": "Unknown",
        }
        denied = client.post(
            "/api/device-backup/pairing-token", headers=unknown_headers
        )
        self.assertEqual(denied.status_code, 403)

    def test_existing_pairing_table_is_migrated_additively(self):
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE device_pairing_tokens (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    manual_code_hash TEXT NOT NULL UNIQUE,
                    owner_user_id TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                )"""
            )
            initialize_device_backup(connection)
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(device_pairing_tokens)"
                ).fetchall()
            }
        self.assertIn("failed_attempts", columns)
        self.assertIn("invalidated_at", columns)


if __name__ == "__main__":
    unittest.main()
