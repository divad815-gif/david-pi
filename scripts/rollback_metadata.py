#!/usr/bin/env python3
"""Generate exact candidate/rollback Compose overrides and rollback metadata."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from release_contract import (
    ContractError,
    file_sha256,
    image_digest,
    image_repository,
    is_sha256_identifier,
    write_json_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_OS = "linux"
SUPPORTED_ARCHITECTURES = {"amd64", "arm64"}
LEGACY_DISABLED_SERVICES = ("device-backup-worker", "slideshow-worker")
LEGACY_DISABLED_CONTAINERS = (
    "david-pi-device-backup-worker",
    "david-pi-slideshow-worker",
)
LEGACY_DISABLED_PROFILE = "rollback-disabled"
LEGACY_READINESS = ROOT / "deploy" / "david-pi-legacy-writer-readiness"
REQUIRED_FIRST_ROLLOUT_SERVICES = {
    "photo-portal",
    "chat-notifier",
    "audiobook-preparer",
    "slideshow-worker",
    "david-pi-maintenance",
    "device-backup-worker",
}
SERVICE_LINE = re.compile(r"^  ([A-Za-z0-9][A-Za-z0-9_.-]*):\s*$")
IMAGE_LINE = re.compile(r"^    image:\s*([^#\s]+)\s*(?:#.*)?$")


def compose_images(path: Path) -> dict[str, str]:
    services: dict[str, str] = {}
    current = ""
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "services:":
            in_services = True
            current = ""
            continue
        if in_services and line and not line.startswith(" "):
            break
        if not in_services:
            continue
        service_match = SERVICE_LINE.match(line)
        if service_match:
            current = service_match.group(1)
            services.setdefault(current, "")
            continue
        image_match = IMAGE_LINE.match(line)
        if current and image_match:
            services[current] = image_match.group(1)
    if not services:
        raise ContractError("compose file contains no services")
    return services


def _local_identity(reference: str, docker: str) -> dict[str, Any]:
    executable = shutil.which(docker) if "/" not in docker else docker
    if not executable:
        raise ContractError(f"container engine is unavailable: {docker}")
    result = subprocess.run(
        [executable, "image", "inspect", reference], text=True, capture_output=True
    )
    if result.returncode:
        raise ContractError(result.stderr.strip() or f"image is not available locally: {reference}")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractError(f"container engine returned invalid image metadata: {error}") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ContractError("container engine returned ambiguous image metadata")
    value = values[0]
    expected_digest = image_digest(reference)
    expected_repository = image_repository(reference)
    matching = []
    repo_digests = value.get("RepoDigests") or []
    if not isinstance(repo_digests, list):
        raise ContractError("container engine returned malformed image repo digests")
    for item in repo_digests:
        if not isinstance(item, str):
            continue
        try:
            if (
                image_digest(item) == expected_digest
                and image_repository(item) == expected_repository
            ):
                matching.append(item)
        except ContractError:
            continue
    if not matching:
        raise ContractError(f"local image does not match requested digest: {reference}")
    image_id = value.get("Id")
    if not is_sha256_identifier(image_id):
        raise ContractError(f"local image has no immutable ID: {reference}")
    return {
        "image_id": image_id,
        "matching_repo_digests": sorted(matching),
        "architecture": str(value.get("Architecture") or ""),
        "os": str(value.get("Os") or ""),
    }


def build_metadata(
    *,
    candidate: str,
    previous: str,
    revision: str,
    compose: Path,
    services: list[str],
    candidate_override: Path,
    rollback_override: Path,
    legacy_readiness: Path,
    candidate_identity: dict[str, Any],
    previous_identity: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate_digest = image_digest(candidate)
    previous_digest = image_digest(previous)
    if image_repository(candidate) != image_repository(previous):
        raise ContractError("candidate and rollback images must use the same repository")
    if candidate_digest == previous_digest:
        raise ContractError("rollback image must differ from the candidate image")
    for label, identity in (
        ("candidate", candidate_identity), ("rollback", previous_identity)
    ):
        if (
            identity.get("architecture") not in SUPPORTED_ARCHITECTURES
            or identity.get("os") != REQUIRED_OS
        ):
            raise ContractError(
                f"{label} image must be linux/amd64 or linux/arm64 for David-Pi"
            )
    if candidate_identity.get("architecture") != previous_identity.get("architecture"):
        raise ContractError("rollback architecture differs from candidate")
    declared = compose_images(compose)
    selected = sorted(set(services))
    if not selected:
        raise ContractError("at least one Compose service is required")
    if set(selected) != REQUIRED_FIRST_ROLLOUT_SERVICES:
        raise ContractError(
            "first-rollout candidate must include the exact six-writer service set"
        )
    missing = [name for name in selected if name not in declared]
    if missing:
        raise ContractError(f"unknown Compose services: {', '.join(missing)}")
    without_image = [name for name in selected if not declared[name]]
    if without_image:
        raise ContractError(f"services have no declared image: {', '.join(without_image)}")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ContractError("source revision must be an exact 40-character Git commit")

    rollback_services = [
        name for name in selected if name not in LEGACY_DISABLED_SERVICES
    ]
    if not rollback_services:
        raise ContractError("legacy rollback service list is empty")

    candidate_override_value = {
        "services": {name: {"image": candidate} for name in selected}
    }
    rollback_override_value = {
        "services": {
            **{name: {"image": previous} for name in rollback_services},
            **{
                name: {
                    "image": candidate,
                    "profiles": [LEGACY_DISABLED_PROFILE],
                }
                for name in LEGACY_DISABLED_SERVICES
            },
        }
    }
    try:
        candidate_command_path = candidate_override.resolve().relative_to(
            compose.parent.resolve()
        ).as_posix()
        rollback_command_path = rollback_override.resolve().relative_to(
            compose.parent.resolve()
        ).as_posix()
        readiness_command_path = legacy_readiness.resolve().relative_to(
            compose.parent.resolve()
        ).as_posix()
    except ValueError as error:
        raise ContractError(
            "Compose overrides and readiness gate must live below the release source root"
        ) from error
    if not legacy_readiness.is_file():
        raise ContractError("legacy rollback readiness gate is missing")
    compose_name = compose.name
    candidate_name = candidate_override.name
    rollback_name = rollback_override.name
    metadata = {
        "schema_version": 2,
        "kind": "david-pi-exact-rollback",
        "status": "ready",
        "source_revision": revision,
        "compose": {"path": compose_name, "sha256": file_sha256(compose)},
        "candidate": {
            "image": candidate,
            "digest": candidate_digest,
            **candidate_identity,
        },
        "rollback": {
            "image": previous,
            "digest": previous_digest,
            **previous_identity,
        },
        "services": [
            {
                "name": name,
                "declared_image": declared[name],
                "candidate_image": candidate,
                "rollback_action": (
                    "stop" if name in LEGACY_DISABLED_SERVICES else "replace"
                ),
                "rollback_image": (
                    None if name in LEGACY_DISABLED_SERVICES else previous
                ),
            }
            for name in selected
        ],
        "rollback_profile": {
            "kind": "legacy-without-candidate-workers",
            "candidate_services": selected,
            "rollback_services": rollback_services,
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
        "readiness": {
            "path": legacy_readiness.name,
            "command_path": readiness_command_path,
            "sha256": file_sha256(legacy_readiness),
        },
        "overrides": {
            "candidate": {"path": candidate_name, "command_path": candidate_command_path},
            "rollback": {"path": rollback_name, "command_path": rollback_command_path},
        },
        "commands": {
            "fetch_candidate": ["docker", "pull", candidate],
            "fetch_rollback": ["docker", "pull", previous],
            "promote": [
                "docker", "compose", "-f", compose_name, "-f", candidate_command_path,
                "up", "-d", "--no-build", "--pull", "never", *selected,
            ],
            "stop_for_rollback": [
                "docker", "compose", "-f", compose_name, "-f", rollback_command_path,
                "stop", "--timeout", "30", *LEGACY_DISABLED_SERVICES,
            ],
            "rollback": [
                "docker", "compose", "-f", compose_name, "-f", rollback_command_path,
                "up", "-d", "--no-build", "--pull", "never", *rollback_services,
            ],
            "verify_rollback": [
                readiness_command_path,
                "--timeout", "90", "--interval", "2", "--stable-samples", "3",
            ],
            "rollback_sequence": [
                "stop_for_rollback", "rollback", "verify_rollback"
            ],
        },
        "automatic_execution": False,
    }
    return metadata, candidate_override_value, rollback_override_value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--compose", type=Path, default=ROOT / "compose.yaml")
    parser.add_argument("--services", required=True)
    parser.add_argument("--candidate-override", type=Path, required=True)
    parser.add_argument("--rollback-override", type=Path, required=True)
    parser.add_argument(
        "--legacy-readiness", type=Path, default=LEGACY_READINESS
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()

    compose = args.compose if args.compose.is_absolute() else ROOT / args.compose
    candidate_override = (
        args.candidate_override if args.candidate_override.is_absolute()
        else ROOT / args.candidate_override
    )
    rollback_override = (
        args.rollback_override if args.rollback_override.is_absolute()
        else ROOT / args.rollback_override
    )
    output = args.output if args.output.is_absolute() else ROOT / args.output
    legacy_readiness = (
        args.legacy_readiness
        if args.legacy_readiness.is_absolute()
        else ROOT / args.legacy_readiness
    )
    try:
        candidate_identity = _local_identity(args.candidate_image, args.docker)
        previous_identity = _local_identity(args.previous_image, args.docker)
        metadata, candidate_value, rollback_value = build_metadata(
            candidate=args.candidate_image,
            previous=args.previous_image,
            revision=args.revision,
            compose=compose,
            services=[item for item in args.services.split(",") if item],
            candidate_override=candidate_override,
            rollback_override=rollback_override,
            legacy_readiness=legacy_readiness,
            candidate_identity=candidate_identity,
            previous_identity=previous_identity,
        )
        write_json_atomic(candidate_override, candidate_value)
        write_json_atomic(rollback_override, rollback_value)
        metadata["overrides"]["candidate"]["sha256"] = file_sha256(candidate_override)
        metadata["overrides"]["rollback"]["sha256"] = file_sha256(rollback_override)
        write_json_atomic(output, metadata)
    except (ContractError, OSError) as error:
        print(f"rollback metadata failed closed: {error}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
