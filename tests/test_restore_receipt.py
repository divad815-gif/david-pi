import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(DEPLOY))
SPEC = importlib.util.spec_from_file_location(
    "david_pi_restore_receipt", DEPLOY / "david_pi_restore_receipt.py"
)
receipt_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipt_module)


class RestoreReceiptTests(unittest.TestCase):
    def setUp(self):
        self.key = b"receipt-test-key" * 4
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.report = {
            "state": "isolated_data_verified",
            "mode": "full",
            "snapshot_id": "20260904T030000Z",
            "manifest_sha256": "a" * 64,
            "completed_at": self.now.isoformat(),
            "network_isolation_verified": True,
            "signed_database_evidence_preserved": True,
            "application_layout_ready": True,
            "storage_sentinel_ready": True,
            "application_boot_verified": False,
            "drill_complete": False,
        }
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name) / "recovery"
        self.state.mkdir(mode=0o700)
        sentinel = self.state / receipt_module.RECEIPT_SENTINEL
        sentinel.write_bytes(receipt_module.RECEIPT_SENTINEL_VALUE)
        sentinel.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self, **overrides):
        report = {**self.report, **overrides}
        return receipt_module.create_restore_receipt(
            report,
            self.key,
            issued_at=self.now,
            receipt_id="b" * 64,
        )

    def verify(self, document, **overrides):
        options = {
            "expected_snapshot_id": self.report["snapshot_id"],
            "expected_manifest_sha256": self.report["manifest_sha256"],
            "now": self.now,
        }
        options.update(overrides)
        return receipt_module.verify_restore_receipt(document, self.key, **options)

    def test_receipt_is_signed_content_neutral_and_scoped_to_data_restore(self):
        document = self.receipt()
        verified = self.verify(document)
        self.assertEqual(verified["result"], "passed")
        self.assertEqual(verified["scope"], "isolated_data_restore")
        self.assertEqual(verified["content_verification"], "complete_signed_data_tree")
        self.assertTrue(verified["network_isolation_verified"])
        self.assertFalse(verified["application_boot_verified"])
        self.assertFalse(verified["disaster_recovery_complete"])
        encoded = json.dumps(document).lower()
        for forbidden in (
            "/srv/",
            "/mnt/",
            "gmail.com",
            "filename",
            "database_count",
            "sampled_bytes",
            "device",
            "filesystem_uuid",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_tampering_or_unsupported_completion_claim_is_rejected(self):
        tampered = self.receipt()
        tampered["application_boot_verified"] = True
        with self.assertRaisesRegex(ValueError, "payload digest"):
            self.verify(tampered)

        forged = copy.deepcopy(self.receipt())
        payload = receipt_module.verify_authenticated_payload(
            forged, self.key, "restore evidence receipt"
        )
        payload["disaster_recovery_complete"] = True
        forged = receipt_module.sign_authenticated_payload(payload, self.key)
        with self.assertRaisesRegex(ValueError, "unsupported completion"):
            self.verify(forged)

    def test_nonisolated_or_incomplete_restore_cannot_issue_receipt(self):
        for field, value in (
            ("state", "data_verified"),
            ("network_isolation_verified", False),
            ("signed_database_evidence_preserved", False),
            ("application_layout_ready", False),
            ("storage_sentinel_ready", False),
            ("drill_complete", True),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.receipt(**{field: value})

    def test_stale_future_and_replayed_snapshot_receipts_fail_closed(self):
        document = self.receipt()
        with self.assertRaises(receipt_module.ReceiptStaleError):
            self.verify(
                document,
                now=self.now + timedelta(days=91),
                max_age_seconds=90 * 86400,
            )
        with self.assertRaisesRegex(ValueError, "from the future"):
            self.verify(document, now=self.now - timedelta(minutes=6))
        with self.assertRaises(receipt_module.ReceiptBindingError):
            self.verify(document, expected_snapshot_id="20260904T040000Z")
        with self.assertRaises(receipt_module.ReceiptBindingError):
            self.verify(document, expected_manifest_sha256="c" * 64)

    def test_receipt_cannot_be_issued_before_or_long_after_restore_completion(self):
        with self.assertRaisesRegex(ValueError, "not adjacent"):
            receipt_module.create_restore_receipt(
                {
                    **self.report,
                    "completed_at": (self.now + timedelta(seconds=1)).isoformat(),
                },
                self.key,
                issued_at=self.now,
            )
        with self.assertRaisesRegex(ValueError, "not adjacent"):
            receipt_module.create_restore_receipt(
                {
                    **self.report,
                    "completed_at": (
                        self.now
                        - timedelta(
                            seconds=receipt_module.MAX_ISSUANCE_DELAY_SECONDS + 1
                        )
                    ).isoformat(),
                },
                self.key,
                issued_at=self.now,
            )

    def test_publisher_and_reader_use_private_fixed_descriptor_path(self):
        first = self.receipt()
        path = receipt_module.publish_restore_receipt(
            self.state, first, expected_uid=os.getuid()
        )
        self.assertEqual(path, self.state / receipt_module.RECEIPT_FILE)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            receipt_module.read_restore_receipt(path, expected_uid=os.getuid()),
            first,
        )
        second = receipt_module.create_restore_receipt(
            {**self.report, "mode": "core"},
            self.key,
            issued_at=self.now,
            receipt_id="c" * 64,
        )
        receipt_module.publish_restore_receipt(
            self.state, second, expected_uid=os.getuid()
        )
        self.assertEqual(
            receipt_module.read_restore_receipt(path, expected_uid=os.getuid()),
            second,
        )
        self.assertEqual(list(self.state.glob(".restore-evidence.*.tmp")), [])

    def test_receipt_symlink_and_unsafe_parent_are_rejected_without_victim_read(self):
        victim = Path(self.temporary.name) / "victim.json"
        victim.write_text('{"private":"unchanged"}\n', encoding="utf-8")
        receipt_path = self.state / receipt_module.RECEIPT_FILE
        receipt_path.symlink_to(victim)
        with self.assertRaises((OSError, PermissionError, ValueError)):
            receipt_module.read_restore_receipt(
                receipt_path, expected_uid=os.getuid()
            )
        self.assertEqual(victim.read_text(encoding="utf-8"), '{"private":"unchanged"}\n')
        receipt_path.unlink()
        self.state.chmod(0o755)
        with self.assertRaisesRegex(PermissionError, "ownership or mode"):
            receipt_module.publish_restore_receipt(
                self.state, self.receipt(), expected_uid=os.getuid()
            )

    def test_receipt_sentinel_and_file_must_be_unique_private_regular_files(self):
        sentinel = self.state / receipt_module.RECEIPT_SENTINEL
        sentinel.chmod(0o700)
        with self.assertRaisesRegex(PermissionError, "sentinel is unsafe"):
            receipt_module.publish_restore_receipt(
                self.state, self.receipt(), expected_uid=os.getuid()
            )
        sentinel.chmod(0o600)
        path = receipt_module.publish_restore_receipt(
            self.state, self.receipt(), expected_uid=os.getuid()
        )
        hardlink = self.state / "receipt-hardlink"
        os.link(path, hardlink)
        with self.assertRaisesRegex(PermissionError, "receipt file is unsafe"):
            receipt_module.read_restore_receipt(path, expected_uid=os.getuid())


if __name__ == "__main__":
    unittest.main()
