import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "david-pi-backup.py"
SPEC = importlib.util.spec_from_file_location("daily_backup", SOURCE)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class DailyBackupTest(unittest.TestCase):
    def test_inventory_includes_audiobook_playback_queue(self):
        relative_paths = [entry[1].as_posix() for entry in backup.database_plan()]
        self.assertIn(".david-pi-operations/audiobook/playback-queue.db", relative_paths)
        self.assertEqual(len(relative_paths), 12)

    def test_manifest_maps_relative_sources_to_compatible_flat_backups(self):
        plan = backup.database_plan()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = backup.write_database_manifest(Path(temporary), plan)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_mode = os.stat(manifest_path).st_mode & 0o777

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["layout"], "flat-basename-v1")
        self.assertEqual(manifest["database_count"], len(plan))
        mappings = {
            entry["source_relative_path"]: entry["backup_relative_path"]
            for entry in manifest["databases"]
        }
        self.assertEqual(
            mappings[".david-pi-operations/audiobook/playback-queue.db"],
            "databases/playback-queue.db",
        )
        self.assertTrue(all(not Path(source).is_absolute() for source in mappings))
        self.assertEqual(len(set(mappings.values())), len(mappings))
        self.assertEqual(manifest_mode, 0o600)

    def test_flat_name_collision_fails_before_backup(self):
        colliding = (
            backup.DATA / "first/shared.db",
            backup.DATA / "second/shared.db",
        )
        with patch.object(backup, "DATABASES", colliding):
            with self.assertRaisesRegex(RuntimeError, "name collision"):
                backup.database_plan()

    def test_first_boot_uses_untouched_legacy_queue_until_isolated_queue_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            isolated = root / "operations/playback-queue.db"
            legacy = root / "audiobooks/playback-queue.db"
            legacy.parent.mkdir(parents=True)
            legacy.touch()
            databases = (isolated,)
            with patch.multiple(
                backup,
                DATA=root,
                AUDIOBOOK_QUEUE=isolated,
                LEGACY_AUDIOBOOK_QUEUE=legacy,
                DATABASES=databases,
            ):
                plan = backup.database_plan()
            self.assertEqual(plan[0][0], legacy)
            self.assertEqual(plan[0][1].as_posix(), "audiobooks/playback-queue.db")


if __name__ == "__main__":
    unittest.main()
