#!/usr/bin/env python3
"""Verify the Android release bundle stored inside a stopped candidate image."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import marshal
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from modules.android_release import (
    AndroidReleaseError,
    EXPECTED_ARTIFACT_PATH,
    EXPECTED_ATTESTATION_PATH,
    file_sha256,
    verified_android_release,
)


CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
POLICY_PATH = "config/android-release.json"
IMAGE_ROOT = Path("/app")
IMAGE_ENTRYPOINT = Path("/usr/local/bin/docker-entrypoint")
RUNTIME_SOURCE_FILES = {
    "app.py": "app.py",
    "requirements.txt": "requirements.txt",
    EXPECTED_ARTIFACT_PATH: EXPECTED_ARTIFACT_PATH,
    EXPECTED_ATTESTATION_PATH: EXPECTED_ATTESTATION_PATH,
}
RUNTIME_SOURCE_TREES = ("modules", "config", "installer", "templates", "static", "assets", "knowledge")
RUNTIME_ENTRYPOINT = "docker-entrypoint.sh"
EXPECTED_IMAGE_ENTRYPOINT = ["/usr/local/bin/docker-entrypoint"]
EXPECTED_IMAGE_CMD = [
    "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2",
    "--timeout", "600", "--worker-tmp-dir", "/dev/shm", "app:app",
]
EXPECTED_IMAGE_ENV = {
    "PATH": "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PHOTO_DATA": "/data",
    "TMPDIR": "/data/tmp/uploads",
    "XDG_CACHE_HOME": "/data/tmp/runtime",
}
EXPECTED_BASE_ENV_KEYS = {"GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256"}


def _docker(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments], text=True, capture_output=True, check=False
        )
    except OSError as error:
        raise AndroidReleaseError("Docker is unavailable for candidate bundle inspection") from error
    if result.returncode:
        raise AndroidReleaseError("Docker could not inspect the candidate Android bundle")
    return result.stdout.strip()


def _regular_file_fingerprint(path: Path, label: str) -> tuple[str, int]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise AndroidReleaseError(f"This platform cannot safely inspect {label}")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise AndroidReleaseError(f"{label} is missing or unsafe") from error
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise AndroidReleaseError(f"{label} is missing or unsafe")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        )
        if identity_before != identity_after or size != before.st_size:
            raise AndroidReleaseError(f"{label} changed during inspection")
    except OSError as error:
        raise AndroidReleaseError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    return digest.hexdigest(), size


def _tree_fingerprints(
    tree: Path, *, label: str, omit_python_cache: bool
) -> dict[str, tuple[str, int]]:
    if not tree.is_dir() or tree.is_symlink():
        raise AndroidReleaseError(f"{label} is missing or unsafe")

    def walk_error(error: OSError) -> None:
        raise AndroidReleaseError(f"{label} is unreadable") from error

    fingerprints: dict[str, tuple[str, int]] = {}
    for directory, child_directories, filenames in os.walk(
        tree, topdown=True, onerror=walk_error, followlinks=False
    ):
        parent = Path(directory)
        retained: list[str] = []
        for name in child_directories:
            child = parent / name
            if child.is_symlink():
                raise AndroidReleaseError(f"{label} contains a symlink")
            if omit_python_cache and name == "__pycache__":
                continue
            retained.append(name)
        child_directories[:] = retained
        for name in filenames:
            child = parent / name
            if child.is_symlink() or not child.is_file():
                raise AndroidReleaseError(f"{label} contains an unsafe file")
            if omit_python_cache and child.suffix.casefold() in {".pyc", ".pyo", ".pyd"}:
                continue
            relative = child.relative_to(tree).as_posix()
            fingerprints[relative] = _regular_file_fingerprint(child, label)
    return fingerprints


def _expected_runtime_source(source_root: Path) -> dict[str, tuple[str, int]]:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            text=True, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AndroidReleaseError(
            "Git is unavailable for the runtime source inventory"
        ) from error
    if top_level.returncode or Path(top_level.stdout.strip()).resolve() != source_root:
        raise AndroidReleaseError("Runtime source is not an exact Git worktree root")
    pathspecs = [
        *RUNTIME_SOURCE_FILES.keys(), RUNTIME_ENTRYPOINT, *RUNTIME_SOURCE_TREES,
    ]
    try:
        listed = subprocess.run(
            ["git", "-C", str(source_root), "ls-files", "-z", "--", *pathspecs],
            capture_output=True, timeout=30,
        )
        tracked = {
            item.decode("utf-8", "strict")
            for item in listed.stdout.split(b"\0")
            if item
        }
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise AndroidReleaseError("Tracked runtime source inventory is unavailable") from error
    if listed.returncode:
        raise AndroidReleaseError("Tracked runtime source inventory is unavailable")
    # APK and attestation are intentionally ignored build assets. The signed
    # release verifier authenticates them separately; application source is tracked.
    required = (set(RUNTIME_SOURCE_FILES) - {EXPECTED_ARTIFACT_PATH, EXPECTED_ATTESTATION_PATH}) | {RUNTIME_ENTRYPOINT}
    if not required.issubset(tracked):
        raise AndroidReleaseError("A required runtime source is not tracked by Git")

    expected: dict[str, tuple[str, int]] = {}
    for source_relative, image_relative in RUNTIME_SOURCE_FILES.items():
        expected[image_relative] = _regular_file_fingerprint(
            source_root / source_relative, f"Host runtime source {source_relative}"
        )
    for relative in RUNTIME_SOURCE_TREES:
        observed = _tree_fingerprints(
            source_root / relative,
            label=f"Host runtime source tree {relative}",
            omit_python_cache=True,
        )
        # The Docker build deliberately excludes installer/tests/. Keep this
        # exact omission aligned with .dockerignore, while still rejecting any
        # unexpected test files found inside the runtime image itself.
        if relative == "installer":
            observed = {name: value for name, value in observed.items() if not name.startswith("tests/")}
        observed_paths = {f"{relative}/{child}" for child in observed}
        tracked_paths = {
            path for path in tracked if path.startswith(f"{relative}/") and not path.startswith("installer/tests/")
        }
        if observed_paths != tracked_paths:
            raise AndroidReleaseError(
                f"Host runtime source tree {relative} contains files not tracked by Git"
            )
        for child, fingerprint in observed.items():
            expected[f"{relative}/{child}"] = fingerprint
    return expected


def _verified_generated_cache(root: Path, files: dict) -> set[str]:
    """Allow only bytecode generated from the accompanying, separately checked source.

    The Dockerfile compiles /app with Python 3.13. Inspectors must use that same
    Python bytecode version; unknown/orphan caches fail rather than being hidden.
    No bytecode is executed during this check.
    """
    generated = set()
    for relative in files:
        path = Path(relative)
        if "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo", ".pyd"}:
            continue
        suffix = "." + sys.implementation.cache_tag + ".pyc"
        if path.parent.name != "__pycache__" or not path.name.endswith(suffix):
            raise AndroidReleaseError("Candidate has an unsupported generated Python cache")
        source = path.parent.parent / (path.name.removesuffix(suffix) + ".py")
        if source.as_posix() not in files:
            raise AndroidReleaseError("Candidate has an orphan generated Python cache")
        payload = (root / path).read_bytes()
        try:
            if payload[:4] != importlib.util.MAGIC_NUMBER or len(payload) < 17:
                raise ValueError("bytecode version differs")
            expected = compile((root / source).read_bytes(), "/app/" + source.as_posix(), "exec", dont_inherit=True, optimize=0)
            if marshal.loads(payload[16:]) != expected:
                raise ValueError("compiled code differs")
        except (ValueError, TypeError, EOFError, SyntaxError) as error:
            raise AndroidReleaseError("Candidate generated Python cache differs from its source") from error
        generated.add(relative)
    return generated


def verify_image_runtime_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise AndroidReleaseError("Candidate image runtime config is missing")
    if config.get("user") != "10001:10001":
        raise AndroidReleaseError("Candidate image user is unsafe")
    if config.get("working_dir") != "/app":
        raise AndroidReleaseError("Candidate image working directory differs")
    if config.get("entrypoint") != EXPECTED_IMAGE_ENTRYPOINT:
        raise AndroidReleaseError("Candidate image entrypoint differs")
    if config.get("cmd") != EXPECTED_IMAGE_CMD:
        raise AndroidReleaseError("Candidate image command differs")
    if config.get("exposed_ports") != ["8000/tcp"]:
        raise AndroidReleaseError("Candidate image ports differ")
    if config.get("healthcheck") is not None:
        raise AndroidReleaseError("Candidate image must not define a healthcheck command")
    if config.get("volumes") != []:
        raise AndroidReleaseError("Candidate image must not declare volumes")
    raw_environment = config.get("env")
    if not (
        isinstance(raw_environment, list)
        and all(isinstance(item, str) and "=" in item for item in raw_environment)
    ):
        raise AndroidReleaseError("Candidate image environment is malformed")
    environment: dict[str, str] = {}
    for item in raw_environment:
        name, value = item.split("=", 1)
        if not name or name in environment:
            raise AndroidReleaseError("Candidate image environment is ambiguous")
        environment[name] = value
    if set(environment) != set(EXPECTED_IMAGE_ENV) | EXPECTED_BASE_ENV_KEYS:
        raise AndroidReleaseError("Candidate image environment has unexpected settings")
    if any(environment.get(name) != value for name, value in EXPECTED_IMAGE_ENV.items()):
        raise AndroidReleaseError("Candidate image environment differs")
    if not (
        re.fullmatch(r"[0-9A-F]{40}", environment.get("GPG_KEY", ""))
        and re.fullmatch(r"3\.13\.[0-9]+", environment.get("PYTHON_VERSION", ""))
        and re.fullmatch(r"[0-9a-f]{64}", environment.get("PYTHON_SHA256", ""))
    ):
        raise AndroidReleaseError("Candidate base image environment is invalid")


def _inspect_candidate_runtime_config(image: str) -> dict:
    try:
        values = json.loads(_docker("image", "inspect", image))
    except json.JSONDecodeError as error:
        raise AndroidReleaseError("Docker returned malformed candidate image config") from error
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
        raise AndroidReleaseError("Docker returned invalid candidate image config")
    raw = values[0].get("Config")
    if not isinstance(raw, dict):
        raise AndroidReleaseError("Candidate image runtime config is missing")
    config = {
        "user": str(raw.get("User") or ""),
        "working_dir": str(raw.get("WorkingDir") or ""),
        "entrypoint": [str(item) for item in (raw.get("Entrypoint") or [])],
        "cmd": [str(item) for item in (raw.get("Cmd") or [])],
        "env": [str(item) for item in (raw.get("Env") or [])],
        "exposed_ports": sorted(str(item) for item in (raw.get("ExposedPorts") or {})),
        "healthcheck": raw.get("Healthcheck"),
        "volumes": sorted(str(item) for item in (raw.get("Volumes") or {})),
    }
    verify_image_runtime_config(config)
    return config


def verified_candidate_android_release(image: str, source_root: Path) -> dict:
    """Compare a candidate's stopped-container bundle to verified host evidence.

    ``docker create`` and ``docker cp`` inspect the filesystem without executing
    the candidate, so this works for cross-architecture production images.
    """
    if not isinstance(image, str) or not image.strip():
        raise AndroidReleaseError("Candidate image is required for Android bundle inspection")
    source_root = source_root.resolve()
    expected = verified_android_release(
        source_root, require_source=True, require_tools=True
    )
    expected_policy = source_root / POLICY_PATH
    if not expected_policy.is_file() or expected_policy.is_symlink():
        raise AndroidReleaseError("Host Android release policy is missing or unsafe")

    _inspect_candidate_runtime_config(image)

    container_id = _docker("create", image)
    if not CONTAINER_ID.fullmatch(container_id):
        raise AndroidReleaseError("Docker returned an invalid candidate container identity")
    cleanup_error: AndroidReleaseError | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="david-pi-android-image-") as temporary:
            extracted_root = Path(temporary)
            extracted_app = extracted_root / "image-app"
            _docker(
                "cp",
                f"{container_id}:{IMAGE_ROOT.as_posix()}",
                str(extracted_app),
            )
            extracted_entrypoint = extracted_root / "docker-entrypoint"
            _docker(
                "cp",
                f"{container_id}:{IMAGE_ENTRYPOINT.as_posix()}",
                str(extracted_entrypoint),
            )
            candidate_runtime = _tree_fingerprints(
                extracted_app,
                label="Candidate runtime source tree",
                omit_python_cache=False,
            )
            legacy_static = extracted_app / "static" / "apk"
            if os.path.lexists(legacy_static):
                raise AndroidReleaseError("Candidate retains a legacy public APK path")
            if any(
                Path(relative).suffix.casefold() == ".apk"
                for relative in candidate_runtime
                if Path(relative).parts[:1] == ("static",)
            ):
                raise AndroidReleaseError("Candidate retains a public APK artifact")
            expected_runtime = _expected_runtime_source(source_root)
            for generated in _verified_generated_cache(extracted_app, candidate_runtime):
                del candidate_runtime[generated]
            if candidate_runtime != expected_runtime:
                raise AndroidReleaseError(
                    "Candidate runtime source differs from the verified host source"
                )
            if _regular_file_fingerprint(
                extracted_entrypoint, "Candidate runtime entrypoint"
            ) != _regular_file_fingerprint(
                source_root / "docker-entrypoint.sh", "Host runtime entrypoint"
            ):
                raise AndroidReleaseError(
                    "Candidate runtime entrypoint differs from the verified host source"
                )
            if file_sha256(extracted_app / POLICY_PATH) != file_sha256(expected_policy):
                raise AndroidReleaseError(
                    "Candidate Android release policy differs from the source policy"
                )
            observed = verified_android_release(
                extracted_app, require_source=False, require_tools=False
            )
            if observed != expected:
                raise AndroidReleaseError(
                    "Candidate Android release bundle differs from verified source evidence"
                )
            return observed
    finally:
        try:
            _docker("rm", "-f", "-v", container_id)
        except AndroidReleaseError as error:
            cleanup_error = error
        if cleanup_error is not None:
            raise AndroidReleaseError(
                "Candidate Android inspection container could not be removed"
            ) from cleanup_error
