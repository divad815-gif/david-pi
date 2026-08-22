import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "david_pi_safe_shutdown.py"
SPEC = importlib.util.spec_from_file_location("david_pi_safe_shutdown", SOURCE)
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)


class SafeShutdownBrokerTests(unittest.TestCase):
    def make_request(self, root, *, age=0, mode=0o600, extra=None):
        root = Path(root)
        sentinel = root / ".david-pi-storage"
        request_path = root / "shutdown.request"
        sentinel.write_text(broker.EXPECTED_DATA_ID, encoding="utf-8")
        payload = {
            "action": "poweroff",
            "request_id": "a" * 32,
            "requested_at": int(time.time()) - age,
            "version": 1,
        }
        if extra:
            payload.update(extra)
        request_path.write_text(json.dumps(payload), encoding="utf-8")
        request_path.chmod(mode)
        return sentinel, request_path

    def test_accepts_only_fresh_fixed_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel, request_path = self.make_request(temporary)
            with patch.object(broker, "MOUNTPOINT", Path(temporary)), \
                 patch.object(broker, "SENTINEL", sentinel), \
                 patch.object(broker, "REQUEST_PATH", request_path), \
                 patch.object(broker, "EXPECTED_UID", request_path.stat().st_uid), \
                 patch("os.path.ismount", return_value=True), \
                 patch.object(broker.stat, "S_IMODE", return_value=0o600):
                result = broker.validate_request()
            self.assertEqual(result["action"], "poweroff")

    def test_rejects_stale_broad_or_symlinked_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel, request_path = self.make_request(temporary, age=broker.MAX_AGE_SECONDS + 1)
            common = (
                patch.object(broker, "MOUNTPOINT", Path(temporary)),
                patch.object(broker, "SENTINEL", sentinel),
                patch.object(broker, "REQUEST_PATH", request_path),
                patch.object(broker, "EXPECTED_UID", request_path.stat().st_uid),
                patch("os.path.ismount", return_value=True),
                patch.object(broker.stat, "S_IMODE", return_value=0o600),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5]:
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    broker.validate_request()
            request_path.unlink()
            target = Path(temporary) / "target"
            target.write_text("{}", encoding="utf-8")
            try:
                request_path.symlink_to(target)
            except OSError:
                # Windows commonly denies symlink creation; the Linux deployment
                # runs this branch in the complete container test suite.
                return
            with patch.object(broker, "MOUNTPOINT", Path(temporary)), \
                 patch.object(broker, "SENTINEL", sentinel), \
                 patch.object(broker, "REQUEST_PATH", request_path), \
                 patch.object(broker, "EXPECTED_UID", request_path.lstat().st_uid), \
                 patch("os.path.ismount", return_value=True), \
                 patch.object(broker.stat, "S_IMODE", return_value=0o600):
                with self.assertRaisesRegex(RuntimeError, "regular file"):
                    broker.validate_request()


if __name__ == "__main__":
    unittest.main()
