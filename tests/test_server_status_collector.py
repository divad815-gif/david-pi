import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "david-pi-server-status.py"
SPEC = importlib.util.spec_from_file_location("server_status_collector", SOURCE)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class ServerStatusCollectorTest(unittest.TestCase):
    def test_collector_schema_and_privacy_declaration(self):
        healthy = lambda: collector.card("healthy", "Fine.", {}, "", "OK")
        with patch.multiple(
            collector,
            portal_card=healthy, drive_card=healthy, storage_card=healthy,
            backups_card=healthy, temperature_card=healthy, tailscale_card=healthy,
            pihole_card=healthy, jobs_card=healthy, services_card=healthy,
            updates_card=healthy, databases_summary=lambda: [],
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
            updates_card=healthy, databases_summary=lambda: [],
        ):
            result = collector.collect()
        self.assertEqual(result["subsystems"]["portal"]["state"], "unavailable")
        self.assertNotIn("private raw failure", json.dumps(result))

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
                if args[0] == "lsblk" and "TRAN" in args:
                    return True, "usb\n"
                if args[0] == "lsblk":
                    return True, "sdb\n"
                return True, ""
            with patch.object(collector, "SENTINEL", sentinel), patch.object(collector, "command", side_effect=command):
                result = collector.drive_card()
            self.assertEqual(result["state"], "healthy")
            self.assertTrue(result["details"]["usb3"])
            self.assertFalse(result["details"]["usb3_uas"])

    def test_sata_data_drive_does_not_require_usb_or_uas(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "sentinel"
            sentinel.write_text(collector.SENTINEL_VALUE)
            def command(args, **_kwargs):
                if args[0] == "findmnt":
                    return True, json.dumps({"filesystems": [{"source": "/dev/sda1", "target": "/srv/data", "fstype": "ext4", "options": "rw,noatime", "uuid": "safe"}]})
                if args[0] == "lsblk" and "TRAN" in args:
                    return True, "sata\n"
                if args[0] == "lsblk":
                    return True, "sda\n"
                if args[0] == "smartctl":
                    return True, "SMART overall-health self-assessment test result: PASSED\n"
                return True, ""
            with patch.object(collector, "SENTINEL", sentinel), patch.object(collector, "command", side_effect=command):
                result = collector.drive_card()
            self.assertEqual(result["state"], "healthy")
            self.assertEqual(result["details"]["transport"], "sata")
            self.assertIsNone(result["details"]["usb3_uas"])
            self.assertEqual(result["details"]["smart"], "healthy")

    def test_generic_linux_temperature_is_used_without_vcgencmd(self):
        with patch.object(collector, "command", return_value=(False, "")), \
             patch.object(collector, "generic_temperature", return_value=(48.5, "linux-sysfs")), \
             patch.object(collector, "battery_details", return_value={"present": False, "capacity_percent": None, "status": "unavailable", "ac_online": None}):
            result = collector.temperature_card()
        self.assertEqual(result["details"]["temperature_c"], 48.5)
        self.assertEqual(result["details"]["temperature_sensor"], "linux-sysfs")
        self.assertEqual(result["state"], "healthy")

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


if __name__ == "__main__":
    unittest.main()
