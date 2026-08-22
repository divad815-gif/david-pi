import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeviceBackupUiContractTest(unittest.TestCase):
    def test_iphone_pairing_hides_android_only_qr_but_shows_shortcut_link(self):
        script = (ROOT / "static" / "device-backup.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "platform.css").read_text(encoding="utf-8")

        self.assertIn('const androidPairing = platform === "android";', script)
        self.assertIn("link.hidden = false;", script)
        self.assertIn("qr.hidden = !androidPairing;", script)
        self.assertIn(".pairing-qr[hidden],.pairing-open[hidden]{display:none!important}", styles)

    def test_pairing_assets_are_cache_busted(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        self.assertIn('/static/platform.css?v=17', template)
        self.assertIn('/static/device-backup.js?v=11', template)

    def test_removed_devices_are_not_presented_as_revoked_cards(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "device-backup.js").read_text(encoding="utf-8")
        self.assertIn("Remove phone", template)
        self.assertNotIn(">Revoked<", template)
        self.assertIn("Existing media stays safe", script)

    def test_iphone_setup_uses_apple_shared_master_and_one_time_pairing(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "device-backup.js").read_text(encoding="utf-8")
        for required in (
            "One Apple-shared Shortcut", "ios_shortcut_icloud_url",
            "Install the iPhone Shortcut", "Generate code and pair",
            "Back Up to David-Pi", "upload-only access",
        ):
            self.assertIn(required, template)
        self.assertNotIn("/api/ios-backup/shortcut", template)
        self.assertNotIn("/api/ios-backup/credential-file", template)
        self.assertNotIn("Download fallback credential", template)
        self.assertIn('if (platform === "ios")', script)
        self.assertIn('createPairing("ios"', script)
        self.assertIn("shortcuts://run-shortcut?name=Back%20Up%20to%20David-Pi", script)
        self.assertIn("encodeURIComponent(data.manual_code)", script)
        self.assertNotIn("request.url_root", template)

    def test_iphone_guide_contains_full_and_incremental_backup_workflow(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        for required in (
            "initial library moves in 200-item batches",
            "server checkpoint prevents completed items from being resent",
            "Approve <strong>All Photos</strong>",
            "schedule incremental runs",
            "Diana must generate her own pairing code",
        ):
            self.assertIn(required, template)
        self.assertIn("publish the master Shortcut once", template)


if __name__ == "__main__":
    unittest.main()
