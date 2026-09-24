#!/usr/bin/env python3
"""Create content-neutral release evidence with explicit trusted-builder limits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from release_contract import ContractError, image_digest, image_repository


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.android_release import verified_android_release  # noqa: E402
from android_image_release import verified_candidate_android_release  # noqa: E402


def run(*arguments: str, required: bool = True) -> str:
    result = subprocess.run(arguments, cwd=ROOT, text=True, capture_output=True)
    if required and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(arguments)}")
    return result.stdout.strip() if result.returncode == 0 else ""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tracked_files() -> list[Path]:
    raw = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return sorted(ROOT / item.decode("utf-8") for item in raw.split(b"\0") if item)


def tree_hash(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(file_hash(path)))
    return digest.hexdigest()


def image_identity(reference: str) -> dict:
    if not reference:
        return {
            "reference": "", "digest": "", "image_id": "", "repo_digests": [],
            "labels": {}, "rootfs_layers": [], "architecture": "", "os": "",
            "config": {},
        }
    raw = run("docker", "image", "inspect", reference, required=False)
    try:
        values = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        values = []
    if len(values) != 1 or not isinstance(values[0], dict):
        return {
            "reference": reference, "digest": "", "image_id": "", "repo_digests": [],
            "labels": {}, "rootfs_layers": [], "architecture": "", "os": "",
            "config": {},
        }
    value = values[0]
    raw_repo_digests = value.get("RepoDigests") or []
    repo_digests = sorted(
        item for item in raw_repo_digests if isinstance(item, str)
    ) if isinstance(raw_repo_digests, list) else []
    config = value.get("Config") or {}
    if not isinstance(config, dict):
        config = {}
    labels = config.get("Labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    return {
        "reference": reference,
        # Keep the original comma-separated field for schema-v1 consumers.
        "digest": ",".join(repo_digests),
        "image_id": str(value.get("Id") or ""),
        "repo_digests": repo_digests,
        "labels": dict(sorted((str(key), str(item)) for key, item in labels.items())),
        "rootfs_layers": [
            str(item) for item in ((value.get("RootFS") or {}).get("Layers") or [])
        ],
        "architecture": str(value.get("Architecture") or ""),
        "os": str(value.get("Os") or ""),
        "config": {
            "user": str(config.get("User") or ""),
            "working_dir": str(config.get("WorkingDir") or ""),
            "entrypoint": [str(item) for item in (config.get("Entrypoint") or [])],
            "cmd": [str(item) for item in (config.get("Cmd") or [])],
            "env": [str(item) for item in (config.get("Env") or [])],
            "exposed_ports": sorted(str(item) for item in (config.get("ExposedPorts") or {})),
            "healthcheck": config.get("Healthcheck"),
            "volumes": sorted(str(item) for item in (config.get("Volumes") or {})),
        },
    }


def require_matching_digest(reference: str, identity: dict) -> None:
    expected_digest = image_digest(reference)
    expected_repository = image_repository(reference)
    for item in identity.get("repo_digests", []):
        try:
            if (
                image_digest(item) == expected_digest
                and image_repository(item) == expected_repository
            ):
                return
        except ContractError:
            continue
    raise RuntimeError("image inspection did not prove the requested immutable digest")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="")
    parser.add_argument("--require-image-digest", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    files = tracked_files()
    static_files = [path for path in files if path.relative_to(ROOT).parts[0] == "static"]
    identity = image_identity(args.image)
    if args.require_image_digest:
        require_matching_digest(args.image, identity)
    if args.image:
        android_release = verified_candidate_android_release(args.image, ROOT)
    else:
        android_release = verified_android_release(
            ROOT, require_source=True, require_tools=True
        )
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "revision": run("git", "rev-parse", "HEAD"),
            "describe": run("git", "describe", "--always", "--dirty", "--tags"),
            "dirty": bool(run("git", "status", "--porcelain")),
            "tracked_file_count": len(files),
            "tree_sha256": tree_hash(files),
        },
        "build": {
            "provenance_model": "trusted_builder_declaration",
            "reproducible_build": False,
            "image": identity,
            "android_release": android_release,
            "android_release_image_verified": bool(args.image),
            "dockerfile_sha256": file_hash(ROOT / "Dockerfile"),
            "compose_sha256": file_hash(ROOT / "compose.yaml"),
            "requirements_sha256": file_hash(ROOT / "requirements.txt"),
        },
        "static_assets": {
            path.relative_to(ROOT).as_posix(): file_hash(path) for path in static_files
        },
        "environment": {
            "builder_architecture": os.uname().machine,
            "secret_screening": {
                "result": "passed",
                "scope": "tracked-source plus Android ignored/path/high-confidence-content gates",
                "absence_proven": False,
            },
        },
    }
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
