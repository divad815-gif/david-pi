import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NotesExperienceContractTest(unittest.TestCase):
    def test_notes_ui_is_read_only_for_visible_nonowners(self):
        script = (ROOT / "static" / "notes.js").read_text(encoding="utf-8")

        self.assertIn("note.can_edit ? 'Saved' : 'Read-only'", script)
        self.assertIn("control.disabled = !note.can_edit", script)
        self.assertIn("noteMenuButton.hidden = !note.can_edit", script)
        self.assertIn("version:target.version", script)

    def test_notes_ui_explains_backup_gated_permanent_deletion(self):
        script = (ROOT / "static" / "notes.js").read_text(encoding="utf-8")

        self.assertIn("confirm:'permanently delete'", script)
        self.assertIn("protected backup newer than this trash action", script)


if __name__ == "__main__":
    unittest.main()
