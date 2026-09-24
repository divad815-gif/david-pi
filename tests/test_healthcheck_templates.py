import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "david-pi-healthcheck-ping.py"
spec = importlib.util.spec_from_file_location("david_pi_healthcheck_ping", SCRIPT)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class HealthcheckTemplateTests(unittest.TestCase):
    def test_ping_is_empty_post_and_state_only_changes_path(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return FakeResponse()

        base = "https://hc-ping.com/12345678-1234-1234-1234-123456789abc"
        for state, suffix in (
            ("start", "/start"),
            ("success", ""),
            ("failure", "/fail"),
        ):
            healthcheck.send_ping(base, state, opener=opener)
            request, timeout = calls[-1]
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.data, b"")
            self.assertEqual(request.full_url, base + suffix)
            self.assertEqual(timeout, 10)

    def test_url_file_must_be_private_regular_and_healthchecks_https(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.url"
            path.write_text(
                "https://hc-ping.com/12345678-1234-1234-1234-123456789abc\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            self.assertEqual(
                healthcheck.load_ping_url(path, expected_uid=os.getuid()),
                path.read_text(encoding="utf-8").strip(),
            )
            path.chmod(0o644)
            with self.assertRaises(PermissionError):
                healthcheck.load_ping_url(path, expected_uid=os.getuid())
            path.chmod(0o600)
            path.write_text("https://example.com/not-approved\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                healthcheck.load_ping_url(path, expected_uid=os.getuid())

    def test_url_file_hardlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backup.url"
            linked = Path(directory) / "linked.url"
            path.write_text(
                "https://hc-ping.com/12345678-1234-1234-1234-123456789abc\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            os.link(path, linked)
            with self.assertRaises(PermissionError):
                healthcheck.load_ping_url(path, expected_uid=os.getuid())

    def test_failure_output_never_contains_ping_credential(self):
        secret = "https://hc-ping.com/12345678-1234-1234-1234-123456789abc"
        output = io.StringIO()
        with patch.object(healthcheck, "load_ping_url", return_value=secret), patch.object(
            healthcheck,
            "send_ping",
            side_effect=RuntimeError(f"network failed for {secret}"),
        ), patch.dict(
            os.environ,
            {"DAVID_PI_HEALTHCHECK_URL_FILE": "/protected/credential"},
            clear=False,
        ), redirect_stderr(output):
            self.assertEqual(healthcheck.main(["ping", "failure"]), 1)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn("state=failure", output.getvalue())

    def test_relative_file_is_resolved_only_inside_systemd_credentials(self):
        self.assertEqual(
            healthcheck.configured_url_path(
                {
                    "DAVID_PI_HEALTHCHECK_URL_FILE": "healthcheck-url",
                    "CREDENTIALS_DIRECTORY": "/run/credentials/example.service",
                }
            ),
            Path("/run/credentials/example.service/healthcheck-url"),
        )
        with self.assertRaises(ValueError):
            healthcheck.configured_url_path(
                {"DAVID_PI_HEALTHCHECK_URL_FILE": "healthcheck-url"}
            )
        for unsafe in ("../healthcheck-url", "nested/healthcheck-url", ".", ".."):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                healthcheck.configured_url_path(
                    {
                        "DAVID_PI_HEALTHCHECK_URL_FILE": unsafe,
                        "CREDENTIALS_DIRECTORY": "/run/credentials/example.service",
                    }
                )
        with self.assertRaises(ValueError):
            healthcheck.configured_url_path(
                {
                    "DAVID_PI_HEALTHCHECK_URL_FILE": "healthcheck-url",
                    "CREDENTIALS_DIRECTORY": "relative/credentials",
                }
            )

    def test_systemd_templates_use_loadcredential_and_contain_no_url(self):
        for state in ("start", "success", "failure"):
            unit = (
                ROOT / "deploy" / f"david-pi-healthcheck-{state}@.service"
            ).read_text(encoding="utf-8")
            self.assertIn("LoadCredential=healthcheck-url:", unit)
            self.assertIn("DAVID_PI_HEALTHCHECK_URL_FILE=healthcheck-url", unit)
            self.assertIn(f"david-pi-healthcheck-ping {state}", unit)
            self.assertNotIn("hc-ping.com", unit)
            self.assertNotIn("http://", unit)
            self.assertNotIn("https://", unit)

    def test_source_dropins_wire_backup_and_status_lifecycle_without_secrets(self):
        checks = {
            "david-pi-backup.service.d": "backup",
            "david-pi-data-backup.service.d": "data-backup",
            "david-pi-b2-backup.service.d": "b2-backup",
            "david-pi-server-status.service.d": "server-status",
        }
        for directory, check in checks.items():
            with self.subTest(check=check):
                dropin = (
                    ROOT
                    / "deploy"
                    / "healthchecks"
                    / directory
                    / "30-healthchecks.conf"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    f"OnFailure=david-pi-healthcheck-failure@{check}.service",
                    dropin,
                )
                self.assertIn(
                    f"ExecStartPre=-/usr/bin/systemctl start david-pi-healthcheck-start@{check}.service",
                    dropin,
                )
                self.assertIn(
                    f"ExecStartPost=-/usr/bin/systemctl start david-pi-healthcheck-success@{check}.service",
                    dropin,
                )
                self.assertNotIn("hc-ping.com", dropin)
                self.assertNotIn("Environment=", dropin)

    def test_restore_job_is_loopback_only_and_receipt_gated(self):
        unit = (ROOT / "deploy" / "david-pi-restore-data-drill.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("PrivateNetwork=true", unit)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
        self.assertIn("--latest-snapshots /srv/backup-data/snapshots", unit)
        self.assertIn("--mode full", unit)
        self.assertIn("--receipt-directory /var/lib/david-pi-recovery", unit)
        self.assertIn("LoadCredential=manifest-key:", unit)
        self.assertIn(
            "OnFailure=david-pi-healthcheck-failure@restore-data.service", unit
        )
        self.assertNotIn("hc-ping.com", unit)

    def test_unconfigured_ping_fails_honestly_without_network_attempt(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(
            healthcheck, "send_ping"
        ) as send, redirect_stderr(output):
            self.assertEqual(healthcheck.main(["ping", "success"]), 78)
        send.assert_not_called()
        self.assertIn("configuration is unavailable", output.getvalue())


if __name__ == "__main__":
    unittest.main()
