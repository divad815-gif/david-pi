import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from datetime import datetime, timezone


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "david-pi-server-status.py"
SPEC = importlib.util.spec_from_file_location("server_status_collector", SOURCE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
import david_pi_restore_receipt as receipt_module


class ServerStatusCollectorTest(unittest.TestCase):
    def test_update_check_requires_fresh_success_not_zero_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            stamp = Path(temporary) / "success"
            with patch.object(collector, "UPDATE_SUCCESS", stamp), patch.object(collector, "REBOOT_REQUIRED", Path(temporary) / "reboot"), patch.object(collector, "command", return_value=(True, "")):
                self.assertEqual(collector.updates_card()["evidence_code"], "UPDATES_CHECK_STALE")
                stamp.touch()
                self.assertEqual(collector.updates_card()["state"], "healthy")
                os.utime(stamp, (1, 1))
                self.assertEqual(collector.updates_card()["state"], "warning")

    def test_disk_percentage_accounts_for_reserved_blocks(self):
        from collections import namedtuple
        usage = namedtuple("Usage", "total used free")(100, 80, 15)
        with patch.object(collector.shutil, "disk_usage", return_value=usage):
            self.assertEqual(collector.disk_details(Path("/"))["used_percent"], 84.2)

    def test_restore_key_uses_only_absolute_or_systemd_credential_path(self):
        self.assertEqual(
            collector.credential_path(
                "restore-evidence-key",
                {"CREDENTIALS_DIRECTORY": "/run/credentials/status.service"},
            ),
            Path("/run/credentials/status.service/restore-evidence-key"),
        )
        self.assertEqual(
            collector.credential_path("/protected/key", {}), Path("/protected/key")
        )
        for unsafe in ("../key", "nested/key", "", "."):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                collector.credential_path(unsafe, {"CREDENTIALS_DIRECTORY": "/run/credentials"})

    def test_expected_databases_include_audiobook_playback_queue(self):
        relative_paths = [path.relative_to(collector.DATA).as_posix() for path in collector.EXPECTED_DATABASES]
        self.assertIn(".david-pi-operations/audiobook/playback-queue.db", relative_paths)
        self.assertEqual(len(relative_paths), 12)

    def test_expected_database_inventory_falls_back_only_until_isolated_queue_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            isolated = root / "operations/playback-queue.db"
            legacy = root / "audiobooks/playback-queue.db"
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            with patch.multiple(
                collector,
                AUDIOBOOK_QUEUE=isolated,
                LEGACY_AUDIOBOOK_QUEUE=legacy,
                EXPECTED_DATABASES=(isolated,),
            ):
                self.assertEqual(collector.expected_databases(), (legacy,))
                isolated.parent.mkdir(parents=True)
                isolated.touch()
                self.assertEqual(collector.expected_databases(), (isolated,))

    def test_host_uptime_is_bounded_and_malformed_input_is_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            uptime = Path(temporary) / "uptime"
            uptime.write_text("90061.72 123.45\n")
            self.assertEqual(collector.host_uptime_seconds(uptime), 90061)
            uptime.write_text("not-a-number\n")
            self.assertIsNone(collector.host_uptime_seconds(uptime))
            self.assertIsNone(collector.host_uptime_seconds(Path(temporary) / "missing"))

    def test_collector_schema_and_privacy_declaration(self):
        healthy = lambda: collector.card("healthy", "Fine.", {}, "", "OK")
        with patch.multiple(
            collector,
            portal_card=healthy, drive_card=healthy, storage_card=healthy,
            backups_card=healthy, temperature_card=healthy, tailscale_card=healthy,
            pihole_card=healthy, jobs_card=healthy, services_card=healthy,
            updates_card=healthy, access_control_card=healthy,
            databases_summary=lambda: [],
        ):
            result = collector.collect()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["state"], "healthy")
        self.assertTrue(all(value is False for value in result["privacy"].values()))
        encoded = json.dumps(result).lower()
        for forbidden in ("password", "query_history", "client_address", "private_key"):
            self.assertNotIn(forbidden, encoded)

    def test_one_failed_metric_does_not_break_snapshot(self):
        healthy = lambda: collector.card("healthy", "Fine.", {}, "", "OK")
        def failed():
            raise RuntimeError("private raw failure")
        with patch.multiple(
            collector,
            portal_card=failed, drive_card=healthy, storage_card=healthy,
            backups_card=healthy, temperature_card=healthy, tailscale_card=healthy,
            pihole_card=healthy, jobs_card=healthy, services_card=healthy,
            updates_card=healthy, access_control_card=healthy,
            databases_summary=lambda: [],
        ):
            result = collector.collect()
        self.assertEqual(result["subsystems"]["portal"]["state"], "unavailable")
        self.assertNotIn("private raw failure", json.dumps(result))

    def test_access_card_aggregates_only_fixed_content_neutral_records(self):
        def fake_command(arguments, **kwargs):
            if arguments[:2] == ["docker", "inspect"]:
                self.assertIn(collector.ACCESS_MODE_LABEL, arguments[-1])
                return True, "enforce\n"
            if arguments[:2] == ["docker", "logs"]:
                self.assertTrue(kwargs.get("include_stderr"))
                return True, "\n".join(
                    (
                        "prefix david_pi_access_counter mode=enforce disposition=blocked",
                        "david_pi_access_counter mode=enforce disposition=blocked",
                        "david_pi_access_counter mode=shadow disposition=shadow",
                        # A contradictory pair is not valid issuer evidence.
                        "david_pi_access_counter mode=enforce disposition=shadow",
                        "portal_access principal=private route=/private-item",
                    )
                )
            return False, ""

        with patch.object(collector, "command", side_effect=fake_command):
            result = collector.access_control_card()
        self.assertEqual(result["state"], "healthy")
        self.assertEqual(result["evidence_code"], "ACCESS_ENFORCEMENT_ACTIVE")
        self.assertTrue(result["details"]["enforcement_active"])
        self.assertEqual(result["details"]["observed_denials_total"], 3)
        self.assertEqual(result["details"]["blocked_denials_total"], 2)
        self.assertEqual(result["details"]["shadow_denials_total"], 1)
        encoded = json.dumps(result).casefold()
        for forbidden in ("principal", "route", "private-item"):
            self.assertNotIn(forbidden, encoded)

    def test_access_mode_unknown_or_counter_unavailable_can_never_be_green(self):
        def invalid_command(arguments, **_kwargs):
            if arguments[:2] == ["docker", "inspect"]:
                return True, "surprise-mode\n"
            return False, "private log error"

        with patch.object(collector, "command", side_effect=invalid_command):
            unknown = collector.access_control_card()
        self.assertEqual(unknown["state"], "warning")
        self.assertEqual(unknown["evidence_code"], "ACCESS_MODE_UNKNOWN")
        self.assertEqual(unknown["details"]["configured_mode"], "unknown")
        self.assertEqual(unknown["details"]["effective_mode"], "enforce")
        self.assertFalse(unknown["details"]["configuration_valid"])
        self.assertIsNone(unknown["details"]["observed_denials_total"])
        self.assertNotIn("private log error", json.dumps(unknown))

        def unavailable_logs(arguments, **_kwargs):
            if arguments[:2] == ["docker", "inspect"]:
                return True, "enforce\n"
            return False, ""

        with patch.object(collector, "command", side_effect=unavailable_logs):
            unavailable = collector.access_control_card()
        self.assertEqual(unavailable["state"], "warning")
        self.assertEqual(
            unavailable["evidence_code"], "ACCESS_COUNTERS_UNAVAILABLE"
        )
        self.assertFalse(unavailable["details"]["counter_window_complete"])

    def test_access_counter_cap_and_shadow_or_off_modes_are_never_green(self):
        def capped_command(arguments, **_kwargs):
            if arguments[:2] == ["docker", "inspect"]:
                return True, "enforce\n"
            return True, "\n".join(
                (
                    "david_pi_access_counter mode=enforce disposition=blocked",
                    "david_pi_access_counter mode=enforce disposition=blocked",
                )
            )

        with patch.object(collector, "ACCESS_COUNTER_LINE_LIMIT", 2), \
             patch.object(collector, "command", side_effect=capped_command):
            capped = collector.access_control_card()
        self.assertEqual(capped["state"], "warning")
        self.assertEqual(capped["evidence_code"], "ACCESS_COUNTER_WINDOW_CAPPED")
        self.assertFalse(capped["details"]["counter_window_complete"])

        for mode, code in (
            ("shadow", "ACCESS_SHADOW_ACTIVE"),
            ("off", "ACCESS_ENFORCEMENT_OFF"),
        ):
            with self.subTest(mode=mode):
                def known_mode(arguments, **_kwargs):
                    if arguments[:2] == ["docker", "inspect"]:
                        return True, mode + "\n"
                    return True, ""

                with patch.object(collector, "command", side_effect=known_mode):
                    result = collector.access_control_card()
                self.assertEqual(result["state"], "warning")
                self.assertEqual(result["evidence_code"], code)
                self.assertFalse(result["details"]["enforcement_active"])

    def test_access_mode_probe_failure_is_unavailable_not_green(self):
        with patch.object(collector, "command", return_value=(False, "private")) as command:
            result = collector.access_control_card()
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["evidence_code"], "ACCESS_STATE_UNAVAILABLE")
        self.assertEqual(command.call_count, 1)
        self.assertNotIn("private", json.dumps(result))

    def test_atomic_replacement_leaves_valid_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "server-status.json"
            with patch.object(collector, "OUTPUT", output):
                collector.publish({"schema_version": 1, "value": "first"})
                collector.publish({"schema_version": 1, "value": "second"})
            self.assertEqual(json.loads(output.read_text())["value"], "second")
            self.assertEqual(list(Path(temporary).glob(".server-status-*")), [])

    def test_backup_thresholds_and_independent_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "backup.json"
            status.write_text(json.dumps({
                "ok": True, "last_success": collector.now_iso(), "database_count": 7,
            }))
            root = Path(temporary) / "backups"
            root.mkdir()
            with patch.object(collector, "BACKUP_STATUS", status), \
                 patch.object(collector, "BACKUPS", root), \
                 patch.object(collector, "unit_state", return_value={"active": "active"}):
                result = collector.backups_card()
            self.assertEqual(result["state"], "warning")
            self.assertFalse(result["details"]["independent_data_backup"]["configured"])
            self.assertEqual(result["evidence_code"], "DATA_BACKUP_NOT_CONFIGURED")

    def test_backup_copy_is_not_called_healthy_without_offsite_restore_proof(self):
        self.addCleanup(patch.stopall)
        patch.object(collector, "OFFSITE_REQUIRED", True).start()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_status = root / "backup.json"
            backup_status.write_text(json.dumps({
                "ok": True, "quick_check": True, "last_success": collector.now_iso(),
                "database_count": len(collector.EXPECTED_DATABASES),
            }))
            independent_status = root / "independent.json"
            independent_status.write_text(json.dumps({
                "state": "healthy", "completed_at": collector.now_iso(),
                "database_count": len(collector.EXPECTED_DATABASES),
            }))
            offsite_status = root / "offsite.json"
            offsite_status.write_text(json.dumps({
                "state": "uploaded_pending_restore", "completed_at": collector.now_iso(),
                "source_snapshot_confirmed": True, "repository_sample_checked": True,
                "object_lock_verified": False, "offsite_restore_verified": False,
                "pruning_enabled": False,
            }))
            sentinel = root / "sentinel"
            sentinel.write_text(collector.INDEPENDENT_BACKUP_SENTINEL_VALUE)

            def command(arguments, **_kwargs):
                if arguments[0] == "mountpoint":
                    return True, ""
                if arguments[0] == "findmnt":
                    return True, "source-uuid\n" if str(collector.DATA) in arguments else "backup-uuid\n"
                return False, ""

            with patch.object(collector, "BACKUP_STATUS", backup_status), \
                 patch.object(collector, "INDEPENDENT_BACKUP_STATUS", independent_status), \
                 patch.object(collector, "B2_BACKUP_STATUS", offsite_status), \
                 patch.object(collector, "INDEPENDENT_BACKUP_SENTINEL", sentinel), \
                 patch.object(collector, "command", side_effect=command), \
                 patch.object(collector, "unit_state", return_value={"active": "active"}):
                result = collector.backups_card()
            self.assertEqual(result["state"], "warning")
            self.assertEqual(result["evidence_code"], "OFFSITE_RESTORE_PENDING")
            self.assertFalse(result["details"]["offsite_backup"]["offsite_restore_verified"])

    def test_restore_receipt_is_verified_and_bound_to_current_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_path = root / "manifest.key"
            key_path.write_bytes(b"k" * 64)
            key_path.chmod(0o600)
            evidence_directory = root / "recovery"
            evidence_directory.mkdir(mode=0o700)
            sentinel = evidence_directory / receipt_module.RECEIPT_SENTINEL
            sentinel.write_bytes(receipt_module.RECEIPT_SENTINEL_VALUE)
            sentinel.chmod(0o600)
            now = datetime.now(timezone.utc).replace(microsecond=0)
            independent = {"snapshot": "20260904T030000Z", "manifest_sha256": "a" * 64}
            report = {
                "state": "isolated_data_verified",
                "mode": "full",
                "snapshot_id": independent["snapshot"],
                "manifest_sha256": independent["manifest_sha256"],
                "completed_at": now.isoformat(),
                "network_isolation_verified": True,
                "signed_database_evidence_preserved": True,
                "application_layout_ready": True,
                "storage_sentinel_ready": True,
                "application_boot_verified": False,
                "drill_complete": False,
            }
            key = collector.load_signing_key(key_path)
            receipt = receipt_module.create_restore_receipt(
                report, key, issued_at=now, receipt_id="b" * 64
            )
            receipt_path = receipt_module.publish_restore_receipt(
                evidence_directory, receipt, expected_uid=os.getuid()
            )
            environment = {collector.RESTORE_EVIDENCE_KEY_ENV: str(key_path)}
            with patch.object(collector, "RESTORE_EVIDENCE", receipt_path):
                verified = collector.restore_evidence_summary(independent, environment)
                replayed = collector.restore_evidence_summary(
                    {**independent, "snapshot": "20260904T040000Z"}, environment
                )
            self.assertEqual(verified["state"], "isolated_data_verified")
            self.assertTrue(verified["current_snapshot_match"])
            self.assertFalse(verified["application_boot_verified"])
            self.assertFalse(verified["disaster_recovery_complete"])
            self.assertEqual(replayed["state"], "snapshot_mismatch")
            encoded = json.dumps(verified).lower()
            self.assertNotIn(str(root).lower(), encoded)
            self.assertNotIn(independent["snapshot"].lower(), encoded)
            self.assertNotIn(independent["manifest_sha256"], encoded)

    def test_fully_current_copies_are_not_green_without_complete_restore_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup_status = root / "backup.json"
            backup_status.write_text(json.dumps({
                "ok": True, "quick_check": True, "last_success": collector.now_iso(),
                "database_count": len(collector.EXPECTED_DATABASES),
            }))
            independent_status = root / "independent.json"
            independent_status.write_text(json.dumps({
                "state": "healthy", "completed_at": collector.now_iso(),
                "snapshot": "20260904T030000Z", "manifest_sha256": "a" * 64,
                "database_count": len(collector.EXPECTED_DATABASES),
            }))
            offsite_status = root / "offsite.json"
            offsite_status.write_text(json.dumps({
                "state": "healthy", "completed_at": collector.now_iso(),
                "source_snapshot_confirmed": True, "repository_sample_checked": True,
                "object_lock_verified": True, "offsite_restore_verified": True,
                "pruning_enabled": False,
            }))
            sentinel = root / "sentinel"
            sentinel.write_text(collector.INDEPENDENT_BACKUP_SENTINEL_VALUE)

            def command(arguments, **_kwargs):
                if arguments[0] == "mountpoint":
                    return True, ""
                if arguments[0] == "findmnt":
                    return True, "source-uuid\n" if str(collector.DATA) in arguments else "backup-uuid\n"
                return False, ""

            with patch.object(collector, "BACKUP_STATUS", backup_status), \
                 patch.object(collector, "INDEPENDENT_BACKUP_STATUS", independent_status), \
                 patch.object(collector, "B2_BACKUP_STATUS", offsite_status), \
                 patch.object(collector, "INDEPENDENT_BACKUP_SENTINEL", sentinel), \
                 patch.object(collector, "command", side_effect=command), \
                 patch.object(collector, "unit_state", return_value={"active": "active"}), \
                 patch.object(collector, "restore_evidence_summary", return_value={
                     "configured": True, "state": "isolated_data_verified",
                     "scope": "isolated_data_restore", "last_verified": collector.now_iso(),
                     "age_hours": 0.0, "current_snapshot_match": True,
                     "network_isolation_verified": True,
                     "application_boot_verified": False,
                     "disaster_recovery_complete": False,
                 }):
                result = collector.backups_card()
            self.assertEqual(result["state"], "warning")
            self.assertEqual(result["evidence_code"], "RESTORE_APPLICATION_BOOT_PENDING")
            self.assertFalse(
                result["details"]["independent_data_backup"]["restore_evidence"][
                    "disaster_recovery_complete"
                ]
            )

    def test_missing_mount_or_sentinel_is_critical(self):
        with patch.object(collector, "command", return_value=(False, "")):
            result = collector.drive_card()
        self.assertEqual(result["state"], "critical")
        self.assertFalse(result["details"]["mounted"])
        self.assertFalse(result["details"]["sentinel_valid"])

    def test_usb3_without_uas_is_healthy_when_drive_has_no_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "sentinel"
            sentinel.write_text(collector.SENTINEL_VALUE)
            def command(args, **_kwargs):
                if args[0] == "findmnt":
                    return True, json.dumps({"filesystems": [{"source": "/dev/sdb1", "target": "/srv/data", "fstype": "ext4", "options": "rw,noatime", "uuid": "safe"}]})
                if args[0] == "lsusb":
                    return True, "/: Bus 002 Driver=xhci_hcd 5000M\n|__ Mass Storage Driver=usb-storage 5000M"
                return True, ""
            with patch.object(collector, "SENTINEL", sentinel), patch.object(collector, "command", side_effect=command):
                result = collector.drive_card()
            self.assertEqual(result["state"], "healthy")
            self.assertTrue(result["details"]["usb3"])
            self.assertFalse(result["details"]["usb3_uas"])

    def test_storage_thresholds_turn_red_at_ninety_percent(self):
        external = {"total_gb": 100, "used_gb": 91, "free_gb": 9, "used_percent": 91}
        microsd = {"total_gb": 50, "used_gb": 10, "free_gb": 40, "used_percent": 20}
        with patch.object(collector, "disk_details", side_effect=[external, microsd]), \
             patch.object(collector, "size_gb", return_value=0), \
             patch.object(collector, "directory_size", return_value=0):
            result = collector.storage_card()
        self.assertEqual(result["state"], "critical")

    def test_pihole_stale_preserves_last_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            summary = Path(temporary) / "pihole.json"
            summary.write_text(json.dumps({
                "updated_at": "2020-01-01T00:00:00+00:00", "total": 123,
                "blocked": 10, "blocked_percent": 8.1, "stale": False,
            }))
            with patch.object(collector, "PIHOLE", summary):
                result = collector.pihole_card()
            self.assertEqual(result["state"], "warning")
            self.assertEqual(result["details"]["total_queries"], 123)

    def test_active_and_historical_throttle_are_distinct(self):
        def fake_command(arguments, **_):
            if "measure_temp" in arguments:
                return True, "temp=55.0'C\n"
            if "get_throttled" in arguments:
                return True, "throttled=0x50000\n"
            if arguments[0] == "journalctl":
                return True, ""
            return False, ""
        with patch.object(collector, "command", side_effect=fake_command):
            result = collector.temperature_card()
        self.assertFalse(result["details"]["active_throttling"])
        self.assertTrue(result["details"]["historical_throttling"])
        self.assertEqual(result["state"], "warning")

    def test_tailscale_funnel_or_lan_listener_is_critical(self):
        status = json.dumps({"BackendState": "Running", "Self": {
            "Online": True, "TailscaleIPs": ["100.64.0.1"], "HostName": "david-pi",
        }})
        def fake_command(arguments, **_):
            joined = " ".join(arguments)
            if joined == "tailscale status --json":
                return True, status
            if joined == "tailscale serve status":
                return True, "https://example.ts.net (tailnet only)\n|-- / proxy http://127.0.0.1:8090\n"
            if joined == "tailscale funnel status":
                return True, "Funnel on: https://public.example\n"
            if arguments[0] == "ss":
                return True, "LISTEN 0 4096 0.0.0.0:80 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:8090 0.0.0.0:*\n"
            if arguments[0] == "curl":
                return True, "0.02"
            return False, ""
        with patch.object(collector, "command", side_effect=fake_command):
            result = collector.tailscale_card()
        self.assertEqual(result["state"], "critical")
        self.assertFalse(result["details"]["funnel_disabled"])
        self.assertFalse(result["details"]["lan_port_80_closed"])

    def test_portal_without_resource_limits_is_warning_not_healthy(self):
        state = json.dumps({
            "Status": "running", "Running": True,
            "StartedAt": collector.now_iso(), "Health": {"Status": "healthy"},
        })
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "sentinel"
            sentinel.write_text(collector.SENTINEL_VALUE)

            def command(arguments, **_kwargs):
                if arguments[:2] == ["docker", "inspect"]:
                    return True, f'{state}|image|10001:10001|0|0|0\n'
                if arguments[0] == "curl":
                    return True, "0.02"
                if arguments[:2] == ["docker", "stats"]:
                    return True, json.dumps({"MemUsage": "10MiB / 1GiB", "MemPerc": "1%", "CPUPerc": "1%", "PIDs": "4"})
                return False, ""

            with patch.object(collector, "SENTINEL", sentinel), patch.object(collector, "command", side_effect=command):
                result = collector.portal_card()
            self.assertEqual(result["state"], "warning")
            self.assertEqual(result["evidence_code"], "PORTAL_LIMITS_UNENFORCED")
            self.assertFalse(result["details"]["resource_limits_enforced"])

    def test_unavailable_package_probe_is_not_reported_current(self):
        with patch.object(collector, "command", return_value=(False, "")):
            result = collector.updates_card()
        self.assertEqual(result["state"], "unavailable")
        self.assertIsNone(result["details"]["pending_packages"])
        self.assertEqual(result["evidence_code"], "UPDATES_CHECK_UNAVAILABLE")

    def test_unavailable_job_database_is_not_reported_healthy(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(collector, "DATA", Path(temporary)), \
             patch.object(collector, "command", return_value=(False, "")), \
             patch.object(collector, "size_gb", return_value=0):
            result = collector.jobs_card()
        self.assertEqual(result["state"], "unavailable")
        self.assertFalse(result["details"]["slideshows"]["database_available"])

    def test_maintenance_must_be_available_running_and_exactly_healthy(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            with collector.sqlite3.connect(data / "photos.db") as database:
                database.execute(
                    "CREATE TABLE slideshow_jobs(status TEXT, updated_at TEXT, created_at TEXT)"
                )

            def command_for(maintenance_state):
                def fake(arguments, **_kwargs):
                    if arguments[:2] != ["docker", "inspect"]:
                        return False, ""
                    name = arguments[2]
                    if name == "david-pi-maintenance" and maintenance_state == "missing":
                        return False, ""
                    health = (
                        maintenance_state
                        if name == "david-pi-maintenance"
                        else "healthy"
                        if name in {
                            "david-pi-slideshow-worker",
                            "david-pi-device-backup-worker",
                        }
                        else None
                    )
                    running = maintenance_state != "stopped" if name == "david-pi-maintenance" else True
                    state = {"Running": running, "Status": "running" if running else "exited"}
                    if health not in (None, "missing", "unknown", "stopped"):
                        state["Health"] = {"Status": health}
                    return True, json.dumps(state) + "|0\n"
                return fake

            for maintenance_state in ("starting", "missing", "unknown", "unhealthy", "stopped"):
                with self.subTest(maintenance_state=maintenance_state), \
                     patch.object(collector, "DATA", data), \
                     patch.object(collector, "command", side_effect=command_for(maintenance_state)), \
                     patch.object(collector, "size_gb", return_value=0):
                    result = collector.jobs_card()
                self.assertEqual(result["state"], "warning")
                if maintenance_state == "unknown":
                    self.assertEqual(
                        result["details"]["workers"]["david-pi-maintenance"]["health"],
                        "unknown",
                    )

            with patch.object(collector, "DATA", data), \
                 patch.object(collector, "command", side_effect=command_for("healthy")), \
                 patch.object(collector, "size_gb", return_value=0):
                result = collector.jobs_card()
            self.assertEqual(result["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
