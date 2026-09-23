import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import modules.secure_storage as secure_storage_module

from modules.secure_storage import (
    PinnedStorageRoot,
    StorageSafetyError,
    ensure_restricted_directory,
)


class PinnedStorageRootTests(unittest.TestCase):
    def test_new_managed_directory_is_private_under_cooperative_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = Path(temporary) / "managed"
            previous = os.umask(0o002)
            try:
                self.assertTrue(ensure_restricted_directory(managed))
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o700)
            PinnedStorageRoot(managed)

    def test_existing_managed_directory_permissions_are_never_changed_silently(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = Path(temporary) / "managed"
            previous = os.umask(0)
            try:
                managed.mkdir(mode=0o770)
            finally:
                os.umask(previous)
            self.assertFalse(ensure_restricted_directory(managed))
            self.assertEqual(stat.S_IMODE(managed.stat().st_mode), 0o770)
            with self.assertRaises(StorageSafetyError):
                PinnedStorageRoot(managed)

    def test_files_module_imports_with_normal_umask_and_private_new_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = os.environ.copy()
            environment.update(
                DAVID_PI_FILES_DATA=str(root / "files"),
                DAVID_PI_PLATFORM_DATA=str(root / "platform"),
                PHOTO_DATA=str(root),
            )
            script = """
import os
import stat
os.umask(0o002)
from modules import files
for path in (files.STORAGE, files.OBJECTS, files.INCOMING, files.PDF_CACHE):
    assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
"""
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_parent_replacement_cannot_redirect_descriptor_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            managed = base / "managed"
            parked = base / "parked"
            outside = base / "outside"
            source = base / "source"
            ensure_restricted_directory(managed)
            outside.mkdir()
            source.write_bytes(b"saved")
            root = PinnedStorageRoot(managed)
            source_descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                managed.rename(parked)
                managed.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(StorageSafetyError):
                    root.link_descriptor(source_descriptor, "object.bin")
                self.assertFalse((outside / "object.bin").exists())
                self.assertFalse((parked / "object.bin").exists())
            finally:
                os.close(source_descriptor)

    def test_open_descriptor_cannot_be_redirected_by_entry_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            managed = base / "managed"
            ensure_restricted_directory(managed)
            item = managed / "item.bin"
            parked = managed / "parked.bin"
            outside = base / "outside.bin"
            item.write_bytes(b"saved")
            outside.write_bytes(b"leaks")
            root = PinnedStorageRoot(managed)
            descriptor, _metadata = root.open_regular("item.bin", expected_size=5)
            try:
                item.rename(parked)
                item.symlink_to(outside)
                self.assertEqual(os.read(descriptor, 5), b"saved")
            finally:
                os.close(descriptor)

    def test_symlink_entry_is_never_opened_as_managed_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            managed = base / "managed"
            ensure_restricted_directory(managed)
            outside = base / "outside.bin"
            outside.write_bytes(b"private")
            (managed / "item.bin").symlink_to(outside)
            root = PinnedStorageRoot(managed)
            with self.assertRaises(OSError):
                root.open_regular("item.bin", expected_size=7)

    def test_nested_open_stays_on_pinned_ancestor_after_path_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            managed = base / "managed"
            year = managed / "2026"
            month = year / "08"
            outside = base / "outside"
            outside_month = outside / "08"
            for directory in (managed, year, month, outside, outside_month):
                ensure_restricted_directory(directory)
            (month / "item.bin").write_bytes(b"saved")
            (outside_month / "item.bin").write_bytes(b"leaks")
            parked = managed / "parked-2026"
            root = PinnedStorageRoot(managed)
            real_stat = os.stat
            swapped = False

            def stat_then_swap(path, *args, **kwargs):
                nonlocal swapped
                metadata = real_stat(path, *args, **kwargs)
                if path == "2026" and kwargs.get("dir_fd") is not None and not swapped:
                    year.rename(parked)
                    year.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return metadata

            try:
                with patch.object(
                    secure_storage_module.os, "stat", side_effect=stat_then_swap
                ):
                    descriptor, _metadata = root.open_regular_path(
                        "2026/08/item.bin", expected_size=5
                    )
                try:
                    self.assertEqual(os.read(descriptor, 5), b"saved")
                finally:
                    os.close(descriptor)
            finally:
                if year.is_symlink():
                    year.unlink()
                if parked.exists():
                    parked.rename(year)


if __name__ == "__main__":
    unittest.main()
