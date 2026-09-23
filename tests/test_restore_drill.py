import importlib.util
import errno
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
sys.path.insert(0, str(DEPLOY))

MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "david_pi_snapshot_manifest", DEPLOY / "david_pi_snapshot_manifest.py"
)
manifest_module = importlib.util.module_from_spec(MANIFEST_SPEC)
MANIFEST_SPEC.loader.exec_module(manifest_module)
sys.modules["david_pi_snapshot_manifest"] = manifest_module

RESTORE_SPEC = importlib.util.spec_from_file_location(
    "david_pi_restore_drill", DEPLOY / "david_pi_restore_drill.py"
)
restore_module = importlib.util.module_from_spec(RESTORE_SPEC)
RESTORE_SPEC.loader.exec_module(restore_module)
import david_pi_fd_tree as fd_tree_module


class RestoreDrillTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        (self.snapshot / "databases" / "platform").mkdir(parents=True)
        (self.snapshot / "config").mkdir()
        (self.snapshot / "data" / "originals").mkdir(parents=True)
        self.snapshot.chmod(0o700)
        with sqlite3.connect(
            self.snapshot / "databases" / "platform" / "app.db"
        ) as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO fixture VALUES (1)")
        (self.snapshot / "config" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
        for index in range(5):
            (self.snapshot / "data" / "originals" / f"object-{index}.bin").write_bytes(
                bytes([index]) * (index + 1)
            )
        (self.snapshot / "data" / ".david-pi-storage").write_bytes(
            restore_module.EXPECTED_APPLICATION_SENTINEL
        )
        self.key_file = self.root / "manifest.key"
        self.key_file.write_bytes(b"s" * 64)
        self.key_file.chmod(0o600)
        self.key = manifest_module.load_signing_key(self.key_file)
        metadata = {
            "snapshot_id": "fixture",
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
        self.manifest = self.snapshot / "MANIFEST.json"
        self.manifest_payload = manifest_module.write_manifest(
            self.snapshot, self.manifest, metadata, self.key
        )

    def tearDown(self):
        self.temporary.cleanup()

    def target(self, valid=True):
        destination = self.root / "restore"
        destination.mkdir()
        destination.chmod(0o700)
        (destination / restore_module.RESTORE_SENTINEL).write_text(
            (restore_module.EXPECTED_SENTINEL + "\n") if valid else "wrong\n",
            encoding="ascii",
        )
        (destination / restore_module.RESTORE_SENTINEL).chmod(0o600)
        return destination

    def restore(self, destination, *, mode="core", sample_count=100, **overrides):
        options = {
            "expected_target_uid": os.getuid(),
            "application_uid": os.getuid(),
            "application_gid": os.getgid(),
            "require_mount": False,
            "require_distinct_filesystem": False,
        }
        options.update(overrides)
        return restore_module.restore(
            self.snapshot,
            destination,
            self.manifest,
            self.key,
            mode=mode,
            sample_count=sample_count,
            **options,
        )

    def test_core_restore_copies_and_verifies_databases_and_config(self):
        destination = self.target()
        report = self.restore(destination)
        self.assertEqual(report["state"], "data_verified")
        self.assertEqual(report["database_count"], 1)
        self.assertEqual(
            report["database_tree_sha256"], self.manifest_payload["database_tree"]["tree_sha256"]
        )
        self.assertFalse(report["network_isolation_verified"])
        self.assertFalse(report["application_boot_verified"])
        self.assertFalse(report["drill_complete"])
        evidence = destination / "databases" / "platform" / "app.db"
        application = destination / "data" / "platform" / "app.db"
        self.assertTrue(evidence.is_file())
        self.assertTrue(application.is_file())
        self.assertNotEqual(evidence.stat().st_ino, application.stat().st_ino)
        self.assertEqual(application.stat().st_uid, os.getuid())
        self.assertEqual(application.stat().st_gid, os.getgid())
        self.assertTrue(report["signed_database_evidence_preserved"])
        self.assertEqual(report["application_data_root"], "data")
        self.assertTrue(report["storage_sentinel_ready"])
        self.assertTrue(report["application_layout_ready"])
        self.assertEqual(application.stat().st_mode & 0o777, 0o640)
        self.assertTrue((destination / "config" / "compose.yaml").is_file())
        for relative in restore_module.REQUIRED_EMPTY_DIRECTORIES:
            directory = destination / "data" / relative
            self.assertTrue(directory.is_dir())
            self.assertEqual(list(directory.iterdir()), [])

    def test_sample_restore_is_deterministic_and_hash_verified(self):
        destination = self.target()
        report = self.restore(destination, mode="sample", sample_count=3)
        self.assertEqual(report["sampled_file_count"], 3)
        application_database = destination / "data" / "platform" / "app.db"
        restored = [
            path
            for path in (destination / "data").rglob("*")
            if path.is_file() and path != application_database
        ]
        self.assertEqual(len(restored), 3)
        self.assertTrue(application_database.is_file())

    def test_sample_content_tamper_after_manifest_verification_fails_signed_evidence(self):
        destination = self.target()

        def tamper(_snapshot, verified):
            selected = restore_module.signed_sample_paths(
                verified["_recovery_evidence"]["trees"]["data"], 100
            )
            relative = next(path for path in selected if path.startswith("originals/"))
            target = self.snapshot / "data" / relative
            target.write_bytes(b"x" * target.stat().st_size)

        with mock.patch.object(
            restore_module, "_after_manifest_verified", side_effect=tamper
        ):
            with self.assertRaisesRegex(RuntimeError, "signed evidence"):
                self.restore(destination, mode="sample", sample_count=100)

    def test_sample_mode_tamper_after_manifest_verification_fails_signed_evidence(self):
        destination = self.target()

        def tamper(_snapshot, verified):
            selected = restore_module.signed_sample_paths(
                verified["_recovery_evidence"]["trees"]["data"], 100
            )
            relative = next(path for path in selected if path.startswith("originals/"))
            target = self.snapshot / "data" / relative
            target.chmod(0o600 if target.stat().st_mode & 0o777 != 0o600 else 0o640)

        with mock.patch.object(
            restore_module, "_after_manifest_verified", side_effect=tamper
        ):
            with self.assertRaisesRegex(RuntimeError, "signed evidence"):
                self.restore(destination, mode="sample", sample_count=100)

    def test_sample_xattr_tamper_after_manifest_verification_fails_signed_evidence(self):
        target = self.snapshot / "data" / "originals" / "object-0.bin"
        try:
            os.setxattr(target, "user.david-pi-sample", b"signed")
        except OSError as error:
            if error.errno in {
                errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }:
                self.skipTest("test filesystem does not support user xattrs")
            raise
        manifest_module.write_manifest(
            self.snapshot,
            self.manifest,
            {
                "snapshot_id": "fixture",
                "started_at": "2026-09-03T00:00:00Z",
                "completed_at": "2026-09-03T00:01:00Z",
                "source_device": "/dev/source",
                "source_uuid": "source",
                "source_st_dev": 101,
                "backup_device": "/dev/backup",
                "backup_uuid": "backup",
                "backup_st_dev": 202,
                "portal_image": "fixture:image",
            },
            self.key,
        )
        destination = self.target()

        def tamper(_snapshot, _verified):
            os.setxattr(target, "user.david-pi-sample", b"changed")

        with mock.patch.object(
            restore_module, "_after_manifest_verified", side_effect=tamper
        ):
            with self.assertRaisesRegex(RuntimeError, "signed evidence"):
                self.restore(destination, mode="sample", sample_count=100)

    def test_sample_selection_comes_from_signed_evidence_not_mutable_source(self):
        destination = self.target()

        def inject(_snapshot, _verified):
            (self.snapshot / "data" / "000-injected.bin").write_bytes(b"injected")

        with mock.patch.object(
            restore_module, "_after_manifest_verified", side_effect=inject
        ):
            report = self.restore(destination, mode="sample", sample_count=2)
        self.assertEqual(report["sampled_file_count"], 2)
        self.assertFalse((destination / "data" / "000-injected.bin").exists())

    def test_target_child_redirect_after_creation_receives_no_content_or_metadata(self):
        destination = self.target()
        victim = self.root / "target-child-victim"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_bytes(b"unchanged")
        original_mode = victim.stat().st_mode & 0o777

        def redirect(_parent_fd, name):
            if name == "databases":
                (destination / name).rmdir()
                (destination / name).symlink_to(victim, target_is_directory=True)

        with mock.patch.object(
            restore_module, "_after_target_child_created", side_effect=redirect
        ):
            with self.assertRaisesRegex(ValueError, "link redirect|symlink"):
                self.restore(destination)
        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertEqual(victim.stat().st_mode & 0o777, original_mode)

    def test_real_target_bind_inserted_after_creation_receives_nothing(self):
        script = textwrap.dedent(
            f"""
            import os
            import pathlib
            import sqlite3
            import subprocess
            import sys
            import tempfile

            sys.path.insert(0, {str(DEPLOY)!r})
            import david_pi_snapshot_manifest as manifest_module
            import david_pi_restore_drill as restore_module
            with tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                snapshot = root / "snapshot"
                (snapshot / "databases").mkdir(parents=True)
                (snapshot / "config").mkdir()
                (snapshot / "data").mkdir()
                snapshot.chmod(0o700)
                with sqlite3.connect(snapshot / "databases" / "app.db") as db:
                    db.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
                (snapshot / "config" / "compose.yaml").write_text("services: {{}}\\n")
                (snapshot / "data" / ".david-pi-storage").write_bytes(
                    restore_module.EXPECTED_APPLICATION_SENTINEL
                )
                key_path = root / "manifest.key"
                key_path.write_bytes(b"k" * 64)
                key_path.chmod(0o600)
                key = manifest_module.load_signing_key(key_path)
                manifest_path = snapshot / "MANIFEST.json"
                manifest_module.write_manifest(
                    snapshot,
                    manifest_path,
                    {{
                        "snapshot_id": "fixture",
                        "started_at": "2026-09-04T00:00:00Z",
                        "completed_at": "2026-09-04T00:01:00Z",
                        "source_device": "/dev/source",
                        "source_uuid": "source",
                        "source_st_dev": 101,
                        "backup_device": "/dev/backup",
                        "backup_uuid": "backup",
                        "backup_st_dev": 202,
                        "portal_image": "fixture:image",
                    }},
                    key,
                )
                destination = root / "restore"
                destination.mkdir(mode=0o700)
                sentinel = destination / restore_module.RESTORE_SENTINEL
                sentinel.write_text(restore_module.EXPECTED_SENTINEL + "\\n")
                sentinel.chmod(0o600)
                external = root / "external"
                external.mkdir()
                marker = external / "keep"
                marker.write_bytes(b"unchanged")
                original_mode = external.stat().st_mode & 0o777
                mounted = False
                def inject(parent_fd, name):
                    global mounted
                    if name == "databases" and not mounted:
                        subprocess.run(
                            ["mount", "--bind", str(external),
                             f"/proc/self/fd/{{parent_fd}}/{{name}}"],
                            check=True,
                            pass_fds=(parent_fd,),
                        )
                        mounted = True
                restore_module._after_target_child_created = inject
                try:
                    try:
                        restore_module.restore(
                            snapshot,
                            destination,
                            manifest_path,
                            key,
                            expected_target_uid=os.getuid(),
                            application_uid=os.getuid(),
                            application_gid=os.getgid(),
                            require_mount=False,
                            require_distinct_filesystem=False,
                        )
                    except ValueError as error:
                        assert "mount boundary" in str(error)
                    else:
                        raise AssertionError("late restore-target bind was accepted")
                    assert marker.read_bytes() == b"unchanged"
                    assert external.stat().st_mode & 0o777 == original_mode
                    assert sorted(path.name for path in external.iterdir()) == ["keep"]
                finally:
                    if mounted:
                        subprocess.run(
                            ["umount", str(destination / "databases")], check=True
                        )

                # Insert a bind over the already-populated data child at the
                # final validation boundary.  Writes used the retained child
                # descriptor, so the external tree must remain untouched, and
                # the restore must still reject the now-visible mount.
                restore_module._after_target_child_created = lambda *_args: None
                destination_late = root / "restore-late"
                destination_late.mkdir(mode=0o700)
                sentinel_late = destination_late / restore_module.RESTORE_SENTINEL
                sentinel_late.write_text(restore_module.EXPECTED_SENTINEL + "\\n")
                sentinel_late.chmod(0o600)
                external_late = root / "external-late"
                external_late.mkdir()
                marker_late = external_late / "keep"
                marker_late.write_bytes(b"unchanged")
                late_mode = external_late.stat().st_mode & 0o777
                late_mounted = False
                def inject_late(destination_fd):
                    global late_mounted
                    subprocess.run(
                        ["mount", "--bind", str(external_late),
                         f"/proc/self/fd/{{destination_fd}}/data"],
                        check=True,
                        pass_fds=(destination_fd,),
                    )
                    late_mounted = True
                restore_module._before_final_target_validation = inject_late
                try:
                    try:
                        restore_module.restore(
                            snapshot,
                            destination_late,
                            manifest_path,
                            key,
                            expected_target_uid=os.getuid(),
                            application_uid=os.getuid(),
                            application_gid=os.getgid(),
                            require_mount=False,
                            require_distinct_filesystem=False,
                        )
                    except ValueError as error:
                        assert "mount boundary" in str(error)
                    else:
                        raise AssertionError("late populated-data bind was accepted")
                    assert marker_late.read_bytes() == b"unchanged"
                    assert external_late.stat().st_mode & 0o777 == late_mode
                    assert sorted(path.name for path in external_late.iterdir()) == ["keep"]
                    assert not (destination_late / "restore-report.json").exists()
                finally:
                    if late_mounted:
                        subprocess.run(
                            ["umount", str(destination_late / "data")], check=True
                        )
            """
        )
        result = subprocess.run(
            [
                "unshare",
                "--user",
                "--map-root-user",
                "--mount",
                sys.executable,
                "-c",
                script,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 and "Operation not permitted" in result.stderr:
            self.skipTest("unprivileged user/mount namespaces are unavailable")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_descendant_redirect_after_creation_receives_no_chown_chmod_or_data(self):
        destination = self.target()
        victim = self.root / "descendant-victim"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_bytes(b"unchanged")
        original_mode = victim.stat().st_mode & 0o777

        def redirect(parent_fd, name):
            if name == "originals":
                path = Path(f"/proc/self/fd/{parent_fd}/{name}")
                path.rmdir()
                path.symlink_to(victim, target_is_directory=True)

        with mock.patch.object(
            fd_tree_module, "after_directory_create", side_effect=redirect
        ):
            with self.assertRaisesRegex(ValueError, "link redirect|symlink"):
                self.restore(destination, mode="sample", sample_count=100)
        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertEqual(victim.stat().st_mode & 0o777, original_mode)

    def test_application_sentinel_hardlink_swap_receives_no_metadata(self):
        destination = self.target()
        victim = self.root / "sentinel-hardlink-victim"
        victim.write_bytes(restore_module.EXPECTED_APPLICATION_SENTINEL)
        signed_mode = (
            self.snapshot / "data" / ".david-pi-storage"
        ).stat().st_mode & 0o777
        victim.chmod(signed_mode)

        def swap(data_root_fd):
            target = Path(
                f"/proc/self/fd/{data_root_fd}/.david-pi-storage"
            )
            target.unlink()
            os.link(victim, target)

        with mock.patch.object(
            restore_module,
            "_before_application_sentinel_open",
            side_effect=swap,
        ):
            with self.assertRaisesRegex(RuntimeError, "uniquely contained"):
                self.restore(destination)
        self.assertEqual(
            victim.read_bytes(), restore_module.EXPECTED_APPLICATION_SENTINEL
        )
        self.assertEqual(victim.stat().st_mode & 0o777, signed_mode)

    def test_symlink_hardlink_swap_receives_no_chown(self):
        link = self.snapshot / "data" / "favorite"
        link.symlink_to("originals/object-0.bin")
        metadata = {
            "snapshot_id": "fixture",
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
        manifest_module.write_manifest(
            self.snapshot, self.manifest, metadata, self.key
        )
        destination = self.target()
        victim = self.root / "symlink-hardlink-victim"
        victim.write_bytes(b"unchanged")
        victim.chmod(0o777)

        def swap(parent_fd, name):
            if name != "favorite":
                return
            target = Path(f"/proc/self/fd/{parent_fd}/{name}")
            target.unlink()
            os.link(victim, target)

        with mock.patch.object(
            fd_tree_module,
            "after_symlink_create",
            side_effect=swap,
        ):
            with self.assertRaisesRegex(RuntimeError, "symlink changed"):
                self.restore(destination, mode="full")
        self.assertEqual(victim.read_bytes(), b"unchanged")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o777)

    def test_missing_or_invalid_sentinel_is_rejected(self):
        destination = self.target(valid=False)
        with self.assertRaisesRegex(ValueError, "sentinel"):
            self.restore(destination)

    def test_restore_sentinel_rejects_extra_newlines(self):
        destination = self.target()
        (destination / restore_module.RESTORE_SENTINEL).write_text(
            restore_module.EXPECTED_SENTINEL + "\n\n", encoding="ascii"
        )
        with self.assertRaisesRegex(ValueError, "sentinel"):
            self.restore(destination)

    def test_nonempty_target_is_rejected(self):
        destination = self.target()
        (destination / "unexpected").write_text("stop", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.restore(destination)

    def test_full_restore_preserves_safe_symlinks_and_matches_signed_tree(self):
        link = self.snapshot / "data" / "favorite"
        link.symlink_to("originals/object-0.bin")
        metadata = {
            "snapshot_id": "fixture",
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
        manifest_module.write_manifest(self.snapshot, self.manifest, metadata, self.key)
        destination = self.target()
        report = self.restore(destination, mode="full")
        restored_link = destination / "data" / "favorite"
        self.assertTrue(restored_link.is_symlink())
        self.assertEqual(os.readlink(restored_link), "originals/object-0.bin")
        self.assertEqual(
            report["sampled_file_count"], self.manifest_payload["content"]["file_count"]
        )

    def test_same_size_corruption_during_full_copy_is_rejected(self):
        destination = self.target()
        real_copy = restore_module.copy_evidence_tree

        def corrupt_after_copy(source_fd, destination_fd, evidence, **kwargs):
            result = real_copy(source_fd, destination_fd, evidence, **kwargs)
            if any(record["path"] == "originals/object-4.bin" for record in evidence):
                target = destination / "data" / "originals" / "object-4.bin"
                target.write_bytes(b"x" * target.stat().st_size)
            return result

        with mock.patch.object(
            restore_module, "copy_evidence_tree", side_effect=corrupt_after_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "data tree"):
                self.restore(destination, mode="full")

    def test_same_size_configuration_corruption_during_copy_is_rejected(self):
        destination = self.target()
        real_copy = restore_module.copy_evidence_tree

        def corrupt_after_copy(source_fd, destination_fd, evidence, **kwargs):
            result = real_copy(source_fd, destination_fd, evidence, **kwargs)
            if any(record["path"] == "compose.yaml" for record in evidence):
                target = destination / "config" / "compose.yaml"
                target.write_text("services: []\n", encoding="utf-8")
            return result

        with mock.patch.object(
            restore_module, "copy_evidence_tree", side_effect=corrupt_after_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "config tree"):
                self.restore(destination)

    def test_database_tree_injection_during_copy_is_rejected(self):
        destination = self.target()
        real_copy = restore_module.copy_evidence_tree

        def inject_after_copy(source_fd, destination_fd, evidence, **kwargs):
            result = real_copy(source_fd, destination_fd, evidence, **kwargs)
            if any(record["path"].endswith("app.db") for record in evidence):
                (destination / "databases" / "injected").mkdir()
            return result

        with mock.patch.object(
            restore_module, "copy_evidence_tree", side_effect=inject_after_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "databases tree"):
                self.restore(destination)

    def test_target_owner_mode_and_mount_policy_fail_closed(self):
        destination = self.target()
        destination.chmod(0o755)
        with self.assertRaisesRegex(PermissionError, "mode 0700"):
            self.restore(destination)
        destination.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "dedicated mount"):
            self.restore(destination, require_mount=True)

    def test_target_symlink_is_rejected(self):
        destination = self.target()
        link = self.root / "restore-link"
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            self.restore(link)

    def test_descendant_of_production_storage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "protected broad path"):
            restore_module.validate_target(
                self.snapshot,
                "/srv/data/disposable-restore",
                expected_uid=os.getuid(),
                require_mount=False,
                require_distinct_filesystem=False,
            )

    def test_same_filesystem_target_is_rejected_when_required(self):
        destination = self.target()
        with mock.patch.object(restore_module.os.path, "ismount", return_value=True):
            with self.assertRaisesRegex(ValueError, "different filesystem"):
                self.restore(
                    destination,
                    require_mount=True,
                    require_distinct_filesystem=True,
                )

    def test_insufficient_capacity_is_rejected_before_copy(self):
        destination = self.target()
        no_space = SimpleNamespace(f_bavail=0, f_frsize=4096)
        with mock.patch.object(restore_module.os, "statvfs", return_value=no_space):
            with self.assertRaisesRegex(OSError, "enough available capacity"):
                self.restore(destination)

    def test_isolated_state_requires_explicit_network_proof(self):
        destination = self.target()
        report = self.restore(destination, network_isolation_verified=True)
        self.assertEqual(report["state"], "isolated_data_verified")
        self.assertTrue(report["network_isolation_verified"])
        self.assertFalse(report["drill_complete"])

    def test_cli_publishes_receipt_only_after_successful_isolated_restore(self):
        receipt_directory = self.root / "receipt-state"
        arguments = SimpleNamespace(
            snapshot=self.snapshot,
            latest_snapshots=None,
            destination=self.root / "unused-target",
            manifest=self.manifest,
            signing_key_file=self.key_file,
            receipt_directory=receipt_directory,
            mode="full",
            sample_count=100,
            application_uid=os.getuid(),
            application_gid=os.getgid(),
        )
        report = {
            "state": "isolated_data_verified",
            "mode": "full",
            "snapshot_id": self.manifest_payload["snapshot_id"],
            "manifest_sha256": self.manifest_payload["integrity"]["payload_sha256"],
            "completed_at": "2026-09-04T03:00:00+00:00",
            "network_isolation_verified": True,
            "signed_database_evidence_preserved": True,
            "application_layout_ready": True,
            "storage_sentinel_ready": True,
            "application_boot_verified": False,
            "drill_complete": False,
        }
        receipt = {"signed": True}
        with mock.patch.object(restore_module, "parse_args", return_value=arguments), \
             mock.patch.object(restore_module, "verify_network_isolation", return_value=True), \
             mock.patch.object(restore_module, "load_signing_key", return_value=self.key), \
             mock.patch.object(restore_module, "restore", return_value=report), \
             mock.patch.object(restore_module, "create_restore_receipt", return_value=receipt) as create, \
             mock.patch.object(restore_module, "publish_restore_receipt") as publish, \
             mock.patch("builtins.print"):
            restore_module.main()
        create.assert_called_once_with(report, self.key)
        publish.assert_called_once_with(receipt_directory, receipt)

        with mock.patch.object(restore_module, "parse_args", return_value=arguments), \
             mock.patch.object(restore_module, "verify_network_isolation", return_value=True), \
             mock.patch.object(restore_module, "load_signing_key", return_value=self.key), \
             mock.patch.object(restore_module, "restore", side_effect=RuntimeError("restore failed")), \
             mock.patch.object(restore_module, "publish_restore_receipt") as publish:
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                restore_module.main()
        publish.assert_not_called()

    def test_restore_report_symlink_redirection_never_touches_victim(self):
        destination = self.target()
        victim = self.root / "report-victim"
        victim.write_text("unchanged\n", encoding="utf-8")
        report_path = destination / "restore-report.json"
        report_path.symlink_to(victim)
        with self.assertRaisesRegex(PermissionError, "unsafe"):
            restore_module._atomic_write_report(destination, {"state": "verified"})
        self.assertEqual(victim.read_text(encoding="utf-8"), "unchanged\n")

    def test_restore_rejects_target_path_replacement_without_touching_replacement(self):
        destination = self.target()
        moved = self.root / "moved-restore"
        real_copy = restore_module.copy_evidence_tree
        swapped = False

        def replace_after_first_copy(source_fd, destination_fd, evidence, **kwargs):
            nonlocal swapped
            result = real_copy(source_fd, destination_fd, evidence, **kwargs)
            if not swapped and any(
                record["path"].endswith("app.db") for record in evidence
            ):
                destination.rename(moved)
                destination.mkdir(mode=0o700)
                (destination / "replacement-marker").write_text(
                    "unchanged\n", encoding="utf-8"
                )
                swapped = True
            return result

        with mock.patch.object(
            restore_module, "copy_evidence_tree", side_effect=replace_after_first_copy
        ):
            with self.assertRaisesRegex(RuntimeError, "pathname was replaced"):
                self.restore(destination)
        self.assertEqual(
            (destination / "replacement-marker").read_text(encoding="utf-8"),
            "unchanged\n",
        )
        self.assertFalse((destination / "restore-report.json").exists())

    def test_atomic_report_uses_pinned_parent_after_path_replacement(self):
        destination = self.target()
        descriptor = os.open(
            destination,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        moved = self.root / "moved-report-target"
        destination.rename(moved)
        destination.mkdir(mode=0o700)
        marker = destination / "replacement-marker"
        marker.write_text("unchanged\n", encoding="utf-8")
        try:
            restore_module._atomic_write_report(descriptor, {"state": "verified"})
        finally:
            os.close(descriptor)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse((destination / "restore-report.json").exists())
        self.assertEqual(
            json.loads((moved / "restore-report.json").read_text(encoding="utf-8")),
            {"state": "verified"},
        )


if __name__ == "__main__":
    unittest.main()
