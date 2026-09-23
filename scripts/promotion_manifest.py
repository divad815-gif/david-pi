#!/usr/bin/env python3
"""Create and verify a fail-closed, digest-only production promotion bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from release_contract import (
    ContractError,
    canonical_sha256,
    file_sha256,
    image_digest,
    image_repository,
    is_sha256_hex,
    is_sha256_identifier,
    load_json,
    relative_evidence_path,
    resolve_evidence_path,
    write_json_atomic,
)
from vulnerability_scan import BLOCKING_SEVERITIES, REQUIRED_TRIVY_VERSION, summarize_scan
from base_image_provenance import REQUIRED_REPOSITORY, require_runtime_contract
from resolve_candidate_image import load_build_metadata


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.android_release import (  # noqa: E402
    AndroidReleaseError,
)
from android_image_release import (  # noqa: E402
    verified_candidate_android_release,
    verify_image_runtime_config,
)

REQUIRED_OS = "linux"
SUPPORTED_ARCHITECTURES = {"amd64", "arm64"}
LEGACY_DISABLED_SERVICES = ("device-backup-worker", "slideshow-worker")
LEGACY_DISABLED_CONTAINERS = (
    "david-pi-device-backup-worker",
    "david-pi-slideshow-worker",
)
LEGACY_DISABLED_PROFILE = "rollback-disabled"
REQUIRED_FIRST_ROLLOUT_SERVICES = {
    "photo-portal",
    "chat-notifier",
    "audiobook-preparer",
    "slideshow-worker",
    "david-pi-maintenance",
    "device-backup-worker",
}


def _require(value: bool, message: str) -> None:
    if not value:
        raise ContractError(message)


def _digest_matches(reference: str, observed: list[Any]) -> bool:
    expected_digest = image_digest(reference)
    expected_repository = image_repository(reference)
    for item in observed:
        if not isinstance(item, str):
            continue
        try:
            if (
                image_digest(item) == expected_digest
                and image_repository(item) == expected_repository
            ):
                return True
        except ContractError:
            continue
    return False


def _image_properties(sbom: dict[str, Any]) -> dict[str, str]:
    component = ((sbom.get("metadata") or {}).get("component") or {})
    properties = component.get("properties") or []
    return {
        str(item.get("name")): str(item.get("value"))
        for item in properties
        if isinstance(item, dict) and item.get("name") is not None
    }


def validate_release_manifest(
    release: dict[str, Any], candidate: str, base: str, revision: str,
    source_root: Path,
) -> None:
    _require(release.get("schema_version") == 1, "unsupported release manifest schema")
    source = release.get("source") or {}
    _require(source.get("revision") == revision, "release manifest revision does not match")
    _require(source.get("dirty") is False, "dirty source cannot be promoted")
    build = release.get("build") or {}
    _require(
        build.get("provenance_model") == "trusted_builder_declaration"
        and build.get("reproducible_build") is False,
        "release build provenance model is unsupported",
    )
    image = build.get("image") or {}
    _require(image.get("reference") == candidate, "release manifest names the wrong candidate")
    repo_digests = image.get("repo_digests")
    if not isinstance(repo_digests, list):
        repo_digests = [item for item in str(image.get("digest") or "").split(",") if item]
    _require(_digest_matches(candidate, repo_digests), "release manifest did not prove candidate digest")
    _require(is_sha256_identifier(image.get("image_id")), "candidate image ID is missing")
    _require(
        image.get("architecture") in SUPPORTED_ARCHITECTURES
        and image.get("os") == REQUIRED_OS,
        "candidate image is not linux/amd64 or linux/arm64",
    )
    try:
        verify_image_runtime_config(image.get("config"))
    except AndroidReleaseError as error:
        raise ContractError(str(error)) from error
    labels = image.get("labels") or {}
    _require(isinstance(labels, dict), "candidate image labels are malformed")
    _require(
        labels.get("org.opencontainers.image.revision") == revision,
        "candidate OCI revision label does not match source",
    )
    _require(
        labels.get("org.opencontainers.image.base.name") == base,
        "candidate OCI base image label does not match verified base",
    )
    _require(
        labels.get("org.opencontainers.image.base.digest") == image_digest(base),
        "candidate OCI base digest label does not match verified base",
    )
    try:
        expected_android = verified_candidate_android_release(candidate, source_root)
    except AndroidReleaseError as error:
        raise ContractError(f"Android release is not promotable: {error}") from error
    _require(
        build.get("android_release") == expected_android,
        "release manifest Android artifact evidence differs from source",
    )
    _require(
        build.get("android_release_image_verified") is True,
        "release manifest did not verify the candidate Android bundle",
    )


def validate_sbom(sbom: dict[str, Any], candidate: str, revision: str) -> None:
    _require(sbom.get("bomFormat") == "CycloneDX", "SBOM is not CycloneDX")
    _require(sbom.get("specVersion") == "1.5", "unsupported SBOM version")
    component = ((sbom.get("metadata") or {}).get("component") or {})
    _require(component.get("version") == revision, "SBOM revision does not match")
    properties = _image_properties(sbom)
    _require(
        properties.get("david-pi:image-reference") == candidate,
        "SBOM image reference does not match candidate",
    )
    components = sbom.get("components")
    _require(isinstance(components, list) and bool(components), "SBOM has no components")


def validate_base_evidence(base_evidence: dict[str, Any]) -> str:
    _require(
        base_evidence.get("kind") == "david-pi-base-image-provenance",
        "base evidence has the wrong kind",
    )
    _require(base_evidence.get("status") == "pass", "base image provenance did not pass")
    base = base_evidence.get("base_image") or {}
    reference = base.get("reference")
    _require(isinstance(reference, str), "base image reference is missing")
    expected_digest = image_digest(reference)
    _require(
        image_repository(reference) == REQUIRED_REPOSITORY,
        "base image is not the Docker Official Image repository python",
    )
    _require(base.get("digest") == expected_digest, "base evidence digest is inconsistent")
    _require(base.get("repository") == image_repository(reference), "base repository is inconsistent")
    _require(
        _digest_matches(reference, base.get("matching_repo_digests") or []),
        "base evidence did not prove its immutable reference",
    )
    _require(is_sha256_identifier(base.get("image_id")), "base image ID is missing")
    _require(
        base.get("architecture") in SUPPORTED_ARCHITECTURES
        and base.get("os") == REQUIRED_OS,
        "base image is not linux/amd64 or linux/arm64",
    )
    rootfs_layers = base.get("rootfs_layers")
    _require(
        isinstance(rootfs_layers, list) and bool(rootfs_layers)
        and all(is_sha256_identifier(item) for item in rootfs_layers),
        "base root filesystem identity is missing",
    )
    try:
        require_runtime_contract(base.get("runtime_contract"), base.get("architecture"))
    except ContractError as error:
        raise ContractError(str(error)) from error
    contract = base_evidence.get("build_contract") or {}
    _require(contract.get("argument") == "PYTHON_BASE_IMAGE", "base build argument is wrong")
    _require(contract.get("verified_locally") is True, "base image was not verified locally")
    return reference


def _raw_scan_payload(
    scan: dict[str, Any], evidence_dir: Path, expected_kind: str
) -> dict[str, Any]:
    raw = scan.get("raw_evidence") or {}
    path = resolve_evidence_path(evidence_dir, raw.get("path"))
    _require(file_sha256(path) == raw.get("sha256"), f"{expected_kind} raw scan hash differs")
    return load_json(path, f"{expected_kind} raw scanner evidence")


def validate_vulnerability_evidence(
    evidence: dict[str, Any], candidate: str, revision: str,
    requirements_sha256: str, candidate_image_id: str, evidence_dir: Path,
) -> None:
    _require(
        evidence.get("kind") == "david-pi-vulnerability-evidence",
        "vulnerability evidence has the wrong kind",
    )
    _require(evidence.get("status") == "pass", "vulnerability scan did not pass")
    _require(evidence.get("blocking_finding_count") == 0, "vulnerability evidence has blockers")
    scanner = evidence.get("scanner") or {}
    _require(scanner.get("name") == "trivy", "promotion requires Trivy evidence")
    _require(
        scanner.get("version") == REQUIRED_TRIVY_VERSION,
        "scanner version does not match the release contract",
    )
    _require(is_sha256_hex(scanner.get("executable_sha256")), "scanner binary identity is missing")
    _require(
        is_sha256_hex(scanner.get("version_output_sha256")),
        "scanner version evidence identity is missing",
    )
    database = scanner.get("vulnerability_database") or {}
    _require(bool(database.get("schema_version")), "scanner database schema is missing")
    _require(bool(database.get("updated_at")), "scanner database update identity is missing")
    _require(bool(database.get("downloaded_at")), "scanner database download identity is missing")
    policy = evidence.get("policy") or {}
    _require(
        sorted(policy.get("blocking_severities") or []) == sorted(BLOCKING_SEVERITIES),
        "vulnerability severity policy was weakened",
    )
    _require(policy.get("unfixed_findings_ignored") is False, "unfixed findings were hidden")
    _require(
        policy.get("scanner_version") == REQUIRED_TRIVY_VERSION,
        "vulnerability policy scanner version differs",
    )
    subjects = evidence.get("subjects") or {}
    source = subjects.get("source") or {}
    _require(source.get("revision") == revision, "dependency scan revision does not match")
    _require(source.get("dirty") is False, "dirty dependency scan cannot be promoted")
    _require(
        source.get("requirements_sha256") == requirements_sha256,
        "dependency scan covered a different requirements file",
    )
    _require(subjects.get("image") == candidate, "image scan covered a different candidate")

    scans = evidence.get("scans")
    _require(isinstance(scans, list), "vulnerability evidence has no scans")
    _require(len(scans) == 2, "vulnerability evidence must contain exactly two scans")
    by_kind = {
        scan.get("kind"): scan for scan in scans
        if isinstance(scan, dict) and isinstance(scan.get("kind"), str)
    }
    _require(set(by_kind) == {"dependencies", "image"}, "both dependency and image scans are required")
    total_blockers = 0
    for kind in ("dependencies", "image"):
        recorded = by_kind[kind]
        _require(recorded.get("status") == "completed", f"{kind} scan did not complete")
        requested = "requirements.txt" if kind == "dependencies" else candidate
        raw_payload = _raw_scan_payload(recorded, evidence_dir, kind)
        rebuilt = summarize_scan(raw_payload, kind, requested)
        comparable = dict(recorded)
        comparable.pop("raw_evidence", None)
        _require(comparable == rebuilt, f"{kind} normalized scan does not match raw evidence")
        total_blockers += rebuilt["blocking_finding_count"]
    _require(total_blockers == 0, "raw scanner evidence contains blocking vulnerabilities")
    image_identity = by_kind["image"].get("identity") or {}
    _require(
        image_identity.get("image_id") == candidate_image_id,
        "image scan and release manifest identify different local images",
    )
    observed = image_identity.get("repo_digests") or []
    artifact_name = image_identity.get("artifact_name")
    _require(
        _digest_matches(candidate, observed) or artifact_name == candidate,
        "image scanner did not prove the immutable candidate identity",
    )


def validate_rollback(
    rollback: dict[str, Any], candidate: str, revision: str,
    candidate_image_id: str, evidence_dir: Path, source_root: Path,
) -> str:
    _require(rollback.get("schema_version") == 2, "unsupported rollback evidence schema")
    _require(rollback.get("kind") == "david-pi-exact-rollback", "rollback evidence has wrong kind")
    _require(rollback.get("status") == "ready", "rollback evidence is not ready")
    _require(rollback.get("source_revision") == revision, "rollback revision does not match")
    candidate_value = rollback.get("candidate") or {}
    previous_value = rollback.get("rollback") or {}
    _require(candidate_value.get("image") == candidate, "rollback metadata candidate differs")
    _require(candidate_value.get("digest") == image_digest(candidate), "candidate digest is inconsistent")
    _require(
        candidate_value.get("image_id") == candidate_image_id,
        "rollback metadata and release manifest identify different candidate images",
    )
    previous = previous_value.get("image")
    _require(isinstance(previous, str), "previous image is missing")
    _require(previous_value.get("digest") == image_digest(previous), "rollback digest is inconsistent")
    _require(image_repository(previous) == image_repository(candidate), "rollback repository differs")
    _require(image_digest(previous) != image_digest(candidate), "rollback and candidate are identical")
    for value, label in ((candidate_value, "candidate"), (previous_value, "rollback")):
        _require(is_sha256_identifier(value.get("image_id")), f"{label} image ID missing")
        _require(
            value.get("architecture") in SUPPORTED_ARCHITECTURES
            and value.get("os") == REQUIRED_OS,
            f"{label} image is not linux/amd64 or linux/arm64",
        )
        _require(
            _digest_matches(value["image"], value.get("matching_repo_digests") or []),
            f"{label} local digest proof is missing",
        )

    _require(candidate_value.get("architecture") == previous_value.get("architecture"), "rollback architecture differs from candidate")
    services = rollback.get("services")
    _require(isinstance(services, list) and bool(services), "rollback service list is empty")
    _require(all(isinstance(item, dict) for item in services), "rollback service list is malformed")
    service_names = [item.get("name") for item in services]
    _require(
        all(isinstance(name, str) and bool(name) for name in service_names),
        "rollback service list is malformed",
    )
    names = sorted(service_names)
    _require(len(names) == len(set(names)), "rollback service list contains duplicates")
    _require(
        set(names) == REQUIRED_FIRST_ROLLOUT_SERVICES,
        "rollback service list does not match the exact six-writer set",
    )
    rollback_names = [name for name in names if name not in LEGACY_DISABLED_SERVICES]
    for item in services:
        _require(item.get("candidate_image") == candidate, "service candidate image is not exact")
        if item.get("name") in LEGACY_DISABLED_SERVICES:
            _require(item.get("rollback_action") == "stop", "candidate-only worker rollback action is unsafe")
            _require(item.get("rollback_image") is None, "previous image was assigned to a candidate-only worker")
        else:
            _require(item.get("rollback_action") == "replace", "service rollback action is not exact")
            _require(item.get("rollback_image") == previous, "service rollback image is not exact")

    profile = rollback.get("rollback_profile") or {}
    _require(
        profile == {
            "kind": "legacy-without-candidate-workers",
            "candidate_services": names,
            "rollback_services": rollback_names,
            "stopped_services": list(LEGACY_DISABLED_SERVICES),
            "disabled_containers": list(LEGACY_DISABLED_CONTAINERS),
            "preserves": [
                "media",
                "publication_intents",
                "slideshow_jobs",
                "device_uploads",
                "secondary_copy_state",
            ],
        },
        "legacy rollback profile is not exact",
    )

    overrides = rollback.get("overrides") or {}
    for role in ("candidate", "rollback"):
        record = overrides.get(role) or {}
        path = resolve_evidence_path(evidence_dir, record.get("path"))
        _require(file_sha256(path) == record.get("sha256"), f"{role} override hash differs")
        command_path = resolve_evidence_path(source_root, record.get("command_path"))
        _require(command_path == path, f"{role} command points to a different override")
        payload = load_json(path, f"{role} Compose override")
        if role == "candidate":
            expected_payload = {
                "services": {name: {"image": candidate} for name in names}
            }
        else:
            expected_payload = {
                "services": {
                    **{name: {"image": previous} for name in rollback_names},
                    **{
                        name: {
                            "image": candidate,
                            "profiles": [LEGACY_DISABLED_PROFILE],
                        }
                        for name in LEGACY_DISABLED_SERVICES
                    },
                }
            }
        _require(payload == expected_payload, f"{role} Compose override is not exact")

    readiness = rollback.get("readiness") or {}
    readiness_path = resolve_evidence_path(
        source_root, readiness.get("command_path")
    )
    _require(readiness_path.is_file(), "legacy rollback readiness gate is missing")
    _require(
        readiness.get("path") == readiness_path.name,
        "legacy rollback readiness path is inconsistent",
    )
    _require(
        file_sha256(readiness_path) == readiness.get("sha256"),
        "legacy rollback readiness hash differs",
    )

    compose = rollback.get("compose") or {}
    compose_path = resolve_evidence_path(source_root, compose.get("path"))
    _require(file_sha256(compose_path) == compose.get("sha256"), "Compose source hash differs")
    commands = rollback.get("commands") or {}
    candidate_override = (overrides.get("candidate") or {}).get("command_path")
    rollback_override = (overrides.get("rollback") or {}).get("command_path")
    compose_name = compose.get("path")
    _require(commands.get("fetch_candidate") == ["docker", "pull", candidate], "candidate fetch is not exact")
    _require(commands.get("fetch_rollback") == ["docker", "pull", previous], "rollback fetch is not exact")
    _require(commands.get("promote") == [
        "docker", "compose", "-f", compose_name, "-f", candidate_override,
        "up", "-d", "--no-build", "--pull", "never", *names,
    ], "promotion command is not exact")
    _require(commands.get("stop_for_rollback") == [
        "docker", "compose", "-f", compose_name, "-f", rollback_override,
        "stop", "--timeout", "30", *LEGACY_DISABLED_SERVICES,
    ], "candidate-only worker stop command is not exact")
    _require(commands.get("rollback") == [
        "docker", "compose", "-f", compose_name, "-f", rollback_override,
        "up", "-d", "--no-build", "--pull", "never", *rollback_names,
    ], "rollback command is not exact")
    _require(commands.get("verify_rollback") == [
        readiness.get("command_path"),
        "--timeout", "90", "--interval", "2", "--stable-samples", "3",
    ], "legacy rollback readiness command is not exact")
    _require(
        commands.get("rollback_sequence") == [
            "stop_for_rollback", "rollback", "verify_rollback"
        ],
        "rollback sequence is not exact",
    )
    _require(rollback.get("automatic_execution") is False, "rollback metadata must not execute itself")
    return previous


def _artifact_record(path: Path, output_parent: Path) -> dict[str, str]:
    return {
        "path": relative_evidence_path(path, output_parent),
        "sha256": file_sha256(path),
    }


def validate_inputs(
    *, release_path: Path, sbom_path: Path, vulnerability_path: Path,
    base_path: Path, rollback_path: Path, builder_metadata_path: Path,
    source_root: Path,
) -> tuple[str, str, str, str]:
    release = load_json(release_path, "release manifest")
    sbom = load_json(sbom_path, "SBOM")
    vulnerability = load_json(vulnerability_path, "vulnerability evidence")
    base_evidence = load_json(base_path, "base image evidence")
    rollback = load_json(rollback_path, "rollback metadata")
    candidate = ((rollback.get("candidate") or {}).get("image"))
    revision = rollback.get("source_revision")
    _require(isinstance(candidate, str), "candidate image is missing")
    image_digest(candidate)
    _require(isinstance(revision, str), "source revision is missing")
    base = validate_base_evidence(base_evidence)
    validate_release_manifest(release, candidate, base, revision, source_root)
    _require(release["build"]["image"]["architecture"] == base_evidence["base_image"]["architecture"], "candidate and base architectures differ")
    release_image = ((release.get("build") or {}).get("image") or {})
    candidate_image_id = release_image.get("image_id")
    builder_metadata = load_build_metadata(builder_metadata_path)
    _require(
        builder_metadata["containerimage.digest"] == image_digest(candidate),
        "candidate builder metadata identifies a different pushed image",
    )
    _require(
        builder_metadata["containerimage.config.digest"] == candidate_image_id,
        "candidate builder metadata identifies a different image configuration",
    )
    candidate_layers = release_image.get("rootfs_layers")
    base_layers = ((base_evidence.get("base_image") or {}).get("rootfs_layers"))
    _require(
        isinstance(candidate_layers, list)
        and candidate_layers[:len(base_layers)] == base_layers,
        "candidate root filesystem does not descend from the verified base image",
    )
    validate_sbom(sbom, candidate, revision)
    requirements_hash = ((release.get("build") or {}).get("requirements_sha256"))
    _require(isinstance(requirements_hash, str), "release requirements hash is missing")
    _require(
        requirements_hash == file_sha256(source_root / "requirements.txt"),
        "release requirements hash differs from source",
    )
    validate_vulnerability_evidence(
        vulnerability, candidate, revision, requirements_hash,
        candidate_image_id, vulnerability_path.parent,
    )
    previous = validate_rollback(
        rollback, candidate, revision, candidate_image_id,
        rollback_path.parent, source_root,
    )
    dockerfile_hash = ((release.get("build") or {}).get("dockerfile_sha256"))
    base_dockerfile_hash = ((base_evidence.get("build_contract") or {}).get("dockerfile_sha256"))
    _require(dockerfile_hash == base_dockerfile_hash, "base evidence covers a different Dockerfile")
    _require(
        dockerfile_hash == file_sha256(source_root / "Dockerfile"),
        "release Dockerfile hash differs from source",
    )
    return candidate, previous, base, revision


def create_manifest(args: argparse.Namespace) -> int:
    output = args.output if args.output.is_absolute() else ROOT / args.output
    paths = {
        "release_manifest": args.release_manifest,
        "sbom": args.sbom,
        "vulnerability_evidence": args.vulnerability_evidence,
        "base_image_evidence": args.base_image_evidence,
        "rollback_metadata": args.rollback_metadata,
        "candidate_build_metadata": args.candidate_build_metadata,
    }
    resolved = {
        name: path if path.is_absolute() else ROOT / path for name, path in paths.items()
    }
    candidate, previous, base, revision = validate_inputs(
        release_path=resolved["release_manifest"],
        sbom_path=resolved["sbom"],
        vulnerability_path=resolved["vulnerability_evidence"],
        base_path=resolved["base_image_evidence"],
        rollback_path=resolved["rollback_metadata"],
        builder_metadata_path=resolved["candidate_build_metadata"],
        source_root=ROOT,
    )
    artifacts = {
        name: _artifact_record(path, output.parent) for name, path in resolved.items()
    }
    contract = {
        "candidate_image": candidate,
        "rollback_image": previous,
        "base_image": base,
        "source_revision": revision,
        "artifacts": artifacts,
        "policy": {
            "digest_only": True,
            "blocking_vulnerability_severities": list(BLOCKING_SEVERITIES),
            "unfixed_findings_ignored": False,
            "automatic_deployment": False,
        },
    }
    manifest = {
        "schema_version": 1,
        "kind": "david-pi-production-promotion",
        "status": "approved",
        **contract,
        "contract_sha256": canonical_sha256(contract),
    }
    write_json_atomic(output, manifest)
    check_manifest(output, ROOT)
    print(output)
    return 0


def check_manifest(path: Path, source_root: Path = ROOT) -> dict[str, Any]:
    manifest = load_json(path, "promotion manifest")
    _require(manifest.get("schema_version") == 1, "unsupported promotion schema")
    _require(manifest.get("kind") == "david-pi-production-promotion", "wrong promotion kind")
    _require(manifest.get("status") == "approved", "promotion is not approved")
    candidate = manifest.get("candidate_image")
    previous = manifest.get("rollback_image")
    base = manifest.get("base_image")
    revision = manifest.get("source_revision")
    image_digest(candidate)
    image_digest(previous)
    image_digest(base)
    _require(image_digest(candidate) != image_digest(previous), "promotion has no distinct rollback")
    artifacts = manifest.get("artifacts")
    _require(isinstance(artifacts, dict), "promotion artifact map is missing")
    expected_names = {
        "release_manifest", "sbom", "vulnerability_evidence",
        "base_image_evidence", "rollback_metadata", "candidate_build_metadata",
    }
    _require(set(artifacts) == expected_names, "promotion artifact set is incomplete")
    resolved: dict[str, Path] = {}
    for name in sorted(expected_names):
        record = artifacts[name]
        _require(isinstance(record, dict), f"{name} artifact record is malformed")
        artifact_path = resolve_evidence_path(path.parent, record.get("path"))
        _require(file_sha256(artifact_path) == record.get("sha256"), f"{name} hash differs")
        resolved[name] = artifact_path
    checked = validate_inputs(
        release_path=resolved["release_manifest"],
        sbom_path=resolved["sbom"],
        vulnerability_path=resolved["vulnerability_evidence"],
        base_path=resolved["base_image_evidence"],
        rollback_path=resolved["rollback_metadata"],
        builder_metadata_path=resolved["candidate_build_metadata"],
        source_root=source_root,
    )
    _require(checked == (candidate, previous, base, revision), "promotion identities differ")
    policy = manifest.get("policy") or {}
    _require(policy.get("digest_only") is True, "digest-only policy is disabled")
    _require(
        policy.get("blocking_vulnerability_severities") == list(BLOCKING_SEVERITIES),
        "promotion vulnerability policy differs",
    )
    _require(policy.get("unfixed_findings_ignored") is False, "unfixed findings were ignored")
    _require(policy.get("automatic_deployment") is False, "manifest must not deploy automatically")
    contract = {
        key: manifest[key] for key in (
            "candidate_image", "rollback_image", "base_image", "source_revision",
            "artifacts", "policy",
        )
    }
    _require(canonical_sha256(contract) == manifest.get("contract_sha256"), "contract digest differs")
    return manifest


def error_manifest(message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "david-pi-production-promotion",
        "status": "error",
        "error": {"message": message[:4000]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--release-manifest", type=Path, required=True)
    create.add_argument("--sbom", type=Path, required=True)
    create.add_argument("--vulnerability-evidence", type=Path, required=True)
    create.add_argument("--base-image-evidence", type=Path, required=True)
    create.add_argument("--rollback-metadata", type=Path, required=True)
    create.add_argument("--candidate-build-metadata", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            return create_manifest(args)
        manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
        check_manifest(manifest, ROOT)
        print(manifest)
        return 0
    except (ContractError, OSError, KeyError) as error:
        if args.command == "create":
            output = args.output if args.output.is_absolute() else ROOT / args.output
            write_json_atomic(output, error_manifest(str(error)))
        print(f"promotion gate failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
