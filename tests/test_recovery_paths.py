import importlib.util
import ctypes
import errno
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "david_pi_recovery_paths.py"
SPEC = importlib.util.spec_from_file_location("recovery_paths", SCRIPT)
paths = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paths)


class RecoveryPathSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "backup"
        self.destination.mkdir(mode=0o700)
        self.snapshots = self.destination / "snapshots"
        paths.prepare_snapshot_root(self.destination, self.snapshots)
        self.snapshot_id = "20260903T120000Z"
        self.work = self.snapshots / f".incomplete-{self.snapshot_id}"
        self.final = self.snapshots / self.snapshot_id

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self):
        return paths.prepare_work(
            self.destination,
            self.snapshots,
            self.work,
            started_at="2026-09-03T12:00:00Z",
        )

    def prepare_pinned_final(self):
        destination_fd = os.open(
            self.destination, os.O_RDONLY | os.O_DIRECTORY
        )
        destination_identity = paths.describe_root_pin(
            self.destination, destination_fd
        )
        _, work_identity = paths.prepare_work_pinned(
            self.destination,
            destination_fd,
            destination_identity,
            self.work.name,
            started_at="2026-09-03T12:00:00Z",
        )
        manifest = self.work / "MANIFEST.json"
        manifest.write_bytes(b"{}\n")
        manifest.chmod(0o600)
        work_fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        paths.finalize_work_pinned(
            self.destination,
            destination_fd,
            destination_identity,
            self.work,
            work_fd,
            work_identity,
            self.snapshot_id,
        )
        return destination_fd, destination_identity, work_fd, work_identity

    def test_prepares_and_revalidates_only_owned_direct_children(self):
        self.assertEqual(self.prepare(), "2026-09-03T12:00:00Z")
        self.assertEqual(
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            ),
            "2026-09-03T12:00:00Z",
        )
        for name in paths.EXPECTED_CHILDREN:
            self.assertEqual((self.work / name).stat().st_mode & 0o777, 0o700)

    def test_resecures_direct_children_after_copy_tool_mode_drift(self):
        self.prepare()
        for name in paths.EXPECTED_CHILDREN:
            (self.work / name).chmod(0o755)
        paths.resecure_work_roots(self.destination, self.snapshots, self.work)
        for name in paths.EXPECTED_CHILDREN:
            self.assertEqual((self.work / name).stat().st_mode & 0o777, 0o700)

    def test_work_symlink_collision_never_touches_victim(self):
        victim = self.root / "victim"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_text("unchanged", encoding="utf-8")
        self.work.symlink_to(victim, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(victim.iterdir()), [marker])

    def test_snapshot_root_symlink_collision_never_touches_victim(self):
        alternate = self.root / "alternate-backup"
        alternate.mkdir(mode=0o700)
        victim = self.root / "victim-root"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_text("unchanged", encoding="utf-8")
        linked_snapshots = alternate / "snapshots"
        linked_snapshots.symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            paths.prepare_snapshot_root(alternate, linked_snapshots)
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_resume_rejects_child_symlink_redirection_and_preserves_victim(self):
        self.prepare()
        victim = self.root / "victim"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_text("unchanged", encoding="utf-8")
        (self.work / "data").rmdir()
        (self.work / "data").symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            paths.prepare_work(
                self.destination, self.snapshots, self.work, resume=True
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_nested_configuration_copy_target_redirect_is_rejected(self):
        self.prepare()
        victim = self.root / "config-victim"
        victim.mkdir()
        marker = victim / "keep"
        marker.write_text("unchanged", encoding="utf-8")
        (self.work / "config" / "photo-portal").symlink_to(
            victim, target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "symlink|redirect"):
            paths.prepare_copy_target(
                self.destination,
                self.snapshots,
                self.work,
                "config/photo-portal",
            )
        with self.assertRaisesRegex(ValueError, "symlink|redirect"):
            paths.validate_mutable_tree(
                self.destination, self.snapshots, self.work, "config"
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_shared_work_inode_is_rejected_before_metadata_mutation(self):
        self.prepare()
        signed_history = self.root / "signed-history.bin"
        signed_history.write_bytes(b"immutable")
        signed_history.chmod(0o640)
        os.link(signed_history, self.work / "data" / "object.bin")
        original_mode = signed_history.stat().st_mode & 0o777
        with self.assertRaisesRegex(ValueError, "shared inode"):
            paths.validate_mutable_tree(
                self.destination,
                self.snapshots,
                self.work,
                "data",
                allow_file_symlinks=True,
            )
        self.assertEqual(signed_history.stat().st_mode & 0o777, original_mode)
        self.assertEqual(signed_history.read_bytes(), b"immutable")

    def test_clear_mutable_tree_removes_only_prevalidated_entries(self):
        self.prepare()
        nested = self.work / "databases" / "platform"
        nested.mkdir()
        (nested / "app.db").write_bytes(b"database")
        paths.clear_mutable_tree(
            self.destination, self.snapshots, self.work, "databases"
        )
        self.assertEqual(list((self.work / "databases").iterdir()), [])

    def test_clear_mutable_tree_fails_before_deleting_when_mount_check_fails(self):
        self.prepare()
        database_root = self.work / "databases"
        first = database_root / "a-first.db"
        first.write_bytes(b"preserve")
        mounted = database_root / "mounted"
        mounted.mkdir()
        (mounted / "victim.db").write_bytes(b"external")
        real_pin = paths._pin_child

        def reject_mount(directory_fd, name):
            if name == "mounted":
                raise ValueError("protected recovery traversal crossed a mount boundary")
            return real_pin(directory_fd, name)

        with mock.patch.object(paths, "_pin_child", side_effect=reject_mount):
            with self.assertRaisesRegex(ValueError, "mount boundary"):
                paths.clear_mutable_tree(
                    self.destination, self.snapshots, self.work, "databases"
                )
        self.assertEqual(first.read_bytes(), b"preserve")
        self.assertEqual((mounted / "victim.db").read_bytes(), b"external")

    def test_mutable_tree_fails_closed_when_openat2_is_unavailable(self):
        self.prepare()

        def unavailable(*_args):
            ctypes.set_errno(errno.ENOSYS)
            return -1

        with mock.patch.object(paths._LIBC, "syscall", side_effect=unavailable):
            with self.assertRaisesRegex(RuntimeError, "openat2 is required"):
                paths.validate_mutable_tree(
                    self.destination, self.snapshots, self.work, "databases"
                )

    def test_real_same_filesystem_bind_mount_is_rejected_before_deletion(self):
        script = textwrap.dedent(
            f"""
            import importlib.util
            import pathlib
            import subprocess
            import tempfile

            spec = importlib.util.spec_from_file_location(
                "recovery_paths", {str(SCRIPT)!r}
            )
            paths = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(paths)
            with tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                destination = root / "backup"
                destination.mkdir(mode=0o700)
                snapshots = destination / "snapshots"
                paths.prepare_snapshot_root(destination, snapshots)
                work = snapshots / ".incomplete-20260904T000000Z"
                paths.prepare_work(
                    destination,
                    snapshots,
                    work,
                    started_at="2026-09-04T00:00:00Z",
                )
                first = work / "databases" / "a-first.db"
                first.write_bytes(b"preserve")
                external = root / "external"
                external.mkdir()
                victim = external / "victim.db"
                victim.write_bytes(b"external")
                mounted = work / "databases" / "mounted"
                mounted.mkdir()
                subprocess.run(
                    ["mount", "--bind", str(external), str(mounted)], check=True
                )
                try:
                    try:
                        paths.clear_mutable_tree(
                            destination, snapshots, work, "databases"
                        )
                    except ValueError as error:
                        assert "mount boundary" in str(error)
                    else:
                        raise AssertionError("bind mount was accepted")
                    assert first.read_bytes() == b"preserve"
                    assert victim.read_bytes() == b"external"
                finally:
                    subprocess.run(["umount", str(mounted)], check=True)
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

    def test_pinned_sync_rejects_bind_inserted_at_actual_delete_timing(self):
        script = textwrap.dedent(
            f"""
            import importlib.util
            import os
            import pathlib
            import subprocess
            import sys
            import tempfile

            sys.path.insert(0, {str(ROOT / 'deploy')!r})
            import david_pi_fd_tree as fd_tree
            spec = importlib.util.spec_from_file_location(
                "recovery_paths", {str(SCRIPT)!r}
            )
            paths = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(paths)
            with tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                source = root / "source"
                source.mkdir(mode=0o700)
                (source / "new.bin").write_bytes(b"new")
                destination = root / "backup"
                destination.mkdir(mode=0o700)
                destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
                destination_identity = paths.describe_root_pin(destination, destination_fd)
                paths.prepare_snapshot_root_pinned(
                    destination, destination_fd, destination_identity
                )
                started, work_identity = paths.prepare_work_pinned(
                    destination,
                    destination_fd,
                    destination_identity,
                    ".incomplete-20260904T010000Z",
                    started_at="2026-09-04T01:00:00Z",
                )
                work = destination / "snapshots" / ".incomplete-20260904T010000Z"
                work_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
                target = work / "data"
                mounted = target / "a-mounted"
                mounted.mkdir()
                preserve = target / "z-preserve.bin"
                preserve.write_bytes(b"preserve")
                external = root / "external"
                external.mkdir()
                victim = external / "victim.bin"
                victim.write_bytes(b"external")
                installed = False
                def inject(parent_fd, name, operation):
                    global installed
                    if not installed and name == "a-mounted" and operation == "remove":
                        subprocess.run(
                            ["mount", "--bind", str(external),
                             f"/proc/self/fd/{{parent_fd}}/{{name}}"],
                            check=True,
                            pass_fds=(parent_fd,),
                        )
                        installed = True
                fd_tree.before_entry_mutation = inject
                try:
                    try:
                        paths.sync_work_tree_pinned(
                            source,
                            work,
                            work_fd,
                            work_identity,
                            "data",
                            require_distinct_filesystem=False,
                        )
                    except ValueError as error:
                        assert "mount boundary" in str(error)
                    else:
                        raise AssertionError("late bind mount was accepted")
                    assert preserve.read_bytes() == b"preserve"
                    assert victim.read_bytes() == b"external"
                finally:
                    if installed:
                        subprocess.run(["umount", str(mounted)], check=True)
                    os.close(work_fd)
                    os.close(destination_fd)
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

    def test_retained_root_pin_rejects_same_filesystem_replacement_with_sentinel(self):
        sentinel = self.root / "source" / ".david-pi-storage"
        source = sentinel.parent
        source.mkdir(mode=0o700)
        sentinel.write_bytes(b"david-pi-family-storage-v1\n")
        sentinel.chmod(0o600)
        descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        identity = paths.describe_root_pin(source, descriptor)
        moved = self.root / "source-original"
        source.rename(moved)
        source.mkdir(mode=0o700)
        replacement = source / ".david-pi-storage"
        replacement.write_bytes(b"david-pi-family-storage-v1\n")
        replacement.chmod(0o600)
        try:
            with self.assertRaisesRegex(RuntimeError, "pathname was replaced"):
                paths.validate_root_pin(source, descriptor, identity)
        finally:
            os.close(descriptor)
        self.assertEqual(replacement.read_bytes(), b"david-pi-family-storage-v1\n")
        self.assertEqual(
            (moved / ".david-pi-storage").read_bytes(),
            b"david-pi-family-storage-v1\n",
        )

    def test_pinned_sentinels_reject_destination_replacement_with_copied_value(self):
        source = self.root / "source"
        source.mkdir(mode=0o700)
        source_sentinel = source / ".david-pi-storage"
        source_sentinel.write_bytes(b"david-pi-family-storage-v1\n")
        source_sentinel.chmod(0o600)
        backup_sentinel = self.destination / ".david-pi-backup-storage"
        backup_sentinel.write_bytes(b"david-pi-independent-backup-v1\n")
        backup_sentinel.chmod(0o600)
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        destination_fd = os.open(
            self.destination, os.O_RDONLY | os.O_DIRECTORY
        )
        source_identity = paths.describe_root_pin(source, source_fd)
        destination_identity = paths.describe_root_pin(
            self.destination, destination_fd
        )
        paths.validate_sentinels_pinned(
            source,
            source_fd,
            source_identity,
            "david-pi-family-storage-v1",
            self.destination,
            destination_fd,
            destination_identity,
            "david-pi-independent-backup-v1",
        )
        moved = self.root / "backup-original"
        self.destination.rename(moved)
        self.destination.mkdir(mode=0o700)
        replacement = self.destination / ".david-pi-backup-storage"
        replacement.write_bytes(b"david-pi-independent-backup-v1\n")
        replacement.chmod(0o600)
        try:
            with self.assertRaisesRegex(RuntimeError, "pathname was replaced"):
                paths.validate_sentinels_pinned(
                    source,
                    source_fd,
                    source_identity,
                    "david-pi-family-storage-v1",
                    self.destination,
                    destination_fd,
                    destination_identity,
                    "david-pi-independent-backup-v1",
                )
        finally:
            os.close(destination_fd)
            os.close(source_fd)
        self.assertEqual(
            replacement.read_bytes(), b"david-pi-independent-backup-v1\n"
        )

    def test_pinned_final_rejects_same_filesystem_name_replacement(self):
        (
            destination_fd,
            destination_identity,
            snapshot_fd,
            snapshot_identity,
        ) = self.prepare_pinned_final()
        moved = self.root / "retained-final"
        self.final.rename(moved)
        self.final.mkdir(mode=0o700)
        replacement_manifest = self.final / "MANIFEST.json"
        replacement_manifest.write_bytes(b"{}\n")
        replacement_manifest.chmod(0o600)
        try:
            with self.assertRaisesRegex(RuntimeError, "name was replaced"):
                paths.publish_latest_pinned(
                    self.destination,
                    destination_fd,
                    destination_identity,
                    self.snapshot_id,
                    self.final,
                    snapshot_fd,
                    snapshot_identity,
                )
        finally:
            os.close(snapshot_fd)
            os.close(destination_fd)
        self.assertFalse((self.snapshots / "latest").exists())
        self.assertEqual(replacement_manifest.read_bytes(), b"{}\n")
        self.assertEqual((moved / "MANIFEST.json").read_bytes(), b"{}\n")

    def test_pinned_latest_candidate_swap_is_rejected_before_publish(self):
        (
            destination_fd,
            destination_identity,
            snapshot_fd,
            snapshot_identity,
        ) = self.prepare_pinned_final()
        victim = self.root / "latest-candidate-victim"
        victim.write_bytes(b"unchanged")
        victim.chmod(0o640)

        def replace_candidate(snapshots_fd, temporary_name):
            candidate = Path(
                f"/proc/self/fd/{snapshots_fd}/{temporary_name}"
            )
            candidate.unlink()
            os.link(victim, candidate)

        try:
            with mock.patch.object(
                paths,
                "after_latest_symlink_create",
                side_effect=replace_candidate,
            ):
                with self.assertRaisesRegex(RuntimeError, "candidate latest"):
                    paths.publish_latest_pinned(
                        self.destination,
                        destination_fd,
                        destination_identity,
                        self.snapshot_id,
                        self.final,
                        snapshot_fd,
                        snapshot_identity,
                    )
        finally:
            os.close(snapshot_fd)
            os.close(destination_fd)
        self.assertFalse((self.snapshots / "latest").exists())
        self.assertEqual(victim.read_bytes(), b"unchanged")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o640)

    def test_pinned_latest_publication_revalidates_retained_snapshot(self):
        (
            destination_fd,
            destination_identity,
            snapshot_fd,
            snapshot_identity,
        ) = self.prepare_pinned_final()
        try:
            paths.publish_latest_pinned(
                self.destination,
                destination_fd,
                destination_identity,
                self.snapshot_id,
                self.final,
                snapshot_fd,
                snapshot_identity,
            )
            self.assertEqual(
                os.readlink(self.snapshots / "latest"), self.snapshot_id
            )
            paths.validate_final_pinned(
                self.destination,
                destination_fd,
                destination_identity,
                self.final,
                snapshot_fd,
                snapshot_identity,
                self.snapshot_id,
            )
        finally:
            os.close(snapshot_fd)
            os.close(destination_fd)

    def test_sync_rejects_source_replacement_at_inventory_copy_boundary(self):
        source = self.root / "source"
        source.mkdir(mode=0o700)
        (source / ".david-pi-storage").write_bytes(
            b"david-pi-family-storage-v1\n"
        )
        (source / "original.bin").write_bytes(b"original")
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        source_identity = paths.describe_root_pin(source, source_fd)

        destination_fd = os.open(
            self.destination, os.O_RDONLY | os.O_DIRECTORY
        )
        destination_identity = paths.describe_root_pin(
            self.destination, destination_fd
        )
        paths.prepare_work_pinned(
            self.destination,
            destination_fd,
            destination_identity,
            self.work.name,
            started_at="2026-09-03T12:00:00Z",
        )
        work_fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        work_identity = paths.describe_root_pin(self.work, work_fd)
        preserve = self.work / "data" / "preserve.bin"
        preserve.write_bytes(b"preserve")
        moved = self.root / "source-original"
        real_inventory = paths.inventory_tree_fd
        replaced = False

        def replace_after_source_inventory(descriptor, **kwargs):
            nonlocal replaced
            records = real_inventory(descriptor, **kwargs)
            if not replaced and os.fstat(descriptor).st_ino == os.fstat(source_fd).st_ino:
                source.rename(moved)
                source.mkdir(mode=0o700)
                (source / ".david-pi-storage").write_bytes(
                    b"david-pi-family-storage-v1\n"
                )
                (source / "replacement.bin").write_bytes(b"replacement")
                replaced = True
            return records

        try:
            with mock.patch.object(
                paths,
                "inventory_tree_fd",
                side_effect=replace_after_source_inventory,
            ):
                with self.assertRaisesRegex(RuntimeError, "pathname was replaced"):
                    paths.sync_work_tree_pinned(
                        source,
                        self.work,
                        work_fd,
                        work_identity,
                        "data",
                        source_fd=source_fd,
                        source_identity=source_identity,
                        require_distinct_filesystem=False,
                    )
        finally:
            os.close(work_fd)
            os.close(destination_fd)
            os.close(source_fd)
        self.assertTrue(replaced)
        self.assertEqual(preserve.read_bytes(), b"preserve")
        self.assertEqual((source / "replacement.bin").read_bytes(), b"replacement")
        self.assertEqual(
            (source / ".david-pi-storage").read_bytes(),
            (moved / ".david-pi-storage").read_bytes(),
        )

    def test_database_copy_rejects_source_replacement_before_clear(self):
        source = self.root / "database-source"
        source.mkdir(mode=0o700)
        sentinel = source / ".david-pi-storage"
        sentinel.write_bytes(b"david-pi-family-storage-v1\n")
        sentinel.chmod(0o600)
        with sqlite3.connect(source / "app.db") as connection:
            connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
        source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
        source_identity = paths.describe_root_pin(source, source_fd)

        destination_fd = os.open(
            self.destination, os.O_RDONLY | os.O_DIRECTORY
        )
        destination_identity = paths.describe_root_pin(
            self.destination, destination_fd
        )
        paths.prepare_work_pinned(
            self.destination,
            destination_fd,
            destination_identity,
            self.work.name,
            started_at="2026-09-03T12:00:00Z",
        )
        work_fd = os.open(self.work, os.O_RDONLY | os.O_DIRECTORY)
        work_identity = paths.describe_root_pin(self.work, work_fd)
        preserve = self.work / "databases" / "preserve.db"
        preserve.write_bytes(b"preserve")
        moved = self.root / "database-source-original"
        real_inventory = paths.inventory_tree_fd
        replaced = False

        def replace_after_inventory(descriptor, **kwargs):
            nonlocal replaced
            records = real_inventory(descriptor, **kwargs)
            if not replaced and os.fstat(descriptor).st_ino == os.fstat(source_fd).st_ino:
                source.rename(moved)
                source.mkdir(mode=0o700)
                replacement = source / ".david-pi-storage"
                replacement.write_bytes(b"david-pi-family-storage-v1\n")
                replacement.chmod(0o600)
                replaced = True
            return records

        try:
            with mock.patch.object(
                paths,
                "inventory_tree_fd",
                side_effect=replace_after_inventory,
            ):
                with self.assertRaisesRegex(RuntimeError, "pathname was replaced"):
                    paths.copy_sqlite_databases_pinned(
                        source,
                        source_fd,
                        source_identity,
                        self.work,
                        work_fd,
                        work_identity,
                    )
        finally:
            os.close(work_fd)
            os.close(destination_fd)
            os.close(source_fd)
        self.assertTrue(replaced)
        self.assertEqual(preserve.read_bytes(), b"preserve")
        self.assertEqual(
            (source / ".david-pi-storage").read_bytes(),
            (moved / ".david-pi-storage").read_bytes(),
        )

    def test_allowed_file_symlink_must_stay_unique_and_inside_work_tree(self):
        self.prepare()
        link = self.work / "data" / "favorite"
        link.symlink_to("object.bin")
        paths.validate_mutable_tree(
            self.destination,
            self.snapshots,
            self.work,
            "data",
            allow_file_symlinks=True,
        )
        link.unlink()
        link.symlink_to("../../../../victim")
        with self.assertRaisesRegex(ValueError, "unsafe symlink"):
            paths.validate_mutable_tree(
                self.destination,
                self.snapshots,
                self.work,
                "data",
                allow_file_symlinks=True,
            )

    def test_impossible_calendar_timestamp_is_rejected_before_work_creation(self):
        with self.assertRaisesRegex(ValueError, "timestamp"):
            paths.prepare_work(
                self.destination,
                self.snapshots,
                self.work,
                started_at="2026-19-39T29:59:59Z",
            )
        self.assertFalse(self.work.exists())
        self.prepare()
        (self.work / ".started-at").write_text(
            "2026-19-39T29:59:59Z\n", encoding="ascii"
        )
        with self.assertRaisesRegex(ValueError, "timestamp"):
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            )
        impossible_work = self.snapshots / ".incomplete-20261939T295959Z"
        with self.assertRaisesRegex(ValueError, "id"):
            paths.prepare_work(
                self.destination,
                self.snapshots,
                impossible_work,
                started_at="2026-09-03T12:00:00Z",
            )
        self.assertFalse(impossible_work.exists())

    def test_marker_symlink_mode_and_content_fail_closed(self):
        self.prepare()
        marker = self.work / ".started-at"
        marker.write_text("2026-09-03T12:00:00Z\nextra\n", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "content is not exact"):
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            )
        marker.write_text("2026-09-03T12:00:00Z\n", encoding="ascii")
        marker.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "mode 0600"):
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            )
        marker.unlink()
        victim = self.root / "marker-victim"
        victim.write_text("2026-09-03T12:00:00Z\n", encoding="ascii")
        marker.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlink"):
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            )

    def test_final_collision_of_any_kind_fails_before_publish(self):
        self.prepare()
        victim = self.root / "final-victim"
        victim.mkdir()
        self.final.symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "already exists"):
            paths.validate_work(
                self.destination, self.snapshots, self.work, self.final
            )

    def make_signed_shape(self):
        self.final.mkdir(mode=0o700)
        manifest = self.final / "MANIFEST.json"
        manifest.write_text("{}\n", encoding="utf-8")
        manifest.chmod(0o600)
        return manifest

    def test_latest_requires_owned_relative_direct_timestamp_child(self):
        self.make_signed_shape()
        latest = self.snapshots / "latest"
        for target in (str(self.final), "../victim", "nested/20260903T120000Z"):
            latest.unlink(missing_ok=True)
            latest.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "direct timestamp child"):
                paths.resolve_latest(self.snapshots, latest)
        latest.unlink()
        latest.write_text(self.snapshot_id, encoding="ascii")
        with self.assertRaisesRegex(ValueError, "symlink"):
            paths.resolve_latest(self.snapshots, latest)

    def test_latest_missing_or_public_manifest_fails_closed(self):
        latest = self.snapshots / "latest"
        self.final.mkdir(mode=0o700)
        latest.symlink_to(self.snapshot_id)
        with self.assertRaisesRegex(ValueError, "manifest.*missing"):
            paths.resolve_latest(self.snapshots, latest)
        manifest = self.final / "MANIFEST.json"
        manifest.write_text("{}\n", encoding="utf-8")
        manifest.chmod(0o644)
        with self.assertRaisesRegex(PermissionError, "private"):
            paths.resolve_latest(self.snapshots, latest)

    def test_publish_latest_is_atomic_and_rejects_unsafe_existing_link(self):
        self.make_signed_shape()
        latest = self.snapshots / "latest"
        paths.publish_latest(
            self.destination, self.snapshots, latest, self.snapshot_id
        )
        self.assertEqual(os.readlink(latest), self.snapshot_id)
        latest.unlink()
        latest.symlink_to("../victim")
        with self.assertRaisesRegex(ValueError, "direct timestamp child"):
            paths.publish_latest(
                self.destination, self.snapshots, latest, self.snapshot_id
            )

    def test_publish_temp_collision_is_not_removed_or_overwritten(self):
        self.make_signed_shape()
        collision = self.snapshots / f".latest-{os.getpid()}.tmp"
        collision.write_text("unchanged", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            paths.publish_latest(
                self.destination,
                self.snapshots,
                self.snapshots / "latest",
                self.snapshot_id,
            )
        self.assertEqual(collision.read_text(encoding="utf-8"), "unchanged")

    def test_sentinel_requires_exact_private_regular_bytes(self):
        sentinel = self.destination / ".david-pi-backup-storage"
        sentinel.write_bytes(b"david-pi-independent-backup-v1\n")
        sentinel.chmod(0o600)
        paths.validate_sentinel(
            self.destination,
            sentinel,
            "david-pi-independent-backup-v1",
            "backup sentinel",
        )
        for invalid in (
            b"david-pi-independent-backup-v1",
            b"david-pi-independent-backup-v1\n\n",
            b" david-pi-independent-backup-v1\n",
        ):
            sentinel.write_bytes(invalid)
            with self.assertRaisesRegex(ValueError, "content is not exact"):
                paths.validate_sentinel(
                    self.destination,
                    sentinel,
                    "david-pi-independent-backup-v1",
                    "backup sentinel",
                )
        sentinel.unlink()
        victim = self.root / "sentinel-victim"
        victim.write_bytes(b"david-pi-independent-backup-v1\n")
        sentinel.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlink"):
            paths.validate_sentinel(
                self.destination,
                sentinel,
                "david-pi-independent-backup-v1",
                "backup sentinel",
            )
        self.assertEqual(
            victim.read_bytes(), b"david-pi-independent-backup-v1\n"
        )

    def test_sentinel_rejects_hardlink_without_touching_victim(self):
        victim = self.root / "sentinel-hardlink-victim"
        victim.write_bytes(b"david-pi-independent-backup-v1\n")
        victim.chmod(0o600)
        sentinel = self.destination / ".david-pi-backup-storage"
        os.link(victim, sentinel)
        with self.assertRaisesRegex(ValueError, "not uniquely contained"):
            paths.validate_sentinel(
                self.destination,
                sentinel,
                "david-pi-independent-backup-v1",
                "backup sentinel",
            )
        self.assertEqual(
            victim.read_bytes(), b"david-pi-independent-backup-v1\n"
        )

    def test_atomic_status_writer_rejects_symlink_and_hardlink_victims(self):
        for name in ("last-attempt.json", "last-success.json"):
            symlink_victim = self.root / f"{name}-symlink-victim"
            symlink_victim.write_bytes(b"unchanged\n")
            status = self.destination / name
            status.symlink_to(symlink_victim)
            with self.assertRaisesRegex(PermissionError, "unsafe"):
                paths.write_destination_file(
                    self.destination, name, b'{"state":"failed"}\n'
                )
            self.assertEqual(symlink_victim.read_bytes(), b"unchanged\n")
            status.unlink()

        hardlink_victim = self.root / "status-hardlink-victim"
        hardlink_victim.write_bytes(b"unchanged\n")
        status = self.destination / "last-attempt.json"
        os.link(hardlink_victim, status)
        with self.assertRaisesRegex(PermissionError, "unsafe"):
            paths.write_destination_file(
                self.destination, "last-attempt.json", b'{"state":"failed"}\n'
            )
        self.assertEqual(hardlink_victim.read_bytes(), b"unchanged\n")

    def test_atomic_writer_never_reuses_a_predictable_temporary_collision(self):
        token = "a" * 32
        collision = self.destination / f".last-success.json.{token}.tmp"
        victim = self.root / "temporary-victim"
        victim.write_bytes(b"unchanged\n")
        collision.symlink_to(victim)
        with mock.patch.object(paths.secrets, "token_hex", return_value=token):
            with self.assertRaises(FileExistsError):
                paths.write_destination_file(
                    self.destination, "last-success.json", b'{"state":"healthy"}\n'
                )
        self.assertTrue(collision.is_symlink())
        self.assertEqual(victim.read_bytes(), b"unchanged\n")

    def test_atomic_writer_rejects_parent_replacement_before_creation(self):
        moved = self.root / "moved-backup"
        real_open = paths.os.open
        replaced = False

        def replace_parent(path, flags, *args, **kwargs):
            nonlocal replaced
            if not replaced and Path(path) == self.destination:
                self.destination.rename(moved)
                self.destination.mkdir(mode=0o700)
                (self.destination / "marker").write_bytes(b"unchanged\n")
                replaced = True
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(paths.os, "open", side_effect=replace_parent):
            with self.assertRaisesRegex(RuntimeError, "parent was replaced"):
                paths.write_destination_file(
                    self.destination, "last-success.json", b'{"state":"healthy"}\n'
                )
        self.assertEqual(
            (self.destination / "marker").read_bytes(), b"unchanged\n"
        )
        self.assertFalse((self.destination / "last-success.json").exists())
        self.assertFalse((moved / "last-success.json").exists())

    def test_legacy_fixed_status_temporary_symlinks_are_never_opened(self):
        for name in ("last-attempt.json", "last-success.json"):
            victim = self.root / f"{name}-legacy-temporary-victim"
            victim.write_bytes(b"unchanged\n")
            legacy_temporary = self.destination / f"{name}.tmp"
            legacy_temporary.symlink_to(victim)
            paths.write_destination_file(
                self.destination, name, b'{"state":"healthy"}\n'
            )
            self.assertTrue(legacy_temporary.is_symlink())
            self.assertEqual(victim.read_bytes(), b"unchanged\n")

    def test_work_summary_writer_rejects_redirect_without_touching_victim(self):
        self.prepare()
        victim = self.root / "summary-victim"
        victim.write_bytes(b"unchanged\n")
        (self.work / "MANIFEST.txt").symlink_to(victim)
        with self.assertRaisesRegex(PermissionError, "unsafe"):
            paths.write_work_file(
                self.destination,
                self.snapshots,
                self.work,
                "MANIFEST.txt",
                b"snapshot=test\n",
            )
        self.assertEqual(victim.read_bytes(), b"unchanged\n")


if __name__ == "__main__":
    unittest.main()
