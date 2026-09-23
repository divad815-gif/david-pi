#!/usr/bin/env python3
"""Verify a locally pulled digest-pinned base image and emit build provenance."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
SUPPORTED_ARCHITECTURES = {"amd64": "/lib/ld-musl-x86_64.so.1", "arm64": "/lib/ld-musl-aarch64.so.1"}
REQUIRED_REPOSITORY = "python"
REQUIRED_PYTHON_SERIES = "3.13"
REQUIRED_ALPINE_SERIES = "3.22"
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
PYTHON_VERSION = re.compile(r"3\.13\.[0-9]+")
ALPINE_VERSION = re.compile(r"3\.22\.[0-9]+")


def require_runtime_contract(value: Any, architecture: str | None = None) -> dict[str, str]:
    expected_keys = {
        "python_series", "python_version", "distribution", "alpine_series",
        "alpine_version", "libc", "musl_loader",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ContractError("base image runtime contract is incomplete")
    if (
        value.get("python_series") != REQUIRED_PYTHON_SERIES
        or not PYTHON_VERSION.fullmatch(str(value.get("python_version") or ""))
        or value.get("distribution") != "alpine"
        or value.get("alpine_series") != REQUIRED_ALPINE_SERIES
        or not ALPINE_VERSION.fullmatch(str(value.get("alpine_version") or ""))
        or value.get("libc") != "musl"
        or value.get("musl_loader") not in ({SUPPORTED_ARCHITECTURES.get(architecture)} if architecture else set(SUPPORTED_ARCHITECTURES.values()))
    ):
        raise ContractError("base image runtime contract is not Python 3.13 on Alpine 3.22/musl")
    return value


def _inspect(reference: str, docker: str, architecture: str | None = None) -> dict[str, Any]:
    executable = shutil.which(docker) if "/" not in docker else docker
    if not executable:
        raise ContractError(f"container engine is unavailable: {docker}")
    try:
        result = subprocess.run(
            [executable, "image", "inspect", *(["--platform", f"linux/{architecture}"] if architecture else []), reference], text=True,
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise ContractError("base image inspection timed out") from error
    if result.returncode:
        # Older engines use one local architecture per reference and do not
        # accept --platform. Validate their selected architecture explicitly.
        if architecture and "unknown flag: --platform" in result.stderr:
            return _inspect(reference, docker)
        raise ContractError(
            result.stderr.strip() or f"could not inspect the pinned base image {reference}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractError(f"container engine returned invalid image metadata: {error}") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ContractError("container engine returned ambiguous base image metadata")
    return payload[0]


def inspect_platform(reference: str, docker: str, architecture: str) -> dict[str, Any]:
    """Require the requested platform even with a multi-platform image store."""
    inspected = _inspect(reference, docker, architecture)
    if inspected.get("Architecture") != architecture or inspected.get("Os") != "linux":
        raise ContractError("selected base image does not match the requested platform")
    return inspected


def _runtime_contract_from_archive(
    inspected: dict[str, Any], archive_path: Path
) -> dict[str, str]:
    environment = (inspected.get("Config") or {}).get("Env") or []
    python_versions = [
        item.split("=", 1)[1]
        for item in environment
        if isinstance(item, str) and item.startswith("PYTHON_VERSION=")
    ]
    if len(python_versions) != 1 or not PYTHON_VERSION.fullmatch(python_versions[0]):
        raise ContractError("base image is not the required Python 3.13 patch release")
    architecture = str(inspected.get("Architecture") or "")
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise ContractError("unsupported base image architecture")
    loader = SUPPORTED_ARCHITECTURES[architecture]
    wanted = {
        "etc/alpine-release": [],
        loader.lstrip("/"): [],
        "usr/local/bin/python3.13": [],
    }
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive:
                normalized = member.name.removeprefix("./").lstrip("/")
                if normalized in wanted:
                    wanted[normalized].append(member)
            if any(len(items) != 1 for items in wanted.values()):
                raise ContractError("base image runtime files are missing or ambiguous")
            alpine_member = wanted["etc/alpine-release"][0]
            if not alpine_member.isfile() or alpine_member.size > 100:
                raise ContractError("base image Alpine release identity is invalid")
            handle = archive.extractfile(alpine_member)
            if handle is None:
                raise ContractError("base image Alpine release identity is unreadable")
            alpine_version = handle.read(101).decode("ascii", "strict").strip()
    except ContractError:
        raise
    except (OSError, UnicodeError, tarfile.TarError) as error:
        raise ContractError("base image root filesystem could not be inspected safely") from error
    if not ALPINE_VERSION.fullmatch(alpine_version):
        raise ContractError("base image is not the required Alpine 3.22 patch release")
    musl = wanted[loader.lstrip("/")][0]
    python_binary = wanted["usr/local/bin/python3.13"][0]
    if not (musl.isfile() or musl.issym() or musl.islnk()):
        raise ContractError("base image does not contain the target architecture musl loader")
    if not (python_binary.isfile() or python_binary.issym() or python_binary.islnk()):
        raise ContractError("base image does not contain the Python 3.13 runtime")
    return {
        "python_series": REQUIRED_PYTHON_SERIES,
        "python_version": python_versions[0],
        "distribution": "alpine",
        "alpine_series": REQUIRED_ALPINE_SERIES,
        "alpine_version": alpine_version,
        "libc": "musl",
        "musl_loader": loader,
    }


def inspect_runtime_contract(
    reference: str, inspected: dict[str, Any], docker: str
) -> dict[str, str]:
    executable = shutil.which(docker) if "/" not in docker else docker
    if not executable:
        raise ContractError(f"container engine is unavailable: {docker}")
    try:
        created = subprocess.run(
            [executable, "create", "--platform", "linux/" + str(inspected["Architecture"]),
             "--entrypoint", "/bin/true", reference],
            text=True, capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise ContractError("base image snapshot creation timed out") from error
    container_id = created.stdout.strip()
    if created.returncode or not CONTAINER_ID.fullmatch(container_id):
        raise ContractError(created.stderr.strip() or "base image snapshot could not be created")
    cleanup_error = ""
    try:
        with tempfile.TemporaryDirectory(prefix="david-pi-base-") as temporary:
            archive_path = Path(temporary) / "rootfs.tar"
            with archive_path.open("wb") as output:
                try:
                    exported = subprocess.run(
                        [executable, "export", container_id], stdout=output,
                        stderr=subprocess.PIPE, timeout=180,
                    )
                except subprocess.TimeoutExpired as error:
                    raise ContractError("base image root filesystem export timed out") from error
            if exported.returncode:
                message = exported.stderr.decode("utf-8", "replace").strip()
                raise ContractError(message or "base image root filesystem export failed")
            return _runtime_contract_from_archive(inspected, archive_path)
    finally:
        try:
            removed = subprocess.run(
                [executable, "rm", "-f", container_id], text=True,
                capture_output=True, timeout=60,
            )
            if removed.returncode:
                cleanup_error = (
                    removed.stderr.strip() or "temporary base snapshot cleanup failed"
                )
        except subprocess.TimeoutExpired:
            cleanup_error = "temporary base snapshot cleanup timed out"
        if cleanup_error and sys.exc_info()[0] is None:
            raise ContractError(cleanup_error)


def build_evidence(
    reference: str,
    inspected: dict[str, Any],
    dockerfile: Path,
    runtime_contract: dict[str, str],
) -> dict[str, Any]:
    expected_digest = image_digest(reference)
    expected_repository = image_repository(reference)
    if expected_repository != REQUIRED_REPOSITORY:
        raise ContractError("production base must be the Docker Official Image repository python")
    require_runtime_contract(runtime_contract, str(inspected.get("Architecture") or ""))
    repo_digests = inspected.get("RepoDigests") or []
    if not isinstance(repo_digests, list):
        raise ContractError("base image repo digests are malformed")
    matching = []
    for item in repo_digests:
        if not isinstance(item, str):
            continue
        try:
            observed_digest = image_digest(item)
            observed_repository = image_repository(item)
        except ContractError:
            continue
        if observed_digest == expected_digest and observed_repository == expected_repository:
            matching.append(item)
    if not matching:
        raise ContractError("local base image identity does not match the requested immutable digest")
    image_id = inspected.get("Id")
    if not is_sha256_identifier(image_id):
        raise ContractError("base image has no immutable local image ID")
    rootfs_layers = (inspected.get("RootFS") or {}).get("Layers") or []
    if (
        not isinstance(rootfs_layers, list)
        or not rootfs_layers
        or not all(is_sha256_identifier(item) for item in rootfs_layers)
    ):
        raise ContractError("base image has no verifiable root filesystem layer identity")
    architecture = str(inspected.get("Architecture") or "")
    operating_system = str(inspected.get("Os") or "")
    if architecture not in SUPPORTED_ARCHITECTURES or operating_system != REQUIRED_OS:
        raise ContractError(
            "production base image must be linux/amd64 or linux/arm64 for David-Pi"
        )
    source = dockerfile.read_text(encoding="utf-8")
    if "ARG PYTHON_BASE_IMAGE=" not in source or "FROM ${PYTHON_BASE_IMAGE} AS runtime-base" not in source:
        raise ContractError("Dockerfile does not build runtime-base from PYTHON_BASE_IMAGE")
    return {
        "schema_version": 1,
        "kind": "david-pi-base-image-provenance",
        "status": "pass",
        "base_image": {
            "reference": reference,
            "repository": expected_repository,
            "digest": expected_digest,
            "image_id": image_id,
            "matching_repo_digests": sorted(matching),
            "rootfs_layers": rootfs_layers,
            "architecture": architecture,
            "os": operating_system,
            "runtime_contract": runtime_contract,
        },
        "build_contract": {
            "argument": "PYTHON_BASE_IMAGE",
            "dockerfile": dockerfile.name,
            "dockerfile_sha256": file_sha256(dockerfile),
            "verified_locally": True,
        },
    }


def error_evidence(reference: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "david-pi-base-image-provenance",
        "status": "error",
        "base_image": {"reference": reference},
        "error": {"message": message[:4000]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--architecture", choices=sorted(SUPPORTED_ARCHITECTURES))
    parser.add_argument("--dockerfile", type=Path, default=ROOT / "Dockerfile")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    dockerfile = args.dockerfile if args.dockerfile.is_absolute() else ROOT / args.dockerfile
    try:
        inspected = (inspect_platform(args.reference, args.docker, args.architecture)
                     if args.architecture else _inspect(args.reference, args.docker))
        runtime_contract = inspect_runtime_contract(args.reference, inspected, args.docker)
        evidence = build_evidence(args.reference, inspected, dockerfile, runtime_contract)
    except (ContractError, OSError, subprocess.TimeoutExpired) as error:
        write_json_atomic(output, error_evidence(args.reference, str(error)))
        print(f"base image provenance failed closed: {error}; evidence: {output}", file=sys.stderr)
        return 2
    write_json_atomic(output, evidence)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
