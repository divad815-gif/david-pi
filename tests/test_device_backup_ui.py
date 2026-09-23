import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeviceBackupUiContractTest(unittest.TestCase):
    def test_v1_does_not_offer_or_register_iphone_backup(self):
        template = (ROOT / "templates/device_backup.html").read_text()
        script = (ROOT / "static/device-backup.js").read_text()
        route = (ROOT / "modules/device_backup.py").read_text()
        self.assertNotIn('data-platform="ios"', template)
        self.assertNotIn("shortcuts://", script)
        self.assertNotRegex(route, r'@blueprint\.(get|post)\("/api/(v1/)?ios-backup/')

    def test_pairing_assets_are_cache_busted(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        self.assertIn('/static/platform.css?v=25', template)
        self.assertIn('/static/device-backup.js?v=16', template)

    def test_pairing_copy_contract_uses_only_the_canonical_origin(self):
        script = (ROOT / "static" / "device-backup.js").read_text(encoding="utf-8")
        self.assertIn(
            'meta[name="paired-server-origin"]',
            script,
        )
        self.assertIn("lastPairing = {server: data.server_url", script)
        self.assertNotIn("lastPairing = {server: location.origin", script)
        self.assertNotIn("`${location.origin}${button.dataset.copyEndpoint}`", script)

    def test_android_download_is_presented_only_as_a_verified_release(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        route = (ROOT / "modules" / "device_backup.py").read_text(encoding="utf-8")
        self.assertIn("Verified signed release", template)
        self.assertIn("signed release could not be verified", template)
        self.assertIn("available_android_release", route)
        self.assertNotIn('apk_version="', route)

    def test_removed_devices_are_not_presented_as_revoked_cards(self):
        template = (ROOT / "templates" / "device_backup.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "device-backup.js").read_text(encoding="utf-8")
        self.assertIn("Remove phone", template)
        self.assertNotIn(">Revoked<", template)
        self.assertIn("Existing photos and videos stay unchanged", template)

    def test_manual_and_periodic_android_work_both_run_reconciliation(self):
        worker = (
            ROOT
            / "clients"
            / "android"
            / "app"
            / "src"
            / "main"
            / "java"
            / "com"
            / "davidpi"
            / "backup"
            / "work"
            / "BackupWorker.kt"
        ).read_text(encoding="utf-8")

        # Both schedules instantiate the same worker, and reconciliation is an
        # unconditional part of that worker's serialized scan path.
        self.assertIn("OneTimeWorkRequestBuilder<BackupWorker>()", worker)
        self.assertIn("PeriodicWorkRequestBuilder<BackupWorker>(", worker)
        self.assertIn("api.reconcile(envelope)", worker)
        self.assertIn("PERIODIC_WORK, ExistingPeriodicWorkPolicy.UPDATE, request", worker)

    def test_android_native_network_and_web_bridges_are_origin_bound(self):
        android = ROOT / "clients" / "android" / "app" / "src" / "main" / "java"
        portal = (android / "com/davidpi/backup/PortalShell.kt").read_text(encoding="utf-8")
        api = (android / "com/davidpi/backup/net/BackupApi.kt").read_text(encoding="utf-8")
        offline = (android / "com/davidpi/backup/offline/OfflineDownloadWorker.kt").read_text(
            encoding="utf-8"
        )
        download = (android / "com/davidpi/backup/net/PortalDownloadWorker.kt").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("addJavascriptInterface", portal)
        self.assertIn("WebViewCompat.addWebMessageListener", portal)
        self.assertIn("setOf(DavidPiOrigin.ORIGIN)", portal)
        self.assertIn("!isMainFrame", portal)
        self.assertIn("DavidPiOrigin.canonicalPairingOrigin(sourceOrigin.toString())", portal)
        self.assertIn("override fun onPageStarted", portal)
        self.assertIn("DavidPiHttp.noRedirects(client)", api)
        self.assertIn("DavidPiHttp.forSession(OkHttpClient.Builder().build())", offline)
        self.assertIn("DavidPiHttp.forSession(", download)
        self.assertNotIn('header("Cookie"', offline)
        self.assertNotIn("DownloadManager.Request", portal)


if __name__ == "__main__":
    unittest.main()
