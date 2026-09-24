import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
import hashlib
import tempfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("registry_driver", ROOT / "scripts/promote_staged_beta_registry.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class DraftLookupTest(TestCase):
    def setUp(self):
        self.repo = "example/david-pi"
        self.tag = "v10.0.0-beta.1"
        self.revision = "1" * 40
        self.release_id = 42
        self.url = "https://api.github.com/repos/example/david-pi/releases/42"
        self.view = {"apiUrl": self.url, "tagName": self.tag, "targetCommitish": self.revision}
        self.release = {"id": 42, "url": self.url, "tag_name": self.tag,
                        "target_commitish": self.revision, "draft": True,
                        "prerelease": True, "assets": [{"name": "testing-candidate.tar"}]}

    def lookup(self, view=None, release=None):
        original = Mock(side_effect=[json.dumps(self.view if view is None else view),
                                    json.dumps(self.release if release is None else release)])
        adapted = driver.draft_command(original, self.repo, self.tag, self.revision, 42)
        result = adapted("gh", "api", f"repos/{self.repo}/releases/tags/{self.tag}")
        return json.loads(result), original

    def test_exact_draft_is_read_by_selected_id_with_flags_and_assets_unchanged(self):
        result, original = self.lookup()
        self.assertEqual(result, self.release)
        self.assertEqual(original.call_args_list[-1].args,
                         ("gh", "api", "repos/example/david-pi/releases/42"))

    def test_foreign_url_tag_or_revision_rejected_before_id_request(self):
        for field, value in [("apiUrl", "https://elsewhere.invalid/releases/42"),
                             ("apiUrl", self.url + "?credential=unexpected"),
                             ("apiUrl", self.url.replace("/42", "/43")),
                             ("tagName", "v10.0.0-beta.2"),
                             ("targetCommitish", "2" * 40)]:
            with self.subTest(field=field, value=value):
                original = Mock(return_value=json.dumps({**self.view, field: value}))
                adapted = driver.draft_command(original, self.repo, self.tag, self.revision, 42)
                with self.assertRaisesRegex(ValueError, "Draft discovery differs"):
                    adapted("gh", "api", f"repos/{self.repo}/releases/tags/{self.tag}")
                self.assertEqual(original.call_count, 1)

    def test_id_response_cannot_change_selected_identity(self):
        for field, value in [("id", "42"), ("id", 43), ("url", self.url + "/"),
                             ("tag_name", "other"), ("target_commitish", "main")]:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "Release ID lookup differs"):
                    self.lookup(release={**self.release, field: value})

    def test_other_reads_and_all_mutations_retain_original_dispatch(self):
        original = Mock(return_value="unchanged")
        adapted = driver.draft_command(original, self.repo, self.tag, self.revision, 42)
        for command in [("gh", "api", "repos/example/david-pi/releases/tags/other"),
                        ("gh", "api", "--method", "PATCH", "repos/example/david-pi/releases/42"),
                        ("docker", "push", "--platform", "linux/amd64", "image")]:
            self.assertEqual(adapted(*command, timeout=12), "unchanged")
            original.assert_called_with(*command, timeout=12)

    def test_discovery_failure_is_not_ignored(self):
        original = Mock(side_effect=ValueError("access unavailable"))
        adapted = driver.draft_command(original, self.repo, self.tag, self.revision, 42)
        with self.assertRaisesRegex(ValueError, "access unavailable"):
            adapted("gh", "api", f"repos/{self.repo}/releases/tags/{self.tag}")
        self.assertEqual(original.call_count, 1)


class RegistryBoundaryTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.evidence = self.path / "evidence.json"
        self.evidence.write_text('{"accepted":true}\n')
        self.pins = SimpleNamespace(expected_revision="1" * 40, repository="example/david-pi",
            tag="v10.0.0-beta.1", release_id=42, expected_transfer_sha256="2" * 64,
            candidate_receipt_sha256="3" * 64,
            acceptance_sha256=hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            publisher_revision="4" * 40)
        self.record = {"revision": "1" * 40, "source_sha256": "5" * 64,
            "local_candidate_receipt_sha256": "3" * 64,
            "architectures": {a: {"image": {"manifest_digest": "sha256:" + c * 64}}
                              for a, c in [("amd64", "a"), ("arm64", "b")]}}
        self.args = SimpleNamespace(output=self.path, evidence=self.evidence)

    def test_boundary_emits_truthful_registry_only_receipt_and_stops(self):
        write = Mock()
        with self.assertRaises(driver.RegistryReady):
            driver.registry_boundary(self.args, self.record, {}, "sha256:" + "c" * 64,
                                     self.pins, write)
        destination, receipt = write.call_args.args
        self.assertEqual(destination, self.path / "publication-receipt.json")
        self.assertFalse(receipt["completed"])
        self.assertFalse(receipt["release_exposed"])
        self.assertTrue(receipt["registry_completed"])
        self.assertEqual(receipt["kind"], "testing-registry-transfer")
        self.assertEqual(receipt["platforms"], {"amd64": "sha256:" + "a" * 64,
                                               "arm64": "sha256:" + "b" * 64})
        self.assertEqual(receipt["acceptance_sha256"], self.pins.acceptance_sha256)
        self.assertEqual(receipt["publisher_driver_sha256"],
                         hashlib.sha256(Path(driver.__file__).read_bytes()).hexdigest())

    def test_mismatched_evidence_or_candidate_cannot_emit_success(self):
        for field in ["candidate_receipt_sha256", "acceptance_sha256", "expected_revision"]:
            with self.subTest(field=field):
                pins = SimpleNamespace(**vars(self.pins))
                setattr(pins, field, "0" * 64)
                write = Mock()
                with self.assertRaises(ValueError):
                    driver.registry_boundary(self.args, self.record, {}, "sha256:" + "c" * 64,
                                             pins, write)
                write.assert_not_called()
