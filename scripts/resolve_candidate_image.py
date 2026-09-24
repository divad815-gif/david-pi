#!/usr/bin/env python3
"""Bind a clean Buildx push result to the immutable image inspected afterward."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from release_contract import ContractError, image_digest, image_repository, is_sha256_identifier


def load_build_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError("candidate builder metadata is unavailable or invalid") from error
    if not isinstance(value, dict):
        raise ContractError("candidate builder metadata must be a JSON object")
    digest = value.get("containerimage.digest")
    config_digest = value.get("containerimage.config.digest")
    descriptor = value.get("containerimage.descriptor")
    if not is_sha256_identifier(digest) or not is_sha256_identifier(config_digest):
        raise ContractError("candidate builder metadata lacks immutable digests")
    if not isinstance(descriptor, dict) or descriptor.get("digest") != digest:
        raise ContractError("candidate builder descriptor differs from its pushed digest")
    return value


def resolve_candidate(*, metadata: Path, repository: str) -> str:
    try:
        canonical_repository = image_repository(
            f"{repository}@sha256:{'0' * 64}"
        )
    except ContractError as error:
        raise ContractError("candidate repository is invalid") from error
    if canonical_repository != repository:
        raise ContractError("candidate repository is not canonical")
    value = load_build_metadata(metadata)
    return f"{repository}@{value['containerimage.digest']}"


def _inspect(reference: str, docker: str) -> dict[str, Any]:
    executable = shutil.which(docker) if "/" not in docker else docker
    if not executable:
        raise ContractError("container engine is unavailable")
    try:
        result = subprocess.run(
            [executable, "image", "inspect", reference], text=True,
            capture_output=True, timeout=60,
        )
    except subprocess.TimeoutExpired as error:
        raise ContractError("candidate image inspection timed out") from error
    if result.returncode:
        raise ContractError(result.stderr.strip() or "candidate image is unavailable")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractError("container engine returned invalid candidate metadata") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise ContractError("container engine returned ambiguous candidate metadata")
    return values[0]


def verify_candidate(
    *, metadata: Path, candidate: str, revision: str, docker: str = "docker"
) -> None:
    builder = load_build_metadata(metadata)
    expected_digest = image_digest(candidate)
    if builder["containerimage.digest"] != expected_digest:
        raise ContractError("candidate reference differs from the builder-returned digest")
    value = _inspect(candidate, docker)
    if value.get("Id") != builder["containerimage.config.digest"]:
        raise ContractError("pulled candidate config differs from builder metadata")
    if value.get("Os") != "linux" or value.get("Architecture") != "arm64":
        raise ContractError("candidate image is not linux/arm64")
    labels = ((value.get("Config") or {}).get("Labels") or {})
    if labels.get("org.opencontainers.image.revision") != revision:
        raise ContractError("candidate image revision differs from the exact checkout")
    observed = value.get("RepoDigests") or []
    if not isinstance(observed, list) or candidate not in observed:
        raise ContractError("pulled candidate does not expose its exact repository digest")
    layers = ((value.get("RootFS") or {}).get("Layers") or [])
    if not isinstance(layers, list) or not layers or not all(
        is_sha256_identifier(layer) for layer in layers
    ):
        raise ContractError("pulled candidate has no immutable root filesystem identity")


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--metadata", type=Path, required=True)
    resolve.add_argument("--repository", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--metadata", type=Path, required=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--revision", required=True)
    verify.add_argument("--docker", default="docker")
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            print(resolve_candidate(metadata=args.metadata, repository=args.repository))
        else:
            verify_candidate(
                metadata=args.metadata,
                candidate=args.candidate,
                revision=args.revision,
                docker=args.docker,
            )
            print("candidate builder digest and pulled root filesystem verified")
    except ContractError as error:
        parser.exit(2, f"candidate resolution failed closed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
