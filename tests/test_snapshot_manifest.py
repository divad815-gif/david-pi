import fcntl
import errno
import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "david_pi_snapshot_manifest.py"
SPEC = importlib.util.spec_from_file_location("snapshot_manifest", SCRIPT)
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)


class SnapshotManifestTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "databases" / "platform").mkdir(parents=True)
        (self.snapshot / "data" / "originals").mkdir(parents=True)
        (self.snapshot / "config").mkdir(parents=True)
        self.snapshot.chmod(0o700)
        self.database = self.snapshot / "databases" / "platform" / "platform.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
            connection.execute(
                "CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
            )
            connection.execute("INSERT INTO parent VALUES (1)")
            connection.execute("INSERT INTO child VALUES (1, 1)")
        (self.snapshot / "data" / "originals" / "fixture.bin").write_bytes(b"fixture")
        (self.snapshot / "config" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        self.key_file = self.root / "manifest.key"
        self.key_file.write_bytes(b"k" * 64)
        self.key_file.chmod(0o600)
        self.output = self.snapshot / "MANIFEST.json"
        self.metadata = {
            "snapshot_id": "20260903T000000Z",
            "started_at": "2026-09-03T00:00:00Z",
            "completed_at": "2026-09-03T00:01:00Z",
            "source_device": "/dev/source",
            "source_uuid": "source-uuid",
            "source_st_dev": 101,
            "backup_device": "/dev/backup",
            "backup_uuid": "backup-uuid",
            "backup_st_dev": 202,
            "portal_image": "david-family-photos:test",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def write(self):
        return manifest_module.write_manifest(
            self.snapshot,
            self.output,
            self.metadata,
            manifest_module.load_signing_key(self.key_file),
        )

    def verify(self):
        return manifest_module.verify_manifest(
            self.snapshot,
            self.output,
            manifest_module.load_signing_key(self.key_file),
        )

    def test_signed_manifest_captures_integrity_without_content(self):
        created = self.write()
        verified = self.verify()
        self.assertEqual(created["schema_version"], 4)
        self.assertEqual(verified["snapshot_id"], self.metadata["snapshot_id"])
        self.assertEqual(len(verified["databases"]), 1)
        self.assertEqual(verified["databases"][0]["quick_check"], "ok")
        self.assertEqual(verified["databases"][0]["mode"], "0644")
        self.assertEqual(verified["databases"][0]["uid"], os.getuid())
        self.assertEqual(verified["databases"][0]["gid"], os.getgid())
        self.assertEqual(verified["database_tree"]["file_count"], 1)
        self.assertEqual(verified["database_tree"]["symlink_count"], 0)
        self.assertEqual(verified["content"]["file_count"], 1)
        self.assertEqual(verified["content"]["directory_count"], 1)
        self.assertEqual(verified["content"]["symlink_count"], 0)
        self.assertNotIn("fixture", json.dumps(created))
        self.assertEqual(
            created["evidence"]["path"], manifest_module.RECOVERY_EVIDENCE_FILE
        )
        evidence = self.snapshot / manifest_module.RECOVERY_EVIDENCE_FILE
        self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
        self.assertIn("originals/fixture.bin", evidence.read_text(encoding="utf-8"))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertRegex(created["created_at"], r"^20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_manifest_metadata_tampering_is_rejected(self):
        self.write()
        payload = json.loads(self.output.read_text(encoding="utf-8"))
        payload["snapshot_id"] = "forged"
        self.output.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "payload digest"):
            self.verify()

    def test_private_recovery_evidence_tampering_is_rejected(self):
        self.write()
        evidence = self.snapshot / manifest_module.RECOVERY_EVIDENCE_FILE
        contents = bytearray(evidence.read_bytes())
        contents[-2] = ord(" ") if contents[-2] != ord(" ") else ord("x")
        evidence.write_bytes(contents)
        evidence.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "evidence hash"):
            self.verify()

    def test_manifest_output_symlink_does_not_touch_victim(self):
        victim = self.root / "manifest-victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        self.output.symlink_to(victim)
        with self.assertRaisesRegex(PermissionError, "output is unsafe"):
            self.write()
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_manifest_output_hardlink_does_not_touch_victim(self):
        victim = self.root / "manifest-hardlink-victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        os.link(victim, self.output)
        with self.assertRaisesRegex(PermissionError, "output is unsafe"):
            self.write()
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_manifest_temporary_collision_is_exclusive_and_preserved(self):
        token = "b" * 32
        collision = self.snapshot / f".MANIFEST.{token}.tmp"
        victim = self.root / "manifest-temporary-victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        collision.symlink_to(victim)
        with mock.patch.object(manifest_module.secrets, "token_hex", return_value=token):
            with self.assertRaises(FileExistsError):
                self.write()
        self.assertTrue(collision.is_symlink())
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_manifest_writer_rejects_parent_replacement_before_creation(self):
        moved = self.root / "moved-snapshot"
        real_open = manifest_module.os.open
        replaced = False

        def replace_parent(path, flags, *args, **kwargs):
            nonlocal replaced
            if not replaced and Path(path) == self.snapshot:
                self.snapshot.rename(moved)
                self.snapshot.mkdir(mode=0o700)
                (self.snapshot / "marker").write_bytes(b"unchanged\n")
                replaced = True
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(
            manifest_module.os, "open", side_effect=replace_parent
        ):
            with self.assertRaisesRegex(RuntimeError, "parent was replaced"):
                manifest_module._atomic_write_manifest(self.snapshot, b"{}\n")
        self.assertEqual((self.snapshot / "marker").read_bytes(), b"unchanged\n")
        self.assertFalse((self.snapshot / "MANIFEST.json").exists())
        self.assertFalse((moved / "MANIFEST.json").exists())

    def test_database_change_after_manifest_is_rejected(self):
        self.write()
        with sqlite3.connect(self.database) as connection:
            connection.execute("INSERT INTO parent VALUES (2)")
        with self.assertRaisesRegex(RuntimeError, "changed|mismatch"):
            self.verify()

    def test_same_size_content_change_is_rejected_without_exposing_paths(self):
        created = self.write()
        (self.snapshot / "data" / "originals" / "fixture.bin").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "content tree"):
            self.verify()
        self.assertIn("tree_sha256", created["content"])
        self.assertNotIn("originals", json.dumps(created["content"]))

    def test_directory_metadata_change_is_rejected(self):
        self.write()
        (self.snapshot / "data" / "originals").chmod(0o700)
        with self.assertRaisesRegex(RuntimeError, "content tree"):
            self.verify()

    def test_extended_attribute_change_is_rejected(self):
        self.write()
        target = self.snapshot / "data" / "originals" / "fixture.bin"
        try:
            os.setxattr(target, "user.david-pi-test", b"changed")
        except OSError as error:
            if error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
                self.skipTest("test filesystem does not support user xattrs")
            raise
        with self.assertRaisesRegex(RuntimeError, "content tree"):
            self.verify()

    def test_safe_symlink_target_is_authenticated_without_being_disclosed(self):
        links = self.snapshot / "data" / "links"
        links.mkdir()
        link = links / "favorite"
        link.symlink_to("../originals/fixture.bin")
        created = self.write()
        self.assertEqual(created["content"]["symlink_count"], 1)
        self.assertNotIn("favorite", json.dumps(created["content"]))
        link.unlink()
        link.symlink_to("../originals/changed.bin")
        with self.assertRaisesRegex(RuntimeError, "content tree"):
            self.verify()

    def test_escaping_symlink_is_rejected_before_manifest_creation(self):
        (self.snapshot / "data" / "escape").symlink_to("../../outside")
        with self.assertRaisesRegex(RuntimeError, "escaping symlink"):
            self.write()

    def test_database_symlink_is_rejected_before_manifest_creation(self):
        (self.snapshot / "databases" / "external.db").symlink_to("../../outside.db")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.write()

    def test_database_tree_rejects_non_database_and_detects_exact_drift(self):
        extra = self.snapshot / "databases" / "unexpected.txt"
        extra.write_text("not a database", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "non-database"):
            self.write()
        extra.unlink()
        self.write()
        (self.snapshot / "databases" / "extra").mkdir()
        with self.assertRaisesRegex(RuntimeError, "database tree"):
            self.verify()

    def test_database_tree_detects_post_signature_file_and_symlink_injection(self):
        self.write()
        injected = self.snapshot / "databases" / "injected.db"
        injected.write_bytes(self.database.read_bytes())
        with self.assertRaisesRegex(RuntimeError, "database tree|inventory"):
            self.verify()
        injected.unlink()
        (self.snapshot / "databases" / "redirect").symlink_to(self.database)
        with self.assertRaisesRegex(RuntimeError, "database tree|symlink"):
            self.verify()

    def test_signing_key_permissions_and_size_are_enforced(self):
        self.key_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            manifest_module.load_signing_key(self.key_file)
        self.key_file.write_bytes(b"short")
        self.key_file.chmod(0o600)
        with self.assertRaises(ValueError):
            manifest_module.load_signing_key(self.key_file)

    def test_signing_key_symlink_is_rejected(self):
        linked = self.root / "linked.key"
        linked.symlink_to(self.key_file)
        with self.assertRaisesRegex(PermissionError, "non-symlink"):
            manifest_module.load_signing_key(linked)

    def test_manifest_creation_requires_uuid_and_st_dev_independence(self):
        self.metadata["backup_uuid"] = self.metadata["source_uuid"]
        with self.assertRaisesRegex(ValueError, "UUIDs must differ"):
            self.write()
        self.metadata["backup_uuid"] = "backup-uuid"
        self.metadata["backup_st_dev"] = self.metadata["source_st_dev"]
        with self.assertRaisesRegex(ValueError, "st_dev values must differ"):
            self.write()

    def test_manifest_rejects_impossible_or_noncanonical_utc_timestamps(self):
        for field, value in (
            ("started_at", "2026-19-39T29:59:59Z"),
            ("completed_at", "2026-09-03T00:01:00+00:00"),
        ):
            with self.subTest(field=field):
                self.metadata[field] = value
                with self.assertRaisesRegex(ValueError, "timestamp|UTC"):
                    self.write()
                self.metadata = {
                    **self.metadata,
                    "started_at": "2026-09-03T00:00:00Z",
                    "completed_at": "2026-09-03T00:01:00Z",
                }


class DataBackupContractTest(unittest.TestCase):
    def test_all_writers_are_quiesced_recovered_and_pruning_is_impossible(self):
        script = (ROOT / "deploy" / "david-pi-data-backup").read_text(encoding="utf-8")
        for container in (
            "family-photo-portal",
            "david-pi-maintenance",
            "david-pi-chat-notifier",
            "david-pi-audiobook-preparer",
            "david-pi-device-backup-worker",
            "david-pi-slideshow-worker",
        ):
            self.assertIn(container, script)
        self.assertIn('DAVID_PI_BACKUP_PRUNE_ENABLED:-0', script)
        self.assertIn('[ "$PRUNE_ENABLED" = 0 ] || fail', script)
        self.assertIn('writers_quiesced', script)
        self.assertIn("david_pi_snapshot_manifest.py", script)
        self.assertIn('flock -n 9 || fail "another data backup is already running"', script)
        self.assertIn('if ! restart_writers; then', script)
        self.assertIn('application writers did not pass bounded readiness', script)
        self.assertIn('write_attempt_status failed "backup_or_writer_recovery_failed"', script)
        self.assertIn("david_pi_recovery_paths.py", script)
        self.assertIn("david-pi-family-storage-v1", script)
        self.assertIn('SOURCE_ST_DEV=$(stat -Lc %d -- "$SOURCE_ROOT_REF")', script)
        self.assertIn('BACKUP_ST_DEV=$(stat -Lc %d -- "$BACKUP_ROOT_REF")', script)
        self.assertIn('[ "$SOURCE_UUID" != "$BACKUP_UUID" ]', script)
        self.assertIn('[ "$SOURCE_ST_DEV" != "$BACKUP_ST_DEV" ]', script)
        self.assertGreaterEqual(script.count("validate_work"), 10)
        self.assertGreaterEqual(script.count("require_pinned_storage"), 15)
        self.assertNotIn("--link-dest", script)
        self.assertNotIn("rsync -aH", script)
        runbook = (ROOT / "deploy" / "BACKUP_RECOVERY_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("device-backup worker, slideshow worker", runbook)
        for comparison in (
            'findmnt -no UUID -T "$SOURCE_ROOT_REF"',
            'findmnt -no UUID -T "$BACKUP_ROOT_REF"',
            'findmnt -no SOURCE -T "$SOURCE_ROOT_REF"',
            'findmnt -no SOURCE -T "$BACKUP_ROOT_REF"',
            'stat -Lc %d -- "$SOURCE_ROOT_REF"',
            'stat -Lc %d -- "$BACKUP_ROOT_REF"',
            'pinned-validate-sentinels',
        ):
            self.assertIn(comparison, script)
        self.assertIn("pinned-write-destination", script)
        self.assertIn("pinned-write-work", script)
        self.assertIn("describe-root-pin", script)
        self.assertIn('exec {SOURCE_ROOT_FD}<"$SOURCE"', script)
        self.assertIn('exec {BACKUP_ROOT_FD}<"$DESTINATION"', script)
        self.assertIn('exec {WORK_ROOT_FD}<"$WORK"', script)
        self.assertIn("pinned-sync-tree", script)
        self.assertIn("pinned-copy-databases", script)
        self.assertIn("pinned-finalize", script)
        self.assertIn("pinned-validate-final", script)
        self.assertIn("pinned-publish-latest", script)
        self.assertIn('--snapshot-fd "$WORK_ROOT_FD"', script)
        self.assertLess(
            script.index('exec {SOURCE_ROOT_FD}<"$SOURCE"'),
            script.rindex('python3 "$PATH_SAFETY_TOOL" pinned-validate-sentinels'),
        )
        self.assertLess(
            script.index('exec {BACKUP_ROOT_FD}<"$DESTINATION"'),
            script.index('SOURCE_UUID=$(findmnt'),
        )
        self.assertNotIn("rsync ", script)
        self.assertNotIn("--delete", script)
        self.assertNotIn("last-attempt.json.tmp", script)
        self.assertNotIn("last-success.json.tmp", script)
        self.assertNotIn('>"$WORK/MANIFEST.txt"', script)
        self.assertIn('python3 "$MANIFEST_TOOL" verify', script)
        self.assertLess(
            script.index('if ! restart_writers; then'),
            script.index('python3 "$MANIFEST_TOOL" create'),
        )
        self.assertNotIn("rm -rf", script)

    def test_b2_uploader_is_append_only_and_uses_signed_snapshot(self):
        script = (ROOT / "deploy" / "david-pi-b2-backup").read_text(encoding="utf-8")
        self.assertIn("david_pi_snapshot_manifest.py", script)
        self.assertIn("restic backup --quiet --no-lock", script)
        self.assertNotIn("restic forget", script)
        self.assertNotIn("restic prune", script)
        self.assertIn('"pruning_enabled": False', script)
        self.assertIn('"object_lock_verified": False', script)
        self.assertIn('"offsite_restore_verified": False', script)
        self.assertIn("write_status failed", script)
        self.assertIn("uploaded_pending_restore", script)
        self.assertIn("LAST_SUCCESS", script)
        self.assertIn("manifest_sha256", script)
        self.assertIn("MAX_SNAPSHOT_AGE_SECONDS", script)
        self.assertIn("david_pi_recovery_paths.py", script)
        self.assertIn("/etc/david-pi/b2", script)
        self.assertNotIn("/srv/compose/photo-portal/b2", script)
        self.assertNotIn("/run/david-pi/", script)

    def test_b2_runs_only_after_a_successful_local_snapshot(self):
        self.assertFalse((ROOT / "deploy" / "david-pi-b2-backup.timer").exists())
        drop_in = (ROOT / "deploy" / "david-pi-data-backup-b2.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("OnSuccess=david-pi-b2-backup.service", drop_in)

    def test_b2_systemd_contract_creates_private_runtime_and_credentials(self):
        unit = (ROOT / "deploy" / "david-pi-b2-backup.service").read_text(encoding="utf-8")
        for credential in (
            "manifest-key",
            "b2-account-id",
            "b2-account-key",
            "restic-repository",
            "restic-password",
        ):
            self.assertIn(f"LoadCredential={credential}:", unit)
        self.assertIn("RuntimeDirectory=david-pi-b2-backup", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertIn("RuntimeDirectoryPreserve=no", unit)
        self.assertIn("StateDirectory=david-pi-b2-backup", unit)
        self.assertIn("StateDirectoryMode=0700", unit)
        self.assertIn("CacheDirectory=david-pi-restic", unit)
        self.assertIn("CacheDirectoryMode=0700", unit)
        self.assertIn("ConditionPathIsMountPoint=/srv/backup-data", unit)
        self.assertNotIn("/run/david-pi/", unit)
        self.assertIn("/var/lib/david-pi-b2-backup/status.json", unit)

    def test_local_backup_unit_cannot_write_to_live_data(self):
        unit = (ROOT / "deploy" / "david-pi-data-backup.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("ReadWritePaths=/srv/backup-data", unit)
        self.assertIn("ReadOnlyPaths=/srv/data/family-photos", unit)
        read_write_line = next(
            line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
        )
        self.assertNotIn("/srv/data/family-photos", read_write_line)
        self.assertIn("RestrictAddressFamilies=AF_UNIX", unit)
        self.assertIn("RuntimeDirectory=david-pi-data-backup", unit)
        self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)
        self.assertIn("/run/david-pi-data-backup", unit)

    def test_backup_runtime_directories_are_unique_and_have_explicit_lifecycle(self):
        data_unit = (ROOT / "deploy" / "david-pi-data-backup.service").read_text()
        b2_unit = (ROOT / "deploy" / "david-pi-b2-backup.service").read_text()
        self.assertNotEqual(
            "david-pi-data-backup", "david-pi-b2-backup"
        )
        for unit in (data_unit, b2_unit):
            self.assertIn("RuntimeDirectoryMode=0700", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", data_unit)
        self.assertIn("RuntimeDirectoryPreserve=no", b2_unit)
        self.assertNotIn("RuntimeDirectory=david-pi\n", data_unit + b2_unit)

    def test_retention_command_is_preview_only(self):
        script = (ROOT / "deploy" / "david-pi-b2-retention-preview").read_text(encoding="utf-8")
        self.assertIn("--dry-run", script)
        self.assertNotIn("restic prune", script)


class B2BackupIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshots = self.root / "snapshots"
        self.snapshot_id = "20260903T000000Z"
        self.snapshot = self.snapshots / self.snapshot_id
        (self.snapshot / "databases").mkdir(parents=True)
        (self.snapshot / "data").mkdir()
        (self.snapshot / "config").mkdir()
        self.snapshots.chmod(0o700)
        self.snapshot.chmod(0o700)
        with sqlite3.connect(self.snapshot / "databases" / "app.db") as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
        (self.snapshot / "data" / "object.bin").write_bytes(b"object")
        (self.snapshot / "config" / "compose.yaml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        self.key = self.root / "manifest.key"
        self.key.write_bytes(b"m" * 64)
        self.key.chmod(0o600)
        metadata = {
            "snapshot_id": self.snapshot_id,
            "started_at": "2026-09-03T00:00:00Z",
            "completed_at": "2026-09-03T00:01:00Z",
            "source_device": "/dev/source",
            "source_uuid": "source",
            "source_st_dev": 101,
            "backup_device": "/dev/backup",
            "backup_uuid": "backup",
            "backup_st_dev": 202,
            "portal_image": "fixture:image",
        }
        manifest = manifest_module.write_manifest(
            self.snapshot,
            self.snapshot / "MANIFEST.json",
            metadata,
            manifest_module.load_signing_key(self.key),
        )
        (self.snapshots / "latest").symlink_to(self.snapshot_id)
        self.local_status = self.root / "last-success.json"
        self.local_payload = {
            "schema_version": 3,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": self.snapshot_id,
            "database_count": 1,
            "manifest_sha256": manifest["integrity"]["payload_sha256"],
            "writers_quiesced": True,
            "writer_quiescence_seconds": 10,
            "pruning_enabled": False,
            "state": "healthy",
        }
        self._write_local_status()

        self.credentials = {}
        for name, value in {
            "account-id": "fixture-id",
            "account-key": "fixture-key",
            "repository": "b2:fixture-bucket:repository",
            "password": "fixture-password",
        }.items():
            path = self.root / name
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)
            self.credentials[name] = path

        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        restic = self.fake_bin / "restic"
        restic.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_RESTIC_LOG"
if [ "$1" = backup ] && [ "${FAKE_RESTIC_FAIL_BACKUP:-0}" = 1 ]; then
    exit 23
fi
if [ "$1" = snapshots ]; then
    printf '%s\\n' '[{"short_id":"fixture123"}]'
fi
""",
            encoding="utf-8",
        )
        restic.chmod(0o755)
        self.restic_log = self.root / "restic.log"
        self.status = self.root / "status" / "b2.json"
        self.lock = self.root / "b2.lock"
        self.status.parent.mkdir(mode=0o700)
        (self.root / "cache").mkdir(mode=0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_local_status(self):
        self.local_status.write_text(
            json.dumps(self.local_payload) + "\n", encoding="utf-8"
        )
        self.local_status.chmod(0o600)

    def run_backup(self, **extra_environment):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}:{environment['PATH']}",
                "FAKE_RESTIC_LOG": str(self.restic_log),
                "DAVID_PI_SNAPSHOTS": str(self.snapshots),
                "DAVID_PI_SNAPSHOT": str(self.snapshots / "latest"),
                "DAVID_PI_LOCAL_BACKUP_STATUS": str(self.local_status),
                "DAVID_PI_BACKUP_MANIFEST_TOOL": str(SCRIPT),
                "DAVID_PI_RECOVERY_PATH_TOOL": str(
                    ROOT / "deploy" / "david_pi_recovery_paths.py"
                ),
                "DAVID_PI_BACKUP_MANIFEST_SIGNING_KEY_FILE": str(self.key),
                "DAVID_PI_B2_ACCOUNT_ID_FILE": str(self.credentials["account-id"]),
                "DAVID_PI_B2_ACCOUNT_KEY_FILE": str(self.credentials["account-key"]),
                "DAVID_PI_RESTIC_REPOSITORY_FILE": str(self.credentials["repository"]),
                "DAVID_PI_RESTIC_PASSWORD_FILE": str(self.credentials["password"]),
                "DAVID_PI_RESTIC_CACHE_DIR": str(self.root / "cache"),
                "DAVID_PI_B2_LOCK_FILE": str(self.lock),
                "DAVID_PI_B2_STATUS_FILE": str(self.status),
            }
        )
        environment.update(extra_environment)
        return subprocess.run(
            [str(ROOT / "deploy" / "david-pi-b2-backup")],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_success_is_truthfully_pending_restore(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "uploaded_pending_restore")
        self.assertEqual(status["source_snapshot"], self.snapshot_id)
        self.assertEqual(status["restic_snapshot"], "fixture123")
        self.assertTrue(status["source_snapshot_confirmed"])
        self.assertTrue(status["repository_sample_checked"])
        self.assertFalse(status["object_lock_verified"])
        self.assertFalse(status["offsite_restore_verified"])

    def test_stale_local_snapshot_is_rejected_before_restic(self):
        self.local_payload["completed_at"] = "2000-01-01T00:00:00+00:00"
        self._write_local_status()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.restic_log.exists())
        self.assertEqual(
            json.loads(self.status.read_text(encoding="utf-8"))["state"], "failed"
        )

    def test_manifest_status_mismatch_is_rejected_before_restic(self):
        self.local_payload["manifest_sha256"] = "0" * 64
        self._write_local_status()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.restic_log.exists())
        self.assertEqual(
            json.loads(self.status.read_text(encoding="utf-8"))["state"], "failed"
        )

    def test_latest_must_be_relative_direct_timestamp_child(self):
        latest = self.snapshots / "latest"
        latest.unlink()
        latest.symlink_to(str(self.snapshot))
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest snapshot is unsafe", result.stderr)
        self.assertFalse(self.restic_log.exists())

    def test_signed_manifest_snapshot_id_must_match_latest_child(self):
        metadata = {
            "snapshot_id": "20260903T000001Z",
            "started_at": "2026-09-03T00:00:00Z",
            "completed_at": "2026-09-03T00:01:00Z",
            "source_device": "/dev/source",
            "source_uuid": "source",
            "source_st_dev": 101,
            "backup_device": "/dev/backup",
            "backup_uuid": "backup",
            "backup_st_dev": 202,
            "portal_image": "fixture:image",
        }
        manifest = manifest_module.write_manifest(
            self.snapshot,
            self.snapshot / "MANIFEST.json",
            metadata,
            manifest_module.load_signing_key(self.key),
        )
        self.local_payload["manifest_sha256"] = manifest["integrity"]["payload_sha256"]
        self._write_local_status()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.restic_log.exists())

    def test_excessive_writer_quiescence_is_rejected_before_restic(self):
        self.local_payload["writer_quiescence_seconds"] = 1801
        self._write_local_status()
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.restic_log.exists())
        self.assertEqual(
            json.loads(self.status.read_text(encoding="utf-8"))["state"], "failed"
        )

    def test_remote_failure_overwrites_status_with_failed_attempt(self):
        result = self.run_backup(FAKE_RESTIC_FAIL_BACKUP="1")
        self.assertNotEqual(result.returncode, 0)
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["source_snapshot"], self.snapshot_id)
        self.assertFalse(status["source_snapshot_confirmed"])

    def test_status_symlink_redirection_fails_without_touching_victim(self):
        victim = self.root / "status-victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        self.status.symlink_to(victim)
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("status path is unsafe", result.stderr)
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse(self.restic_log.exists())

    def test_second_uploader_is_rejected_by_lock(self):
        with self.lock.open("w", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already running", result.stderr)
        self.assertEqual(
            json.loads(self.status.read_text(encoding="utf-8"))["state"], "failed"
        )


class DataBackupFailurePathTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.script = ROOT / "deploy" / "david-pi-data-backup"

    def tearDown(self):
        self.temporary.cleanup()

    def test_restart_function_fails_if_any_writer_does_not_recover(self):
        command = f"""
source {shlex.quote(str(self.script))}
storage_identity_matches() {{ return 0; }}
docker() {{
    if [ "$1" = start ] && [ "$2" = broken ]; then return 1; fi
    if [ "$1" = inspect ]; then printf '%s\\n' true; return 0; fi
    return 0
}}
RUNNING_WRITERS=(healthy broken)
restart_writers
"""
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not restart writer broken", result.stderr)

    def test_restart_function_uses_shared_bounded_readiness_gate(self):
        readiness = self.root / "writer-readiness"
        readiness_log = self.root / "writer-readiness.log"
        readiness.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$READINESS_LOG\"\n",
            encoding="utf-8",
        )
        readiness.chmod(0o755)
        command = f"""
source {shlex.quote(str(self.script))}
storage_identity_matches() {{ return 0; }}
docker() {{
    [ "$1" = start ] && return 0
    return 1
}}
WRITER_READINESS={shlex.quote(str(readiness))}
READINESS_LOG={shlex.quote(str(readiness_log))}
export READINESS_LOG
RUNNING_WRITERS=(david-pi-maintenance david-pi-chat-notifier david-pi-audiobook-preparer family-photo-portal)
restart_writers
"""
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            readiness_log.read_text(encoding="utf-8").strip(),
            "--timeout 90 --interval 2 --stable-samples 3",
        )

    def test_exit_recovery_publishes_failure_when_restart_fails(self):
        destination = self.root / "backup"
        destination.mkdir()
        destination.chmod(0o700)
        command = f"""
source {shlex.quote(str(self.script))}
PATH_SAFETY_TOOL={shlex.quote(str(ROOT / 'deploy' / 'david_pi_recovery_paths.py'))}
storage_identity_matches() {{ return 0; }}
docker() {{
    if [ "$1" = start ]; then return 1; fi
    return 0
}}
DESTINATION={shlex.quote(str(destination))}
STAMP=20260903T000000Z
exec {{BACKUP_ROOT_FD}}<"$DESTINATION"
BACKUP_ROOT_IDENTITY=$(python3 "$PATH_SAFETY_TOOL" describe-root-pin \
    --path "$DESTINATION" --fd "$BACKUP_ROOT_FD")
BACKUP_DESTINATION_VERIFIED=1
RUNNING_WRITERS=(broken)
trap cleanup EXIT
exit 0
"""
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        status = json.loads(
            (destination / "last-attempt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["detail"], "backup_or_writer_recovery_failed")

    def test_private_runtime_lock_rejects_a_real_concurrent_process_before_mutation(self):
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        (source / ".david-pi-storage").write_text(
            "david-pi-family-storage-v1\n", encoding="utf-8"
        )
        (source / ".david-pi-storage").chmod(0o600)
        (destination / ".david-pi-backup-storage").write_text(
            "david-pi-independent-backup-v1\n", encoding="utf-8"
        )
        (destination / ".david-pi-backup-storage").chmod(0o600)
        key = self.root / "manifest.key"
        key.write_bytes(b"k" * 64)
        key.chmod(0o600)
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        helpers = {
            "id": "#!/bin/sh\n[ \"$1\" = -u ] && { printf '%s\\n' 0; exit 0; }\nexec /usr/bin/id \"$@\"\n",
            "mountpoint": "#!/bin/sh\nexit 0\n",
            "findmnt": """#!/usr/bin/env python3
import os, sys
field = sys.argv[sys.argv.index('-no') + 1]
target = sys.argv[-1]
is_destination = os.path.realpath(target) == os.environ['DAVID_PI_BACKUP_DESTINATION']
if field == 'UUID':
    print(os.environ['DAVID_PI_BACKUP_UUID'] if is_destination else 'source-uuid')
elif field == 'SOURCE':
    print('/dev/backup' if is_destination else '/dev/source')
else:
    raise SystemExit(2)
""",
            "stat": """#!/usr/bin/env python3
import os, subprocess, sys
option_index = next(
    index for index, value in enumerate(sys.argv) if value.startswith('-') and 'c' in value[1:]
)
fmt = sys.argv[option_index + 1]
target = sys.argv[-1]
if fmt == '%d':
    print('202' if os.path.realpath(target) == os.environ['DAVID_PI_BACKUP_DESTINATION'] else '101')
elif fmt == '%u':
    print('0')
else:
    raise SystemExit(subprocess.run(['/usr/bin/stat', *sys.argv[1:]]).returncode)
""",
        }
        for name, body in helpers.items():
            helper = fake_bin / name
            helper.write_text(body, encoding="utf-8")
            helper.chmod(0o755)
        runtime = self.root / "run" / "david-pi-data-backup"
        runtime.mkdir(parents=True, mode=0o700)
        lock = runtime / "backup.lock"
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "DAVID_PI_DATA_SOURCE": str(source),
                "DAVID_PI_BACKUP_DESTINATION": str(destination),
                "DAVID_PI_BACKUP_UUID": "backup-uuid",
                "DAVID_PI_BACKUP_MANIFEST_TOOL": str(SCRIPT),
                "DAVID_PI_RECOVERY_PATH_TOOL": str(
                    ROOT / "deploy" / "david_pi_recovery_paths.py"
                ),
                "DAVID_PI_BACKUP_MANIFEST_SIGNING_KEY_FILE": str(key),
                "DAVID_PI_DATA_BACKUP_LOCK_FILE": str(lock),
                "DAVID_PI_ID_BIN": str(fake_bin / "id"),
            }
        )
        holder = subprocess.Popen(
            [
                "bash", "-c",
                'exec 9>"$1"; flock -n 9 || exit 2; echo locked; read -r',
                "holder", str(lock),
            ],
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            holder.stdout.close()
            result = subprocess.run(
                [str(self.script)],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            holder.stdin.close()
            holder.wait(timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another data backup is already running", result.stderr)
        self.assertFalse((destination / "snapshots").exists())

    def test_pinned_identity_guard_detects_uuid_device_and_sentinel_drift(self):
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
        (source / ".david-pi-storage").write_text(
            "david-pi-family-storage-v1\n", encoding="ascii"
        )
        (destination / ".david-pi-backup-storage").write_text(
            "david-pi-independent-backup-v1\n", encoding="ascii"
        )
        (source / ".david-pi-storage").chmod(0o600)
        (destination / ".david-pi-backup-storage").chmod(0o600)
        command = f"""
source {shlex.quote(str(self.script))}
SOURCE={shlex.quote(str(source))}
DESTINATION={shlex.quote(str(destination))}
PATH_SAFETY_TOOL={shlex.quote(str(ROOT / 'deploy' / 'david_pi_recovery_paths.py'))}
SOURCE_UUID=source-uuid
BACKUP_UUID=backup-uuid
SOURCE_DEVICE=/dev/source
BACKUP_DEVICE=/dev/backup
SOURCE_ST_DEV=101
BACKUP_ST_DEV=202
CURRENT_SOURCE_UUID=source-uuid
CURRENT_BACKUP_UUID=backup-uuid
CURRENT_SOURCE_DEVICE=/dev/source
CURRENT_BACKUP_DEVICE=/dev/backup
CURRENT_SOURCE_ST_DEV=101
CURRENT_BACKUP_ST_DEV=202
STORAGE_IDENTITIES_PINNED=1
exec {{SOURCE_ROOT_FD}}<"$SOURCE"
exec {{BACKUP_ROOT_FD}}<"$DESTINATION"
SOURCE_ROOT_IDENTITY=$(python3 "$PATH_SAFETY_TOOL" describe-root-pin \
    --path "$SOURCE" --fd "$SOURCE_ROOT_FD")
BACKUP_ROOT_IDENTITY=$(python3 "$PATH_SAFETY_TOOL" describe-root-pin \
    --path "$DESTINATION" --fd "$BACKUP_ROOT_FD")
SOURCE_ROOT_REF="/proc/self/fd/$SOURCE_ROOT_FD"
BACKUP_ROOT_REF="/proc/self/fd/$BACKUP_ROOT_FD"
findmnt() {{
    local field=$2
    local target=${{@: -1}}
    if [ "$field" = UUID ]; then
        if [ "$target" = "$SOURCE_ROOT_REF" ]; then printf '%s\n' "$CURRENT_SOURCE_UUID";
        else printf '%s\n' "$CURRENT_BACKUP_UUID"; fi
    elif [ "$target" = "$SOURCE_ROOT_REF" ]; then printf '%s\n' "$CURRENT_SOURCE_DEVICE";
    else printf '%s\n' "$CURRENT_BACKUP_DEVICE"; fi
}}
stat() {{
    local target=${{@: -1}}
    if [ "$target" = "$SOURCE_ROOT_REF" ]; then printf '%s\n' "$CURRENT_SOURCE_ST_DEV";
    else printf '%s\n' "$CURRENT_BACKUP_ST_DEV"; fi
}}
storage_identity_matches || exit 21
CURRENT_BACKUP_UUID=changed
if storage_identity_matches; then exit 22; fi
CURRENT_BACKUP_UUID=backup-uuid
CURRENT_BACKUP_DEVICE=/dev/changed
if storage_identity_matches; then exit 25; fi
CURRENT_BACKUP_DEVICE=/dev/backup
CURRENT_SOURCE_ST_DEV=999
if storage_identity_matches; then exit 23; fi
CURRENT_SOURCE_ST_DEV=101
printf '%s\n' tampered > "$SOURCE/.david-pi-storage"
if storage_identity_matches; then exit 24; fi
"""
        result = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
