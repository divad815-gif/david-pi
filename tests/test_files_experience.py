import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FilesExperienceContractTest(unittest.TestCase):
    def test_files_exposes_honest_loading_error_and_retry_states(self):
        template = (ROOT / "templates" / "files.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "files.js").read_text(encoding="utf-8")

        for required in ("filesLoading", "filesError", "retryFiles", 'aria-busy="true"'):
            self.assertIn(required, template)
        self.assertIn("loadController.abort()", script)
        self.assertIn("generation!==loadGeneration", script)
        self.assertIn("Your saved files were not changed.", script)

    def test_files_has_keyboard_preview_and_long_name_support(self):
        script = (ROOT / "static" / "files.js").read_text(encoding="utf-8")

        self.assertIn("event.key==='ArrowLeft'", script)
        self.assertIn("event.key==='ArrowRight'", script)
        self.assertIn("title.title=file.name", script)
        self.assertIn("aria-current", script)
        self.assertIn("No matching files.", script)
        self.assertIn("aria-pressed", script)

    def test_files_ui_respects_owner_and_retention_contracts(self):
        script = (ROOT / "static" / "files.js").read_text(encoding="utf-8")

        self.assertIn("if(file.can_edit)", script)
        self.assertIn("version:file.version", script)
        self.assertIn("version:selectedAction.file.version", script)
        self.assertIn("protected backup newer than this trash action", script)
        self.assertIn("disabled=!data.can_add_here", script)


if __name__ == "__main__":
    unittest.main()
