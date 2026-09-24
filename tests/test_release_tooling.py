import importlib.util
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import base_image_provenance
import promotion_manifest
import release_contract
import rollback_metadata
import vulnerability_scan
from resolve_candidate_image import resolve_candidate, verify_candidate
from modules.android_release import verified_android_release


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "generate_sbom.py"
WORKFLOW = ROOT / ".github" / "workflows" / "verify.yml"
SPEC = importlib.util.spec_from_file_location("generate_sbom", SOURCE)
sbom = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sbom)


@pytest.fixture(autouse=True)
def _stub_candidate_android_bundle_inspection(monkeypatch):
    """Promotion contract tests use their host fixture as the candidate filesystem."""
    monkeypatch.setattr(
        promotion_manifest,
        "verified_candidate_android_release",
        lambda _candidate, source_root: verified_android_release(
            source_root, require_source=True, require_tools=False
        ),
    )


def test_sbom_is_deterministic_and_marks_direct_dependencies():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        requirements = root / "requirements.txt"
        requirements.write_text("Flask==3.1.3\nqrcode[pil]==8.2\n", encoding="utf-8")
        packages = root / "python.json"
        packages.write_text(json.dumps([
            {"name": "Werkzeug", "version": "3.1.0"},
            {"name": "Flask", "version": "3.1.3"},
            {"name": "qrcode", "version": "8.2"},
        ]), encoding="utf-8")
        operating_system = root / "os.tsv"
        operating_system.write_text("ffmpeg\t7:5.1.6-0+deb12u1\tarm64\n", encoding="utf-8")

        direct = sbom.direct_requirements(requirements)
        components = sbom.python_components(packages, direct)
        components.extend(sbom.debian_components(operating_system))
        first = sbom.build_document(components, "example@sha256:abc", "deadbeef")
        second = sbom.build_document(list(reversed(components)), "example@sha256:abc", "deadbeef")

    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["specVersion"] == "1.5"
    assert first["serialNumber"].startswith("urn:uuid:")
    by_name = {component["name"]: component for component in first["components"]}
    assert by_name["flask"]["properties"][0]["value"] == "direct"
    assert by_name["qrcode"]["properties"][0]["value"] == "direct"
    assert "properties" not in by_name["werkzeug"]
    assert by_name["ffmpeg"]["purl"].startswith("pkg:deb/debian/ffmpeg@")


def test_sbom_rejects_malformed_inventory_rows():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "bad.tsv"
        path.write_text("package-without-version\n", encoding="utf-8")
        try:
            sbom.debian_components(path)
        except ValueError as error:
            assert "invalid entry" in str(error)
        else:
            raise AssertionError("malformed inventory was accepted")


def test_sbom_emits_alpine_package_urls():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "apk.tsv"
        path.write_text("ffmpeg\t7.1.2-r3\taarch64\nalpine-baselayout\t3.7.0-r0\taarch64\n")
        components = sbom.os_components(path, "apk")
    by_name = {item["name"]: item for item in components}
    assert by_name["ffmpeg"]["purl"] == "pkg:apk/alpine/ffmpeg@7.1.2-r3?arch=aarch64"
    assert by_name["alpine-baselayout"]["properties"] == [
        {"name": "david-pi:package-architecture", "value": "aarch64"}
    ]


def test_ci_never_publishes_before_stable_acceptance():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    assert "workflow_dispatch:" in workflow
    assert "environment: stable-release" in workflow
    assert "scripts/stable_release_gate.py" in workflow
    assert workflow.index("Require completed acceptance") < workflow.index("docker/login-action")
    assert "linux/amd64,linux/arm64" in workflow
    assert "importlib.metadata" in workflow
    assert "-m pip list" not in workflow
    assert "artifacts/android/david-pi-backup.apk" in workflow
    assert "--prerelease" not in workflow


def test_release_guide_names_both_candidate_only_rollback_workers():
    guide = (ROOT / "deploy" / "RELEASE_PROMOTION.md").read_text(encoding="utf-8")
    assert "candidate override" in guide and "starts all six\nservices" in guide
    assert "stop `device-backup-worker` and `slideshow-worker`" in guide
    assert "device uploads, secondary-copy state" in guide
    assert "six-writer readiness gate" in guide


def _candidate_builder_metadata():
    return {
        "containerimage.digest": f"sha256:{'b' * 64}",
        "containerimage.config.digest": f"sha256:{'c' * 64}",
        "containerimage.descriptor": {"digest": f"sha256:{'b' * 64}"},
    }


def _candidate_inspection(repository, revision):
    return {
        "Id": f"sha256:{'c' * 64}",
        "Os": "linux",
        "Architecture": "arm64",
        "RepoDigests": [f"{repository}@sha256:{'b' * 64}"],
        "RootFS": {"Layers": [f"sha256:{'d' * 64}"]},
        "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
    }


def _mock_candidate_inspect(monkeypatch, value):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps([value]), ""
        ),
    )


def test_published_candidate_resolution_uses_builder_returned_digest(tmp_path, monkeypatch):
    repository = "ghcr.io/example/david-pi"
    revision = "a" * 40
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(_candidate_builder_metadata()), encoding="utf-8")
    _mock_candidate_inspect(monkeypatch, _candidate_inspection(repository, revision))
    assert resolve_candidate(
        metadata=metadata,
        repository=repository,
    ) == f"{repository}@sha256:{'b' * 64}"
    verify_candidate(
        metadata=metadata,
        candidate=f"{repository}@sha256:{'b' * 64}",
        revision=revision,
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("Architecture", "amd64", "linux/arm64"),
        ("RepoDigests", [], "exact repository digest"),
    ],
)
def test_published_candidate_verification_fails_closed(
    tmp_path, monkeypatch, field, value, message
):
    repository = "ghcr.io/example/david-pi"
    revision = "a" * 40
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(_candidate_builder_metadata()), encoding="utf-8")
    inspected = _candidate_inspection(repository, revision)
    inspected[field] = value
    _mock_candidate_inspect(monkeypatch, inspected)
    with pytest.raises(release_contract.ContractError, match=message):
        verify_candidate(
            metadata=metadata,
            candidate=f"{repository}@sha256:{'b' * 64}",
            revision=revision,
        )


def test_published_candidate_verification_rejects_wrong_revision_or_config(
    tmp_path, monkeypatch
):
    repository = "ghcr.io/example/david-pi"
    revision = "a" * 40
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(_candidate_builder_metadata()), encoding="utf-8")
    inspected = _candidate_inspection(repository, "0" * 40)
    _mock_candidate_inspect(monkeypatch, inspected)
    with pytest.raises(release_contract.ContractError, match="revision"):
        verify_candidate(
            metadata=metadata,
            candidate=f"{repository}@sha256:{'b' * 64}",
            revision=revision,
        )
    inspected = _candidate_inspection(repository, revision)
    inspected["Id"] = f"sha256:{'e' * 64}"
    _mock_candidate_inspect(monkeypatch, inspected)
    with pytest.raises(release_contract.ContractError, match="config differs"):
        verify_candidate(
            metadata=metadata,
            candidate=f"{repository}@sha256:{'b' * 64}",
            revision=revision,
        )


def test_candidate_resolution_rejects_ambiguous_builder_metadata(tmp_path):
    metadata = tmp_path / "metadata.json"
    value = _candidate_builder_metadata()
    value["containerimage.descriptor"]["digest"] = f"sha256:{'f' * 64}"
    metadata.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(release_contract.ContractError, match="descriptor differs"):
        resolve_candidate(metadata=metadata, repository="ghcr.io/example/david-pi")


def test_image_gate_exercises_the_alpine_renameat2_fallback():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "DAVID_PI_EXPECT_RENAMEAT2_ARCH" in makefile
    assert "audiobook_cleanup_uses_kernel_renameat2" in makefile
    assert "audiobook_cleanup_syscall_collision_preserves_both_files" in makefile
    assert "audiobook_cleanup_unsupported_platform_never_calls_raw_syscall" in makefile
    assert "audiobook_cleanup_release_image_matches_expected_abi" in makefile
    assert "!tests/test_app.py" in dockerignore


def _ref(name, character):
    return f"{name}@sha256:{character * 64}"


def _write_legacy_readiness(root):
    readiness = root / "deploy" / "david-pi-legacy-writer-readiness"
    readiness.parent.mkdir(parents=True, exist_ok=True)
    readiness.write_text("#!/usr/bin/env python3\n# fixture readiness gate\n", encoding="utf-8")
    return readiness


def _raw_scans(candidate, high=False):
    finding = []
    if high:
        finding = [{
            "VulnerabilityID": "CVE-2099-0001",
            "PkgName": "unsafe",
            "InstalledVersion": "1.0",
            "FixedVersion": "1.1",
            "Severity": "HIGH",
            "Status": "fixed",
        }]
    dependencies = {
        "SchemaVersion": 2,
        "ArtifactName": "requirements.txt",
        "Results": [{
            "Target": "requirements.txt", "Class": "lang-pkgs", "Type": "pip",
            "Packages": [{"Name": "flask", "Version": "3.1.3"}],
            "Vulnerabilities": finding,
        }],
    }
    image = {
        "SchemaVersion": 2,
        "ArtifactName": candidate,
        "Metadata": {
            "ImageID": f"sha256:{'e' * 64}",
            "RepoDigests": [candidate],
        },
        "Results": [{
            "Target": "debian", "Class": "os-pkgs", "Type": "debian",
            "Packages": [{"Name": "base-files", "Version": "1"}],
            "Vulnerabilities": [],
        }],
    }
    return dependencies, image


def test_digest_contract_rejects_mutable_or_ambiguous_images():
    candidate = _ref("registry.invalid/david-pi", "a")
    assert release_contract.image_digest(candidate) == f"sha256:{'a' * 64}"
    assert release_contract.image_repository(candidate) == "registry.invalid/david-pi"
    assert release_contract.is_digest_reference(
        _ref("registry.example:5000/team/david--pi", "b")
    )
    for invalid in (
        "registry.invalid/david-pi:latest",
        "registry.invalid/david-pi@sha256:abc",
        f" registry.invalid/david-pi@sha256:{'a' * 64}",
        f"registry.invalid/david-pi@sha256:{'A' * 64}",
        f"registry.invalid/`id`@sha256:{'a' * 64}",
        f"registry.invalid/$IMAGE@sha256:{'a' * 64}",
        f"registry.invalid/david-pi:tag@sha256:{'a' * 64}",
    ):
        try:
            release_contract.image_digest(invalid)
        except release_contract.ContractError:
            pass
        else:
            raise AssertionError(f"mutable/ambiguous image was accepted: {invalid}")


def test_scan_normalization_is_deterministic_and_fails_empty_coverage():
    candidate = _ref("registry.invalid/david-pi", "a")
    dependencies, _ = _raw_scans(candidate, high=True)
    summary = vulnerability_scan.summarize_scan(
        dependencies, "dependencies", "requirements.txt"
    )
    assert summary["blocking_finding_count"] == 1
    assert summary["findings"][0]["id"] == "CVE-2099-0001"
    empty = {"SchemaVersion": 2, "ArtifactName": "requirements.txt", "Results": []}
    try:
        vulnerability_scan.summarize_scan(empty, "dependencies", "requirements.txt")
    except vulnerability_scan.ScanError as error:
        assert error.code == "dependencies-coverage-empty"
        assert "false-success" in str(error)
    else:
        raise AssertionError("empty successful scanner output was accepted")


def test_unavailable_scanner_writes_machine_readable_failure():
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "scan.json"
        result = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "vulnerability_scan.py"),
            "--scanner", str(Path(temporary) / "missing-trivy"),
            "--image", "david-family-photos:test",
            "--output", str(output),
        ], cwd=ROOT, text=True, capture_output=True)
        evidence = json.loads(output.read_text(encoding="utf-8"))
    assert result.returncode == 2
    assert evidence["status"] == "error"
    assert evidence["error"]["code"] == "scanner-unavailable"


def test_scanner_identity_binds_vulnerability_database_metadata():
    with tempfile.TemporaryDirectory() as temporary:
        scanner = Path(temporary) / "trivy"
        scanner.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'Version: 0.74.0' 'Vulnerability DB:' "
            "'  Version: 2' '  UpdatedAt: 2099-01-01T00:00:00Z' "
            "'  DownloadedAt: 2099-01-01T00:01:00Z'\n",
            encoding="utf-8",
        )
        scanner.chmod(0o755)
        identity = vulnerability_scan.scanner_identity(str(scanner), cwd=ROOT)
    assert identity["version"] == "0.74.0"
    assert len(identity["executable_sha256"]) == 64
    assert len(identity["version_output_sha256"]) == 64
    assert identity["vulnerability_database"]["schema_version"] == "2"
    assert identity["vulnerability_database"]["updated_at"].startswith("2099-")


def test_scanner_identity_rejects_a_different_trivy_version():
    with tempfile.TemporaryDirectory() as temporary:
        scanner = Path(temporary) / "trivy"
        scanner.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' 'Version: 0.73.0' 'Vulnerability DB:' "
            "'  Version: 2' '  UpdatedAt: 2099-01-01T00:00:00Z'\n",
            encoding="utf-8",
        )
        scanner.chmod(0o755)
        try:
            vulnerability_scan.scanner_identity(str(scanner), cwd=ROOT)
        except vulnerability_scan.ScanError as error:
            assert error.code == "scanner-version-mismatch"
        else:
            raise AssertionError("an unapproved scanner version was accepted")


def test_base_provenance_requires_matching_local_digest_and_build_contract():
    reference = _ref("python", "c")
    runtime_contract = {
        "python_series": "3.13", "python_version": "3.13.7",
        "distribution": "alpine", "alpine_series": "3.22",
        "alpine_version": "3.22.2", "libc": "musl",
        "musl_loader": "/lib/ld-musl-aarch64.so.1",
    }
    with tempfile.TemporaryDirectory() as temporary:
        dockerfile = Path(temporary) / "Dockerfile"
        dockerfile.write_text(
            "ARG PYTHON_BASE_IMAGE=python:3.13-slim\n"
            "FROM ${PYTHON_BASE_IMAGE} AS runtime-base\n",
            encoding="utf-8",
        )
        inspected = {
            "Id": f"sha256:{'f' * 64}", "RepoDigests": [reference],
            "Architecture": "arm64", "Os": "linux",
            "RootFS": {"Layers": [f"sha256:{'1' * 64}"]},
        }
        evidence = base_image_provenance.build_evidence(
            reference, inspected, dockerfile, runtime_contract
        )
        assert evidence["status"] == "pass"
        wrong_architecture = dict(inspected, Architecture="amd64")
        try:
            base_image_provenance.build_evidence(
                reference, wrong_architecture, dockerfile, runtime_contract
            )
        except release_contract.ContractError as error:
            assert any(message in str(error) for message in ("architectures differ", "linux/amd64 or linux/arm64", "runtime contract"))
        else:
            raise AssertionError("an AMD64 production base was accepted")
        inspected["RepoDigests"] = [_ref("python", "d")]
        try:
            base_image_provenance.build_evidence(
                reference, inspected, dockerfile, runtime_contract
            )
        except release_contract.ContractError as error:
            assert "does not match" in str(error)
        else:
            raise AssertionError("wrong local base digest was accepted")

        inspected["RepoDigests"] = [_ref("evil.invalid/python", "c")]
        with pytest.raises(release_contract.ContractError, match="Official Image"):
            base_image_provenance.build_evidence(
                _ref("evil.invalid/python", "c"), inspected, dockerfile,
                runtime_contract,
            )
        wrong_runtime = dict(runtime_contract, alpine_version="3.21.9")
        inspected["RepoDigests"] = [reference]
        with pytest.raises(release_contract.ContractError, match="Python 3.13"):
            base_image_provenance.build_evidence(
                reference, inspected, dockerfile, wrong_runtime
            )


def _write_base_rootfs(path, alpine_version="3.22.2"):
    with tarfile.open(path, "w") as archive:
        release = alpine_version.encode("ascii") + b"\n"
        release_info = tarfile.TarInfo("etc/alpine-release")
        release_info.size = len(release)
        archive.addfile(release_info, io.BytesIO(release))
        musl = tarfile.TarInfo("lib/ld-musl-aarch64.so.1")
        musl.type = tarfile.SYMTYPE
        musl.linkname = "libc.musl-aarch64.so.1"
        archive.addfile(musl)
        python = tarfile.TarInfo("usr/local/bin/python3.13")
        python.type = tarfile.SYMTYPE
        python.linkname = "python3"
        archive.addfile(python)


def test_base_rootfs_contract_proves_python_alpine_and_musl(tmp_path):
    archive = tmp_path / "rootfs.tar"
    _write_base_rootfs(archive)
    inspected = {"Architecture": "arm64", "Config": {"Env": ["PATH=/usr/bin", "PYTHON_VERSION=3.13.7"]}}
    contract = base_image_provenance._runtime_contract_from_archive(inspected, archive)
    assert contract["python_series"] == "3.13"
    assert contract["alpine_series"] == "3.22"
    assert contract["libc"] == "musl"


@pytest.mark.parametrize(
    ("python_version", "alpine_version", "message"),
    [
        ("3.12.11", "3.22.2", "Python 3.13"),
        ("3.13.7", "3.21.9", "Alpine 3.22"),
    ],
)
def test_base_rootfs_contract_rejects_wrong_runtime(
    tmp_path, python_version, alpine_version, message
):
    archive = tmp_path / "rootfs.tar"
    _write_base_rootfs(archive, alpine_version)
    inspected = {"Architecture": "arm64", "Config": {"Env": [f"PYTHON_VERSION={python_version}"]}}
    with pytest.raises(release_contract.ContractError, match=message):
        base_image_provenance._runtime_contract_from_archive(inspected, archive)


def test_default_runtime_base_uses_current_alpine_packages():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert dockerfile.startswith("ARG PYTHON_BASE_IMAGE=python:3.13-alpine3.22\n")
    assert "ARG PIP_VERSION=26.2.1" in dockerfile
    assert "RUN apk upgrade --no-cache" in dockerfile
    assert "apk add --no-cache ffmpeg poppler-utils" in dockerfile
    assert 'python -m pip install --no-cache-dir --upgrade "pip==${PIP_VERSION}"' in dockerfile
    assert "python -m pip install --no-cache-dir -r requirements.txt" in dockerfile
    assert "apt-get" not in dockerfile
    assert "BASE_IMAGE ?= python:3.13-alpine3.22" in makefile
    assert "dpkg-query" not in makefile
    assert makefile.count("/lib/apk/db/installed") == 2
    assert makefile.count("--os-package-type apk") == 2


def test_rollback_commands_reference_generated_overrides_from_source_root():
    candidate = _ref("registry.invalid/david-pi", "a")
    previous = _ref("registry.invalid/david-pi", "b")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        build = root / "build"
        compose = root / "compose.yaml"
        compose.write_text(
            "services:\n"
            "  photo-portal:\n    image: david-family-photos:old\n"
            "  chat-notifier:\n    image: david-family-photos:old\n"
            "  audiobook-preparer:\n    image: david-family-photos:old\n"
            "  slideshow-worker:\n    image: david-family-photos:old\n"
            "  david-pi-maintenance:\n    image: david-family-photos:old\n"
            "  device-backup-worker:\n    image: david-family-photos:old\n",
            encoding="utf-8",
        )
        readiness = _write_legacy_readiness(root)
        metadata, candidate_value, rollback_value = rollback_metadata.build_metadata(
            candidate=candidate, previous=previous, revision="d" * 40,
            compose=compose, services=[
                "photo-portal", "chat-notifier", "audiobook-preparer",
                "slideshow-worker", "david-pi-maintenance",
                "device-backup-worker",
            ],
            candidate_override=build / "candidate.json",
            rollback_override=build / "rollback.json",
            legacy_readiness=readiness,
            candidate_identity={
                "image_id": f"sha256:{'e' * 64}",
                "architecture": "arm64", "os": "linux",
            },
            previous_identity={
                "image_id": f"sha256:{'f' * 64}",
                "architecture": "arm64", "os": "linux",
            },
        )
    assert metadata["overrides"]["candidate"]["path"] == "candidate.json"
    assert metadata["overrides"]["candidate"]["command_path"] == "build/candidate.json"
    assert "build/candidate.json" in metadata["commands"]["promote"]
    assert metadata["rollback_profile"]["rollback_services"] == [
        "audiobook-preparer", "chat-notifier", "david-pi-maintenance", "photo-portal"
    ]
    assert metadata["commands"]["rollback_sequence"] == [
        "stop_for_rollback", "rollback", "verify_rollback"
    ]
    assert metadata["commands"]["stop_for_rollback"][-2:] == [
        "device-backup-worker", "slideshow-worker"
    ]
    assert candidate_value["services"]["slideshow-worker"] == {"image": candidate}
    assert rollback_value["services"]["slideshow-worker"] == {
        "image": candidate, "profiles": ["rollback-disabled"]
    }
    assert rollback_value["services"]["device-backup-worker"] == {
        "image": candidate, "profiles": ["rollback-disabled"]
    }
    assert metadata["rollback_profile"]["stopped_services"] == [
        "device-backup-worker", "slideshow-worker"
    ]
    assert all(
        service["rollback_image"] is None
        for service in metadata["services"]
        if service["name"] in {"device-backup-worker", "slideshow-worker"}
    )


def test_first_rollout_contract_rejects_an_incomplete_writer_set():
    candidate = _ref("registry.invalid/david-pi", "a")
    previous = _ref("registry.invalid/david-pi", "b")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        compose = root / "compose.yaml"
        compose.write_text(
            "services:\n"
            "  photo-portal:\n    image: old\n"
            "  slideshow-worker:\n    image: old\n",
            encoding="utf-8",
        )
        readiness = _write_legacy_readiness(root)
        with pytest.raises(release_contract.ContractError, match="six-writer"):
            rollback_metadata.build_metadata(
                candidate=candidate, previous=previous, revision="d" * 40,
                compose=compose, services=["photo-portal", "slideshow-worker"],
                candidate_override=root / "candidate.json",
                rollback_override=root / "rollback.json",
                legacy_readiness=readiness,
                candidate_identity={"architecture": "arm64", "os": "linux"},
                previous_identity={"architecture": "arm64", "os": "linux"},
            )


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is unavailable")
def test_rollback_compose_profile_omits_worker_from_active_default_model():
    compose_plugin = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    if compose_plugin.returncode:
        pytest.skip("Docker Compose plugin is unavailable")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        compose = root / "compose.yaml"
        services = [
            "photo-portal", "chat-notifier", "audiobook-preparer",
            "slideshow-worker", "david-pi-maintenance", "device-backup-worker",
        ]
        compose.write_text(
            "services:\n" + "".join(
                f"  {name}:\n    image: old:latest\n" for name in services
            ),
            encoding="utf-8",
        )
        rollback_override = root / "rollback.json"
        release_contract.write_json_atomic(rollback_override, {
            "services": {
                **{
                    name: {"image": "old:previous"}
                    for name in services
                    if name not in {"device-backup-worker", "slideshow-worker"}
                },
                **{
                    name: {
                        "image": "new:candidate",
                        "profiles": ["rollback-disabled"],
                    }
                    for name in {"device-backup-worker", "slideshow-worker"}
                },
            }
        })
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(compose), "-f", str(rollback_override),
                "config", "--services",
            ],
            capture_output=True, text=True, check=False,
        )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == set(services) - {
        "device-backup-worker", "slideshow-worker"
    }


def _write_promotion_fixture(root, *, raw_high=False):
    candidate = _ref("registry.invalid/david-pi", "a")
    previous = _ref("registry.invalid/david-pi", "b")
    base = _ref("python", "c")
    revision = "d" * 40
    compose = root / "compose.yaml"
    compose.write_text(
        "services:\n  photo-portal:\n    image: david-family-photos:old\n"
        "  chat-notifier:\n    image: david-family-photos:old\n"
        "  audiobook-preparer:\n    image: david-family-photos:old\n"
        "  slideshow-worker:\n    image: david-family-photos:old\n"
        "  david-pi-maintenance:\n    image: david-family-photos:old\n"
        "  device-backup-worker:\n    image: david-family-photos:old\n",
        encoding="utf-8",
    )
    readiness = _write_legacy_readiness(root)
    candidate_override = root / "candidate-images.compose.json"
    rollback_override = root / "rollback-images.compose.json"
    identity_candidate = {
        "image_id": f"sha256:{'e' * 64}", "matching_repo_digests": [candidate],
        "architecture": "arm64", "os": "linux",
    }
    identity_previous = {
        "image_id": f"sha256:{'f' * 64}", "matching_repo_digests": [previous],
        "architecture": "arm64", "os": "linux",
    }
    rollback, candidate_value, rollback_value = rollback_metadata.build_metadata(
        candidate=candidate, previous=previous, revision=revision, compose=compose,
        services=[
            "photo-portal", "chat-notifier", "audiobook-preparer",
            "slideshow-worker", "david-pi-maintenance", "device-backup-worker",
        ],
        candidate_override=candidate_override, rollback_override=rollback_override,
        legacy_readiness=readiness,
        candidate_identity=identity_candidate, previous_identity=identity_previous,
    )
    release_contract.write_json_atomic(candidate_override, candidate_value)
    release_contract.write_json_atomic(rollback_override, rollback_value)
    rollback["overrides"]["candidate"]["sha256"] = release_contract.file_sha256(candidate_override)
    rollback["overrides"]["rollback"]["sha256"] = release_contract.file_sha256(rollback_override)
    rollback_path = root / "rollback-metadata.json"
    release_contract.write_json_atomic(rollback_path, rollback)

    dependencies, image = _raw_scans(candidate, high=raw_high)
    dependency_raw = root / "scan.dependencies.raw.json"
    image_raw = root / "scan.image.raw.json"
    release_contract.write_json_atomic(dependency_raw, dependencies)
    release_contract.write_json_atomic(image_raw, image)
    dependency_summary = vulnerability_scan.summarize_scan(
        dependencies, "dependencies", "requirements.txt"
    )
    image_summary = vulnerability_scan.summarize_scan(image, "image", candidate)
    dependency_summary["raw_evidence"] = {
        "path": dependency_raw.name, "sha256": release_contract.file_sha256(dependency_raw),
    }
    image_summary["raw_evidence"] = {
        "path": image_raw.name, "sha256": release_contract.file_sha256(image_raw),
    }
    requirements = root / "requirements.txt"
    requirements.write_text("Flask==3.1.3\n", encoding="utf-8")
    requirements_hash = release_contract.file_sha256(requirements)
    vulnerability = {
        "schema_version": 1, "kind": "david-pi-vulnerability-evidence",
        "status": "pass", "blocking_finding_count": 0,
        "scanner": {
            "name": "trivy", "version": "0.74.0",
            "executable_sha256": "3" * 64,
            "version_output_sha256": "4" * 64,
            "vulnerability_database": {
                "schema_version": "2", "updated_at": "2099-01-01T00:00:00Z",
                "downloaded_at": "2099-01-01T00:01:00Z",
            },
        },
        "policy": {
            "blocking_severities": ["CRITICAL", "HIGH"],
            "unfixed_findings_ignored": False, "scanner_required": "trivy",
            "scanner_version": "0.74.0",
        },
        "subjects": {
            "source": {
                "revision": revision, "dirty": False,
                "requirements_sha256": requirements_hash,
            },
            "image": candidate,
        },
        "scans": [dependency_summary, image_summary],
    }
    vulnerability_path = root / "vulnerability.json"
    release_contract.write_json_atomic(vulnerability_path, vulnerability)

    dockerfile = root / "Dockerfile"
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    dockerfile_hash = release_contract.file_sha256(dockerfile)
    base_evidence = {
        "schema_version": 1, "kind": "david-pi-base-image-provenance", "status": "pass",
        "base_image": {
            "reference": base, "repository": "python", "digest": f"sha256:{'c' * 64}",
            "image_id": f"sha256:{'9' * 64}", "matching_repo_digests": [base],
            "rootfs_layers": [f"sha256:{'7' * 64}"],
            "architecture": "arm64", "os": "linux",
            "runtime_contract": {
                "python_series": "3.13", "python_version": "3.13.7",
                "distribution": "alpine", "alpine_series": "3.22",
                "alpine_version": "3.22.2", "libc": "musl",
                "musl_loader": "/lib/ld-musl-aarch64.so.1",
            },
        },
        "build_contract": {
            "argument": "PYTHON_BASE_IMAGE", "verified_locally": True,
            "dockerfile_sha256": dockerfile_hash,
        },
    }
    base_path = root / "base.json"
    release_contract.write_json_atomic(base_path, base_evidence)
    android_build = root / "clients" / "android" / "app" / "build.gradle.kts"
    android_build.parent.mkdir(parents=True, exist_ok=True)
    android_build.write_text(
        "android {\n  defaultConfig {\n"
        '    applicationId = "com.davidpi.backup"\n'
        "    versionCode = 25\n"
        '    versionName = "1.1.5-complete-reconciliation"\n'
        "  }\n}\n",
        encoding="utf-8",
    )
    android_source = (
        root / "clients" / "android" / "app" / "src" / "main" / "java"
        / "com" / "davidpi" / "backup" / "Main.kt"
    )
    android_source.parent.mkdir(parents=True, exist_ok=True)
    android_source.write_text("package com.davidpi.backup\n", encoding="utf-8")
    android_apk = root / "artifacts" / "android" / "david-pi-backup.apk"
    android_apk.parent.mkdir(parents=True, exist_ok=True)
    android_apk.write_bytes(b"signed-android-release-fixture")
    android_signer = "6" * 64
    android_files = []
    tree = hashlib.sha256()
    for path in sorted((android_build, android_source)):
        relative = path.relative_to(root).as_posix()
        digest = release_contract.file_sha256(path)
        size = path.stat().st_size
        encoded = relative.encode("utf-8")
        tree.update(len(encoded).to_bytes(4, "big"))
        tree.update(encoded)
        tree.update(size.to_bytes(8, "big"))
        tree.update(bytes.fromhex(digest))
        android_files.append({"path": relative, "sha256": digest, "size_bytes": size})
    android_release = {
        "schema_version": 2,
        "kind": "david-pi-android-release-attestation",
        "provenance_model": "signed_builder_declaration",
        "reproducible_build": False,
        "android_build_tools_version": "35.0.0",
        "verification_tools": {
            "aapt": {
                "sha256": "4" * 64,
                "implementation_sha256": "7" * 64, "version": "fixture",
            },
            "apksigner": {
                "sha256": "5" * 64,
                "implementation_sha256": "6" * 64,
                "runtime_sha256": "8" * 64,
                "runtime_file_count": 3,
                "runtime_tree_sha256": "9" * 64,
                "runtime_version": "fixture java 17",
                "version": "fixture",
            },
        },
        "application_id": "com.davidpi.backup",
        "version_code": 25,
        "version_name": "1.1.5-complete-reconciliation",
        "version_policy": {
            "minimum_version_code": 25,
            "expected_version_code": 25,
            "expected_version_name": "1.1.5-complete-reconciliation",
        },
        "firebase_client_configured": False,
        "artifact": {
            "path": "artifacts/android/david-pi-backup.apk",
            "sha256": release_contract.file_sha256(android_apk),
            "size_bytes": android_apk.stat().st_size,
        },
        "signing": {
            "certificate_sha256": android_signer,
            "signer_count": 1,
            "schemes": {"v1": False, "v2": True, "v3": False, "v31": False, "v4": False},
        },
        "source": {
            "application_id": "com.davidpi.backup",
            "build_file": "clients/android/app/build.gradle.kts",
            "build_file_sha256": release_contract.file_sha256(android_build),
            "embedded_tree_sha256": tree.hexdigest(),
            "version_code": 25,
            "version_name": "1.1.5-complete-reconciliation",
            "release_inputs": {
                "root": "clients/android",
                "file_count": len(android_files),
                "tree_sha256": tree.hexdigest(),
                "files": android_files,
            },
        },
    }
    android_policy = root / "config" / "android-release.json"
    android_policy.parent.mkdir(parents=True, exist_ok=True)
    release_contract.write_json_atomic(android_policy, {
        "schema_version": 1,
        "kind": "david-pi-android-release-policy",
        "application_id": "com.davidpi.backup",
        "artifact": "artifacts/android/david-pi-backup.apk",
        "attestation": "artifacts/android/david-pi-backup.manifest.json",
        "source_build_file": "clients/android/app/build.gradle.kts",
        "release_signer_certificate_sha256": android_signer,
        "minimum_version_code": 25,
        "expected_version_code": 25,
        "expected_version_name": "1.1.5-complete-reconciliation",
        "android_build_tools_version": "35.0.0",
        "verification_tools": {
            "aapt": {
                "sha256": "4" * 64,
                "implementation_sha256": "7" * 64,
                "version": "fixture",
            },
            "apksigner": {
                "sha256": "5" * 64,
                "implementation_sha256": "6" * 64,
                "runtime_sha256": "8" * 64,
                "runtime_file_count": 3,
                "runtime_tree_sha256": "9" * 64,
                "runtime_version": "fixture java 17",
                "version": "fixture",
            },
        },
    })
    release_contract.write_json_atomic(
        root / "artifacts" / "android" / "david-pi-backup.manifest.json",
        android_release,
    )
    release = {
        "schema_version": 1,
        "source": {"revision": revision, "dirty": False},
        "build": {
            "provenance_model": "trusted_builder_declaration",
            "reproducible_build": False,
            "requirements_sha256": requirements_hash,
            "dockerfile_sha256": dockerfile_hash,
            "android_release": android_release,
            "android_release_image_verified": True,
            "image": {
                "reference": candidate, "repo_digests": [candidate],
                "digest": candidate, "image_id": f"sha256:{'e' * 64}",
                "rootfs_layers": [f"sha256:{'7' * 64}", f"sha256:{'8' * 64}"],
                "architecture": "arm64", "os": "linux",
                "config": {
                    "user": "10001:10001",
                    "working_dir": "/app",
                    "entrypoint": ["/usr/local/bin/docker-entrypoint"],
                    "cmd": [
                        "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2",
                        "--threads", "2", "--timeout", "600", "--worker-tmp-dir",
                        "/dev/shm", "app:app",
                    ],
                    "env": [
                        "PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                        "GPG_KEY=" + "A" * 40,
                        "PYTHON_VERSION=3.13.13",
                        "PYTHON_SHA256=" + "9" * 64,
                        "PYTHONDONTWRITEBYTECODE=1",
                        "PYTHONUNBUFFERED=1",
                        "PHOTO_DATA=/data",
                        "TMPDIR=/data/tmp/uploads",
                        "XDG_CACHE_HOME=/data/tmp/runtime",
                    ],
                    "exposed_ports": ["8000/tcp"],
                    "healthcheck": None,
                    "volumes": [],
                },
                "labels": {
                    "org.opencontainers.image.revision": revision,
                    "org.opencontainers.image.base.name": base,
                    "org.opencontainers.image.base.digest": f"sha256:{'c' * 64}",
                },
            },
        },
    }
    release_path = root / "release.json"
    release_contract.write_json_atomic(release_path, release)
    builder_metadata_path = root / "candidate-build-metadata.json"
    release_contract.write_json_atomic(builder_metadata_path, {
        "containerimage.digest": release_contract.image_digest(candidate),
        "containerimage.config.digest": f"sha256:{'e' * 64}",
        "containerimage.descriptor": {
            "digest": release_contract.image_digest(candidate),
        },
    })
    sbom_value = {
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "metadata": {"component": {
            "version": revision,
            "properties": [{"name": "david-pi:image-reference", "value": candidate}],
        }},
        "components": [{"name": "flask", "version": "3.1.3"}],
    }
    sbom_path = root / "sbom.json"
    release_contract.write_json_atomic(sbom_path, sbom_value)
    return {
        "candidate": candidate, "previous": previous, "base": base, "revision": revision,
        "release": release_path, "sbom": sbom_path, "vulnerability": vulnerability_path,
        "base_evidence": base_path, "rollback": rollback_path,
        "candidate_build_metadata": builder_metadata_path,
    }


def _promotion_document(root, fixture):
    artifacts = {
        name: {
            "path": path.name,
            "sha256": release_contract.file_sha256(path),
        }
        for name, path in {
            "release_manifest": fixture["release"], "sbom": fixture["sbom"],
            "vulnerability_evidence": fixture["vulnerability"],
            "base_image_evidence": fixture["base_evidence"],
            "rollback_metadata": fixture["rollback"],
            "candidate_build_metadata": fixture["candidate_build_metadata"],
        }.items()
    }
    contract = {
        "candidate_image": fixture["candidate"],
        "rollback_image": fixture["previous"],
        "base_image": fixture["base"],
        "source_revision": fixture["revision"],
        "artifacts": artifacts,
        "policy": {
            "digest_only": True,
            "blocking_vulnerability_severities": ["CRITICAL", "HIGH"],
            "unfixed_findings_ignored": False,
            "automatic_deployment": False,
        },
    }
    return {
        "schema_version": 1, "kind": "david-pi-production-promotion",
        "status": "approved", **contract,
        "contract_sha256": release_contract.canonical_sha256(contract),
    }


def test_promotion_checker_accepts_bound_evidence_and_exact_rollback():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        checked = promotion_manifest.check_manifest(manifest, root)
    assert checked["candidate_image"] == fixture["candidate"]
    assert checked["rollback_image"] == fixture["previous"]


def test_promotion_rejects_changed_builder_metadata_even_with_rehashed_artifact():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        metadata = json.loads(fixture["candidate_build_metadata"].read_text())
        metadata["containerimage.config.digest"] = f"sha256:{'0' * 64}"
        release_contract.write_json_atomic(fixture["candidate_build_metadata"], metadata)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        with pytest.raises(release_contract.ContractError, match="different image configuration"):
            promotion_manifest.check_manifest(manifest, root)


@pytest.mark.parametrize(
    "worker_name", ["device-backup-worker", "slideshow-worker"]
)
def test_promotion_rejects_assigning_pre_worker_image_to_candidate_worker(worker_name):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        rollback_override = root / "rollback-images.compose.json"
        override = json.loads(rollback_override.read_text(encoding="utf-8"))
        override["services"][worker_name] = {"image": fixture["previous"]}
        release_contract.write_json_atomic(rollback_override, override)
        rollback = json.loads(fixture["rollback"].read_text(encoding="utf-8"))
        rollback["overrides"]["rollback"]["sha256"] = release_contract.file_sha256(
            rollback_override
        )
        release_contract.write_json_atomic(fixture["rollback"], rollback)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        with pytest.raises(release_contract.ContractError, match="rollback Compose override"):
            promotion_manifest.check_manifest(manifest, root)


def test_promotion_rejects_changed_legacy_readiness_gate():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        readiness = root / "deploy" / "david-pi-legacy-writer-readiness"
        readiness.write_text("#!/bin/false\n", encoding="utf-8")
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        with pytest.raises(release_contract.ContractError, match="readiness hash"):
            promotion_manifest.check_manifest(manifest, root)


def test_promotion_rejects_mutable_candidate_even_with_rehashed_contract():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        document = _promotion_document(root, fixture)
        document["candidate_image"] = "registry.invalid/david-pi:latest"
        contract = {
            key: document[key] for key in (
                "candidate_image", "rollback_image", "base_image", "source_revision",
                "artifacts", "policy",
            )
        }
        document["contract_sha256"] = release_contract.canonical_sha256(contract)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, document)
        try:
            promotion_manifest.check_manifest(manifest, root)
        except release_contract.ContractError as error:
            assert "@sha256" in str(error)
        else:
            raise AssertionError("mutable production candidate was accepted")


def test_promotion_rejects_false_success_when_raw_scan_has_blocker():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root, raw_high=True)
        # Forge the top-level result while leaving raw machine evidence intact.
        vulnerability = json.loads(fixture["vulnerability"].read_text(encoding="utf-8"))
        vulnerability["status"] = "pass"
        vulnerability["blocking_finding_count"] = 0
        vulnerability["scans"][0]["blocking_finding_count"] = 0
        vulnerability["scans"][0]["finding_counts"] = {}
        vulnerability["scans"][0]["findings"] = []
        release_contract.write_json_atomic(fixture["vulnerability"], vulnerability)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        try:
            promotion_manifest.check_manifest(manifest, root)
        except release_contract.ContractError as error:
            assert "normalized scan" in str(error) or "blocking" in str(error)
        else:
            raise AssertionError("forged false-success vulnerability scan was accepted")


def test_promotion_rejects_candidate_identity_disagreement():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        release = json.loads(fixture["release"].read_text(encoding="utf-8"))
        release["build"]["image"]["image_id"] = f"sha256:{'0' * 64}"
        release_contract.write_json_atomic(fixture["release"], release)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        try:
            promotion_manifest.check_manifest(manifest, root)
        except release_contract.ContractError as error:
            assert "different image configuration" in str(error)
        else:
            raise AssertionError("conflicting candidate image identities were accepted")


def test_promotion_rejects_android_release_evidence_disagreement():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        release = json.loads(fixture["release"].read_text(encoding="utf-8"))
        release["build"]["android_release"]["version_code"] = 24
        release_contract.write_json_atomic(fixture["release"], release)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        with pytest.raises(release_contract.ContractError, match="Android artifact evidence"):
            promotion_manifest.check_manifest(manifest, root)


def test_promotion_rejects_candidate_base_architecture_disagreement():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        release = json.loads(fixture["release"].read_text(encoding="utf-8"))
        release["build"]["image"]["architecture"] = "amd64"
        release_contract.write_json_atomic(fixture["release"], release)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        try:
            promotion_manifest.check_manifest(manifest, root)
        except release_contract.ContractError as error:
            assert any(message in str(error) for message in ("architectures differ", "linux/amd64 or linux/arm64", "runtime contract"))
        else:
            raise AssertionError("an AMD64 production candidate was accepted")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("user", "0", "user"),
        ("working_dir", "/tmp", "working directory"),
        ("entrypoint", ["/evil"], "entrypoint"),
        ("cmd", ["python", "-c", "pass"], "command"),
        ("exposed_ports", ["22/tcp", "8000/tcp"], "ports"),
        ("healthcheck", {"Test": ["CMD", "/evil"]}, "healthcheck"),
        ("volumes", ["/app"], "volumes"),
        ("env", ["PYTHONPATH=/evil"], "environment"),
    ],
)
def test_promotion_rejects_candidate_runtime_config_disagreement(field, value, message):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = _write_promotion_fixture(root)
        release = json.loads(fixture["release"].read_text(encoding="utf-8"))
        release["build"]["image"]["config"][field] = value
        release_contract.write_json_atomic(fixture["release"], release)
        manifest = root / "promotion.json"
        release_contract.write_json_atomic(manifest, _promotion_document(root, fixture))
        with pytest.raises(release_contract.ContractError, match=message):
            promotion_manifest.check_manifest(manifest, root)


def test_base_provenance_accepts_amd64_with_its_own_musl_loader(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ARG PYTHON_BASE_IMAGE=python:3.13-alpine3.22\nFROM ${PYTHON_BASE_IMAGE} AS runtime-base\n")
    reference = _ref("python", "c")
    inspected = {"Id": "sha256:" + "f" * 64, "RepoDigests": [reference], "Architecture": "amd64", "Os": "linux", "RootFS": {"Layers": ["sha256:" + "1" * 64]}}
    contract = {"python_series": "3.13", "python_version": "3.13.7", "distribution": "alpine", "alpine_series": "3.22", "alpine_version": "3.22.2", "libc": "musl", "musl_loader": "/lib/ld-musl-x86_64.so.1"}
    assert base_image_provenance.build_evidence(reference, inspected, dockerfile, contract)["base_image"]["architecture"] == "amd64"
    with pytest.raises(release_contract.ContractError):
        base_image_provenance.require_runtime_contract(contract, "arm64")


def test_base_platform_selection_does_not_reuse_native_index_metadata(monkeypatch):
    calls = []
    def engine(arguments, **kwargs):
        calls.append(arguments)
        architecture = 'arm64' if '--platform' in arguments and 'linux/arm64' in arguments else 'amd64'
        return subprocess.CompletedProcess(arguments, 0, json.dumps([{'Architecture': architecture, 'Os': 'linux'}]), '')
    monkeypatch.setattr(base_image_provenance.shutil, 'which', lambda _: '/docker')
    monkeypatch.setattr(base_image_provenance.subprocess, 'run', engine)
    actual = base_image_provenance.inspect_platform('python@sha256:' + '1' * 64, 'docker', 'arm64')
    assert actual['Architecture'] == 'arm64'
    assert ['--platform', 'linux/arm64'] == calls[0][3:5]


def test_legacy_engine_platform_fallback_rejects_wrong_architecture(monkeypatch):
    def engine(arguments, **kwargs):
        if '--platform' in arguments:
            return subprocess.CompletedProcess(arguments, 125, '', 'unknown flag: --platform')
        return subprocess.CompletedProcess(arguments, 0, json.dumps([{'Architecture':'amd64','Os':'linux'}]), '')
    monkeypatch.setattr(base_image_provenance.shutil, 'which', lambda _: '/docker')
    monkeypatch.setattr(base_image_provenance.subprocess, 'run', engine)
    with pytest.raises(release_contract.ContractError, match='requested platform'):
        base_image_provenance.inspect_platform('python@sha256:' + '1' * 64, 'docker', 'arm64')
