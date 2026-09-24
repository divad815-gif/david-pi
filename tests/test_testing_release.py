import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import testing_release_gate as beta_gate
import stable_release_gate as stable_gate


def acceptance(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "VERSION").write_text("10.0.0-beta.1\n")
    apk = root / "artifacts/android/david-pi-backup.apk"
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b"unit fixture, not a signed release")
    source = beta_gate.source_digest(root)
    apk_hash = hashlib.sha256(apk.read_bytes()).hexdigest()
    return {
        "schema_version": 1, "kind": "testing-release-acceptance",
        "version": "10.0.0-beta.1", "source_sha256": source,
        "android_apk_sha256": apk_hash, "reviewer": "Unit fixture reviewer",
        "reviewed_at": "2026-09-23T00:00:00Z",
        "known_limitations": ["Physical devices and newcomer testing pending"],
        "pending_acceptance": sorted(beta_gate.PENDING),
        "checks": [
            {
                "id": name, "environment": environment, "status": "pass",
                "source_sha256": source, "implementation": "actual",
                "performed_by": "Unit fixture tester", "completed_at": "2026-09-23T00:00:00Z",
                "notes": "Unit fixture only; not actual acceptance evidence",
                "fixture_scope": "Synthetic household content and recorded test identities",
                "limitations": ["Does not establish physical-device behavior"],
                "evidence_sha256": "a" * 64,
                "actual_services": True, "actual_filesystems": True,
                "default_timings": True, "real_tailscale": True,
                "real_admission_smoke": True,
                "android_apk_sha256": apk_hash,
            }
            for name, environment in beta_gate.REQUIRED.items()
        ],
    }


def test_beta_accepts_recorded_synthetic_content_without_claiming_field_acceptance(tmp_path):
    document = acceptance(tmp_path)
    assert beta_gate.validate(document, tmp_path) == []
    assert beta_gate.PENDING.isdisjoint(beta_gate.REQUIRED)
    assert not any(check["environment"].startswith("physical") for check in document["checks"])


@pytest.mark.parametrize("field,value", [
    ("status", "failed"), ("environment", "container"), ("implementation", "mocked"),
    ("actual_services", False), ("actual_filesystems", False), ("default_timings", False),
    ("real_tailscale", False), ("fixture_scope", ""), ("limitations", None),
    ("evidence_sha256", ""), ("source_sha256", "0" * 64),
])
def test_beta_refuses_missing_or_substituted_technical_checks(tmp_path, field, value):
    document = acceptance(tmp_path)
    document["checks"][0][field] = value
    assert beta_gate.validate(document, tmp_path)


@pytest.mark.parametrize("change", ["missing", "duplicate", "extra", "pending", "limitations"])
def test_beta_cannot_hide_required_work_or_disclosures(tmp_path, change):
    document = acceptance(tmp_path)
    if change == "missing":
        document["checks"].pop()
    elif change == "duplicate":
        document["checks"][-1] = copy.deepcopy(document["checks"][0])
    elif change == "extra":
        document["checks"].append({"id": "unverified-success"})
    elif change == "pending":
        document["pending_acceptance"] = []
    else:
        document["known_limitations"] = []
    assert beta_gate.validate(document, tmp_path)


def test_beta_requires_exact_source_and_apk_for_emulator_and_release(tmp_path):
    document = acceptance(tmp_path)
    document["checks"][-1]["android_apk_sha256"] = "f" * 64
    assert any("same signed APK" in error for error in beta_gate.validate(document, tmp_path))
    document["checks"][-1]["android_apk_sha256"] = document["android_apk_sha256"]
    (tmp_path / "artifacts/android/david-pi-backup.apk").write_bytes(b"different APK")
    assert any("Android artifact differs" in error for error in beta_gate.validate(document, tmp_path))
    (tmp_path / "new_source.py").write_text("changed = True\n")
    assert any("source" in error for error in beta_gate.validate(document, tmp_path))


def test_fixture_household_isolation_requires_separate_real_admission_smoke(tmp_path):
    document = acceptance(tmp_path)
    check = next(item for item in document["checks"] if item["id"] == "household-admission-isolation")
    check["real_admission_smoke"] = False
    assert any("real admission smoke" in error for error in beta_gate.validate(document, tmp_path))


@pytest.mark.parametrize("version", ["10.0.0", "10.0.0-rc.1", "10.0.0-beta.0", "010.0.0-beta.1", "10.0.0-beta.1+build"])
def test_testing_gate_is_only_for_an_explicit_canonical_beta(tmp_path, version):
    document = acceptance(tmp_path)
    (tmp_path / "VERSION").write_text(version)
    document["version"] = version
    assert any("testing publication requires" in error for error in beta_gate.validate(document, tmp_path))


def test_stable_gate_still_requires_physical_and_newcomer_receipts(tmp_path):
    document = acceptance(tmp_path)
    assert any("stable semantic version" in error for error in stable_gate.validate(document, tmp_path))
    (tmp_path / "VERSION").write_text("10.0.0\n")
    document["version"] = "10.0.0"
    errors = stable_gate.validate(document, tmp_path)
    for name in beta_gate.PENDING:
        assert any(error.startswith(name + ":") for error in errors)


@pytest.mark.parametrize("version,selected", [("10.0.0", "latest"), ("10.0.0-beta.1", "10.0.0-beta.1")])
def test_metadata_pins_beta_bootstrap_but_keeps_stable_latest(tmp_path, version, selected):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(ROOT / "scripts/build-release-metadata.sh", scripts)
    (tmp_path / "VERSION").write_text(version + "\n")
    (tmp_path / "install.sh").write_text('REPOSITORY="__GITHUB_REPOSITORY__"\nRELEASE_VERSION="${DAVID_PI_VERSION:-__RELEASE_VERSION__}"\n')
    output = tmp_path / "dist"
    output.mkdir()
    archive = output / f"david-pi-{version}.tar.gz"
    archive.write_bytes(b"source archive fixture")
    result = subprocess.run(["bash", str(scripts / "build-release-metadata.sh")],
                            env={**os.environ, "GITHUB_REPOSITORY": "Example/david-pi", "DAVID_PI_IMAGE_DIGEST": "sha256:" + "b" * 64},
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    rendered = (output / "install.sh").read_text()
    assert "__RELEASE_VERSION__" not in rendered
    assert f'RELEASE_VERSION="${{DAVID_PI_VERSION:-{selected}}}"' in rendered
    manifest = dict(line.split("=", 1) for line in (output / "release-manifest.txt").read_text().splitlines())
    assert manifest["VERSION"] == version
    assert manifest["ARCHIVE_SHA256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert manifest["IMAGE"] == "ghcr.io/example/david-pi@sha256:" + "b" * 64
    subprocess.run(["sha256sum", "-c", "install.sh.sha256"], cwd=output, check=True, capture_output=True)
