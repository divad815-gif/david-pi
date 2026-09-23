"""Fail-closed Android release artifact provenance and runtime validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any


SHA256 = re.compile(r"[0-9a-f]{64}")
GRADLE_APPLICATION_ID = re.compile(r'^\s*applicationId\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
GRADLE_VERSION_CODE = re.compile(r"^\s*versionCode\s*=\s*([1-9][0-9]*)\s*$", re.MULTILINE)
GRADLE_VERSION_NAME = re.compile(r'^\s*versionName\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
BADGING = re.compile(
    r"^package: name='([^']+)' versionCode='([1-9][0-9]*)' versionName='([^']+)'",
    re.MULTILINE,
)
SIGNER_DIGEST = re.compile(r"^Signer #1 certificate SHA-256 digest: ([0-9a-f]{64})$", re.MULTILINE)
SIGNER_COUNT = re.compile(r"^Number of signers: ([0-9]+)$", re.MULTILINE)
SCHEME = re.compile(
    r"^Verified using (v1|v2|v3|v3\.1|v4) scheme.*: (true|false)$", re.MULTILINE
)
RELEASE_SOURCE_METADATA = "com.davidpi.backup.RELEASE_SOURCE_SHA256"
FIREBASE_STATE_METADATA = "com.davidpi.backup.RELEASE_FIREBASE_CLIENT_STATE"
FIREBASE_UNCONFIGURED = "unconfigured"
MANIFEST_STRING_ATTRIBUTE = re.compile(
    r'^\s*A:\s+android:(name|value)(?:\([^)]*\))?\s*=\s*"([^"]*)"(?:\s|$)'
)
ANDROID_SOURCE_ROOT = Path("clients/android")
EXPECTED_ARTIFACT_PATH = "artifacts/android/david-pi-backup.apk"
EXPECTED_ATTESTATION_PATH = "artifacts/android/david-pi-backup.manifest.json"
EXPECTED_SOURCE_BUILD_PATH = "clients/android/app/build.gradle.kts"
EXCLUDED_ANDROID_OUTPUT_ROOTS = {
    Path(".gradle"),
    Path(".kotlin"),
    Path("build"),
    Path("app/build"),
}
FORBIDDEN_ANDROID_FILES = {
    "google-services.json",
    "keystore.properties",
    "local.properties",
}
FORBIDDEN_ANDROID_SUFFIXES = {
    ".bks", ".jceks", ".jks", ".key", ".keystore", ".p8", ".p12", ".pem",
    ".pfx", ".pk8", ".pkcs8", ".secret",
}
FORBIDDEN_ANDROID_DIRECTORIES = {"secret", "secrets"}
FIREBASE_INIT_PROVIDER = "com.google.firebase.provider.FirebaseInitProvider"
FIREBASE_RESOURCE_NAMES = {
    "default_web_client_id",
    "firebase_database_url",
    "gcm_defaultSenderId",
    "google_api_key",
    "google_app_id",
    "google_storage_bucket",
    "project_id",
}
TOOL_TIMEOUT_SECONDS = 30
ANDROID_BUILD_TOOLS_VERSION = "35.0.0"
MAX_JAVA_RUNTIME_FILES = 512
MAX_JAVA_RUNTIME_BYTES = 512 * 1024 * 1024
SECRET_SCAN_CHUNK_BYTES = 1024 * 1024
SECRET_SCAN_OVERLAP_BYTES = 4096
SECRET_TOKEN_PATTERNS = (
    re.compile(br"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(br"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(br"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(br"\bgh[pousr]_[0-9A-Za-z]{20,255}\b"),
    re.compile(br"\bxox[baprs]-[0-9A-Za-z-]{20,255}\b"),
)
SECRET_ASSIGNMENT = re.compile(
    br'''(?im)^\s*["']?(?:storePassword|keyPassword|client_secret|api_key|'''
    br'''access_token|private_key)["']?\s*[:=]\s*["']?([^\s,"'#}]{8,})'''
)
KOTLIN_LITERAL_SECRET_ASSIGNMENT = re.compile(
    br'''(?im)^\s*(?:(?:val|var)\s+)?(?:storePassword|keyPassword|client_secret|'''
    br'''api_key|access_token|private_key)\s*=\s*["']([^"'\r\n]{8,})["']'''
    br'''\s*[,;]?\s*(?://.*)?$'''
)
SECRET_PLACEHOLDERS = {
    b"change-me", b"changeme", b"example", b"not-configured", b"placeholder",
    b"replace-me", b"unconfigured",
}


class AndroidReleaseError(RuntimeError):
    """The Android release artifact or its provenance is incomplete or inconsistent."""


def available_android_release(app_root: Path, logger):
    """Return only a hash-bound, policy-matching APK release for the portal."""
    try:
        return verified_android_release(
            app_root, require_source=False, require_tools=False
        )
    except Exception as error:
        logger.warning("Android release download disabled: %s", error)
        return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise AndroidReleaseError(f"{label} is missing or invalid")
    try:
        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise AndroidReleaseError(f"{label} contains duplicate JSON keys")
                value[key] = item
            return value

        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except AndroidReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AndroidReleaseError(f"{label} is missing or invalid") from error
    if not isinstance(value, dict):
        raise AndroidReleaseError(f"{label} must be a JSON object")
    return value


def _safe_relative(root: Path, value: Any, label: str) -> tuple[str, Path]:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AndroidReleaseError(f"{label} path is invalid")
    relative = Path(value)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise AndroidReleaseError(f"{label} path is invalid")
    resolved_root = root.resolve()
    lexical = resolved_root / relative
    resolved = lexical.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise AndroidReleaseError(f"{label} escapes the source root")
    return relative.as_posix(), lexical


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise AndroidReleaseError(f"{label} is not a lowercase SHA-256 digest")
    return value


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_uint(value: Any, label: str, *, minimum: int = 0) -> int:
    if not _plain_int(value) or value < minimum or value > (2**64 - 1):
        raise AndroidReleaseError(f"{label} is invalid")
    return value


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise AndroidReleaseError(f"{label} fields are invalid")
    return value


def source_version(build_file: Path) -> dict[str, Any]:
    if not build_file.is_file() or build_file.is_symlink():
        raise AndroidReleaseError("Android source build file is unavailable")
    try:
        text = build_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AndroidReleaseError("Android source build file is unavailable") from error
    matches = {
        "application_id": GRADLE_APPLICATION_ID.findall(text),
        "version_code": GRADLE_VERSION_CODE.findall(text),
        "version_name": GRADLE_VERSION_NAME.findall(text),
    }
    if any(len(values) != 1 for values in matches.values()):
        raise AndroidReleaseError("Android source version declarations must be unique literals")
    return {
        "application_id": matches["application_id"][0],
        "version_code": int(matches["version_code"][0]),
        "version_name": matches["version_name"][0],
    }


def _included_android_path(relative: Path) -> bool:
    if not relative.parts or relative.parts[:2] != ANDROID_SOURCE_ROOT.parts:
        return False
    within_android = relative.relative_to(ANDROID_SOURCE_ROOT)
    return not any(
        within_android.parts[:len(output.parts)] == output.parts
        for output in EXCLUDED_ANDROID_OUTPUT_ROOTS
    )


def _android_source_paths(root: Path) -> list[Path]:
    android_root = root / ANDROID_SOURCE_ROOT
    if not android_root.is_dir() or android_root.is_symlink():
        raise AndroidReleaseError("Android release source root is missing or unsafe")
    relative_paths: list[Path] = []
    def walk_error(error: OSError) -> None:
        raise AndroidReleaseError("Android release source tree is unreadable") from error

    for directory, child_directories, filenames in os.walk(
        android_root, topdown=True, onerror=walk_error, followlinks=False
    ):
        parent = Path(directory)
        retained: list[str] = []
        for name in child_directories:
            child = parent / name
            if child.is_symlink():
                raise AndroidReleaseError("Android release source contains a symlink directory")
            if name.casefold() in FORBIDDEN_ANDROID_DIRECTORIES:
                raise AndroidReleaseError("Android release source contains a secret directory")
            within_android = child.relative_to(android_root)
            if within_android in EXCLUDED_ANDROID_OUTPUT_ROOTS:
                continue
            retained.append(name)
        child_directories[:] = retained
        for name in filenames:
            path = parent / name
            relative = path.relative_to(root)
            within_android = relative.relative_to(ANDROID_SOURCE_ROOT)
            lowered_name = within_android.name.casefold()
            if (
                lowered_name in FORBIDDEN_ANDROID_FILES
                or lowered_name == ".env"
                or lowered_name.startswith(".env.")
                or within_android.suffix.lower() in FORBIDDEN_ANDROID_SUFFIXES
            ):
                raise AndroidReleaseError(
                    f"machine-local Android signing input is present: {within_android.as_posix()}"
                )
            if not path.is_file() or path.is_symlink():
                raise AndroidReleaseError("Android release source contains an unsafe input")
            relative_paths.append(relative)
    included = sorted(
        (path for path in relative_paths if _included_android_path(path)),
        key=lambda path: path.as_posix(),
    )
    if not included:
        raise AndroidReleaseError("Android release source inventory is empty")
    return included


def _reject_ignored_android_inputs(root: Path, included: list[Path]) -> None:
    """Reject ignored build inputs that ordinary tracked-file scans cannot see."""
    if not (root / ".git").exists():
        return
    payload = b"\0".join(path.as_posix().encode("utf-8") for path in included) + b"\0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-z", "--stdin"],
            input=payload,
            capture_output=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AndroidReleaseError("Android ignored-input policy could not run") from error
    if result.returncode == 0:
        raise AndroidReleaseError("Android release source contains an ignored build input")
    if result.returncode != 1:
        raise AndroidReleaseError("Android ignored-input policy could not run")


def _buffer_contains_credential(
    data: bytes, *, assignments: bool, literal_assignments: bool = False
) -> bool:
    if any(pattern.search(data) for pattern in SECRET_TOKEN_PATTERNS):
        return True
    if not assignments:
        if not literal_assignments:
            return False
        for match in KOTLIN_LITERAL_SECRET_ASSIGNMENT.finditer(data):
            value = match.group(1).strip().lower()
            if value not in SECRET_PLACEHOLDERS and not value.startswith(
                (b"${", b"<", b"your-")
            ):
                return True
        return False
    for match in SECRET_ASSIGNMENT.finditer(data):
        value = match.group(1).strip().lower()
        if value not in SECRET_PLACEHOLDERS and not value.startswith((b"${", b"<", b"your-")):
            return True
    return False


def _stream_contains_credential(
    handle, *, assignments: bool, literal_assignments: bool = False
) -> bool:
    overlap = b""
    while True:
        block = handle.read(SECRET_SCAN_CHUNK_BYTES)
        if not block:
            return False
        combined = overlap + block
        if _buffer_contains_credential(
            combined,
            assignments=assignments,
            literal_assignments=literal_assignments,
        ):
            return True
        overlap = combined[-SECRET_SCAN_OVERLAP_BYTES:]


def _source_contains_credential(path: Path) -> bool:
    assignments = path.suffix.casefold() not in {".java", ".kt", ".kts"}
    literal_assignments = path.suffix.casefold() in {".java", ".kt", ".kts"}
    try:
        with path.open("rb") as handle:
            return _stream_contains_credential(
                handle,
                assignments=assignments,
                literal_assignments=literal_assignments,
            )
    except OSError as error:
        raise AndroidReleaseError("Android release source input is unreadable") from error


def android_source_inventory(root: Path) -> dict[str, Any]:
    """Hash every build-relevant Android input, including untracked files."""
    root = root.resolve()
    included = _android_source_paths(root)
    _reject_ignored_android_inputs(root, included)
    files: list[dict[str, Any]] = []
    tree_digest = hashlib.sha256()
    for relative in included:
        path = root / relative
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise AndroidReleaseError("Android release source contains an unsafe input")
        if _source_contains_credential(path):
            raise AndroidReleaseError(
                f"Android release source contains credential-like content: {relative.as_posix()}"
            )
        try:
            digest = file_sha256(path)
            size = _require_uint(
                path.stat().st_size, "Android source input size"
            )
        except OSError as error:
            raise AndroidReleaseError("Android release source input is unreadable") from error
        encoded = relative.as_posix().encode("utf-8")
        tree_digest.update(len(encoded).to_bytes(4, "big"))
        tree_digest.update(encoded)
        tree_digest.update(size.to_bytes(8, "big"))
        tree_digest.update(bytes.fromhex(digest))
        files.append({"path": relative.as_posix(), "sha256": digest, "size_bytes": size})
    return {
        "root": ANDROID_SOURCE_ROOT.as_posix(),
        "file_count": len(files),
        "tree_sha256": tree_digest.hexdigest(),
        "files": files,
    }


def _tool_candidates(name: str, build_tools_version: str) -> list[Path]:
    environment_name = f"ANDROID_{name.upper()}"
    values: list[Path] = []
    configured = os.environ.get(environment_name, "").strip()
    if configured:
        values.append(Path(configured))
    for variable in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        sdk = os.environ.get(variable, "").strip()
        if not sdk:
            continue
        values.append(Path(sdk) / "build-tools" / build_tools_version / name)
    discovered = shutil.which(name)
    if discovered:
        values.append(Path(discovered))
    return values


def locate_android_tool(name: str, build_tools_version: str) -> Path:
    for candidate in _tool_candidates(name, build_tools_version):
        if candidate.is_file() and not candidate.is_symlink() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise AndroidReleaseError(
        f"Android release verification requires {name}; configure ANDROID_{name.upper()}"
    )


def locate_java_runtime() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("ANDROID_JAVA", "").strip()
    if configured:
        candidates.append(Path(configured))
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if java_home:
        candidates.append(Path(java_home) / "bin" / "java")
    # This fixed fallback never consults ambient PATH. Its resolved executable
    # still has to match the authenticated policy before verifier output counts.
    candidates.append(Path("/usr/bin/java"))
    for candidate in candidates:
        if candidate.is_symlink():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and not resolved.is_symlink() and os.access(resolved, os.X_OK):
            return resolved
    raise AndroidReleaseError(
        "Android release verification requires an absolute Java runtime; configure ANDROID_JAVA"
    )


def java_runtime_inventory(java_runtime: Path) -> dict[str, Any]:
    """Bind every executable/runtime input under a canonical JDK's bin/conf/lib."""
    try:
        java = java_runtime.resolve(strict=True)
        java_home = java.parent.parent.resolve(strict=True)
    except OSError as error:
        raise AndroidReleaseError("Java runtime inventory is unavailable") from error
    if (
        java != java_home / "bin" / "java"
        or java.is_symlink()
        or not java.is_file()
    ):
        raise AndroidReleaseError("Java runtime layout is unsupported")
    relative_files: list[Path] = []

    def walk_error(error: OSError) -> None:
        raise AndroidReleaseError("Java runtime inventory is unreadable") from error

    for root_name in ("bin", "conf", "lib"):
        root = java_home / root_name
        if not root.is_dir() or root.is_symlink():
            raise AndroidReleaseError("Java runtime inventory is incomplete")
        for directory, child_directories, filenames in os.walk(
            root, topdown=True, onerror=walk_error, followlinks=False
        ):
            parent = Path(directory)
            for name in child_directories:
                if (parent / name).is_symlink():
                    raise AndroidReleaseError("Java runtime inventory contains a symlink")
            for name in filenames:
                path = parent / name
                if not path.is_file() or path.is_symlink():
                    raise AndroidReleaseError("Java runtime inventory contains an unsafe file")
                resolved = path.resolve()
                if not resolved.is_relative_to(java_home):
                    raise AndroidReleaseError("Java runtime inventory escapes its root")
                relative_files.append(path.relative_to(java_home))
    relative_files.sort(key=lambda item: item.as_posix())
    if not relative_files or len(relative_files) > MAX_JAVA_RUNTIME_FILES:
        raise AndroidReleaseError("Java runtime inventory exceeds its safe file limit")
    sizes: dict[Path, int] = {}
    total_size = 0
    for relative in relative_files:
        try:
            size = _require_uint(
                (java_home / relative).stat().st_size, "Java runtime input size"
            )
            total_size += size
        except OSError as error:
            raise AndroidReleaseError("Java runtime inventory is unreadable") from error
        if total_size > MAX_JAVA_RUNTIME_BYTES:
            raise AndroidReleaseError("Java runtime inventory exceeds its safe byte limit")
        sizes[relative] = size
    digest = hashlib.sha256()
    for relative in relative_files:
        path = java_home / relative
        size = sizes[relative]
        content_hash = file_sha256(path)
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(content_hash))
    return {
        "root_path": str(java_home),
        "file_count": len(relative_files),
        "tree_sha256": digest.hexdigest(),
    }


def _snapshot_java_runtime(
    java_runtime: Path,
    destination: Path,
    *,
    expected: dict[str, Any] | None = None,
    mismatch_label: str = "Android Java runtime",
) -> tuple[Path, dict[str, Any]]:
    """Copy a closed Java runtime before any of its code is executed."""
    source_inventory = java_runtime_inventory(java_runtime)
    source_root = Path(source_inventory["root_path"])
    if expected is not None and (
        source_inventory["file_count"] != expected.get("runtime_file_count")
        or source_inventory["tree_sha256"] != expected.get("runtime_tree_sha256")
        or file_sha256(java_runtime) != expected.get("runtime_sha256")
    ):
        raise AndroidReleaseError(
            f"{mismatch_label} differs from the pinned release policy"
        )
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)

    def walk_error(error: OSError) -> None:
        raise AndroidReleaseError("Java runtime inventory is unreadable") from error

    for root_name in ("bin", "conf", "lib"):
        source = source_root / root_name
        for directory, child_directories, filenames in os.walk(
            source, topdown=True, onerror=walk_error, followlinks=False
        ):
            parent = Path(directory)
            for name in child_directories:
                child = parent / name
                if child.is_symlink():
                    raise AndroidReleaseError("Java runtime inventory contains a symlink")
            for name in filenames:
                input_path = parent / name
                relative = input_path.relative_to(source_root)
                output_path = destination / relative
                output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    source_mode = input_path.stat(follow_symlinks=False).st_mode
                except OSError as error:
                    raise AndroidReleaseError("Java runtime inventory is unreadable") from error
                _snapshot_regular_file(
                    input_path,
                    output_path,
                    label="Java runtime input",
                    executable=bool(source_mode & 0o111),
                )
    _make_snapshot_directories_private(destination)
    snapshot_java = destination / "bin" / "java"
    snapshot_inventory = java_runtime_inventory(snapshot_java)
    if (
        snapshot_inventory["file_count"] != source_inventory["file_count"]
        or snapshot_inventory["tree_sha256"] != source_inventory["tree_sha256"]
    ):
        raise AndroidReleaseError("Java runtime changed while it was snapshotted")
    return snapshot_java, snapshot_inventory


def _make_snapshot_directories_private(root: Path) -> None:
    try:
        root.chmod(0o700)
        for directory, child_directories, _ in os.walk(root, followlinks=False):
            parent = Path(directory)
            for name in child_directories:
                child = parent / name
                if child.is_symlink():
                    raise AndroidReleaseError("Verifier snapshot contains a symlink")
                child.chmod(0o700)
    except OSError as error:
        raise AndroidReleaseError("Verifier snapshot permissions are unsafe") from error


def _android_tool_environment() -> dict[str, str]:
    """Return a minimal environment that cannot override verifier code loading."""
    environment: dict[str, str] = {"LANG": "C", "LC_ALL": "C"}
    # All verifier executables are invoked by authenticated absolute paths.
    # Everything else is intentionally omitted: in particular PATH, JAVA_HOME,
    # LD_*/DYLD_* loader hooks, and Java agent/classpath option variables must
    # never select or modify release-verification code.
    for name in ("SYSTEMROOT", "WINDIR"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _run_android_tool(
    tool: Path,
    arguments: list[str],
    label: str,
    *,
    java_runtime: Path | None = None,
) -> subprocess.CompletedProcess:
    command = [str(tool), *arguments]
    if label == "apksigner":
        runtime = java_runtime or locate_java_runtime()
        implementation = tool.parent / "lib" / "apksigner.jar"
        command = [str(runtime), "-jar", str(implementation), *arguments]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
            env=_android_tool_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AndroidReleaseError(f"{label} could not run safely") from error


def _tool_material_identity(name: str, evidence: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "aapt": {"sha256", "implementation_sha256"},
        "apksigner": {
            "sha256", "implementation_sha256", "runtime_sha256",
            "runtime_file_count", "runtime_tree_sha256",
        },
    }[name]
    return {key: evidence.get(key) for key in sorted(keys)}


def _require_tool_material_matches_policy(
    observed: dict[str, Any], policy: dict[str, Any]
) -> None:
    for name in ("aapt", "apksigner"):
        if _tool_material_identity(name, observed[name]) != _tool_material_identity(
            name, policy[name]
        ):
            raise AndroidReleaseError(
                f"Android {name} verifier differs from the pinned release policy"
            )


def _snapshot_verification_tools(
    destination: Path,
    *,
    build_tools_version: str,
    tool_policy: dict[str, Any],
) -> dict[str, Any]:
    """Authenticate a closed verifier bundle before executing any verifier code."""
    source_aapt = locate_android_tool("aapt", build_tools_version)
    source_apksigner = locate_android_tool("apksigner", build_tools_version)
    source_java = locate_java_runtime()
    source_aapt_implementation = source_aapt.parent / "lib64" / "libc++.so"
    source_apksigner_implementation = source_apksigner.parent / "lib" / "apksigner.jar"

    # A deliberately deep private layout keeps every relative aapt RUNPATH
    # candidate inside the mode-0700 temporary root. Only the authenticated
    # lib64 companion is present there.
    tools_root = destination / "closed" / "sdk" / "one" / "two" / build_tools_version
    tools_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    snapshot_aapt = tools_root / "aapt"
    snapshot_apksigner = tools_root / "apksigner"
    snapshot_aapt_implementation = tools_root / "lib64" / "libc++.so"
    snapshot_apksigner_implementation = tools_root / "lib" / "apksigner.jar"
    snapshot_aapt_implementation.parent.mkdir(mode=0o700)
    snapshot_apksigner_implementation.parent.mkdir(mode=0o700)

    aapt_hash, _ = _snapshot_regular_file(
        source_aapt, snapshot_aapt, label="Android aapt verifier", executable=True
    )
    aapt_implementation_hash, _ = _snapshot_regular_file(
        source_aapt_implementation,
        snapshot_aapt_implementation,
        label="Android aapt implementation",
    )
    apksigner_hash, _ = _snapshot_regular_file(
        source_apksigner,
        snapshot_apksigner,
        label="Android apksigner launcher",
        executable=True,
    )
    apksigner_implementation_hash, _ = _snapshot_regular_file(
        source_apksigner_implementation,
        snapshot_apksigner_implementation,
        label="Android apksigner implementation",
    )
    snapshot_java, runtime_inventory = _snapshot_java_runtime(
        source_java,
        destination / "closed" / "java-runtime",
        expected=tool_policy["apksigner"],
        mismatch_label="Android apksigner verifier",
    )
    _make_snapshot_directories_private(destination)

    evidence: dict[str, dict[str, Any]] = {
        "aapt": {
            "path": str(source_aapt),
            "sha256": aapt_hash,
            "implementation_path": str(source_aapt_implementation),
            "implementation_sha256": aapt_implementation_hash,
        },
        "apksigner": {
            "path": str(source_apksigner),
            "sha256": apksigner_hash,
            "implementation_path": str(source_apksigner_implementation),
            "implementation_sha256": apksigner_implementation_hash,
            "runtime_path": str(source_java),
            "runtime_sha256": file_sha256(snapshot_java),
            "runtime_root_path": str(source_java.parent.parent),
            "runtime_file_count": runtime_inventory["file_count"],
            "runtime_tree_sha256": runtime_inventory["tree_sha256"],
        },
    }
    # Hash/tree identity is checked before the first aapt or Java execution.
    _require_tool_material_matches_policy(evidence, tool_policy)

    aapt_version = _run_android_tool(snapshot_aapt, ["version"], "aapt")
    aapt_version_output = (aapt_version.stdout + aapt_version.stderr).strip()
    if aapt_version.returncode or not aapt_version_output or len(aapt_version_output) > 1024:
        raise AndroidReleaseError("aapt version evidence is unavailable")
    java_version = _run_android_tool(snapshot_java, ["-version"], "java")
    java_version_output = (java_version.stdout + java_version.stderr).strip()
    if java_version.returncode or not java_version_output or len(java_version_output) > 4096:
        raise AndroidReleaseError("Java runtime version evidence is unavailable")
    apksigner_version = _run_android_tool(
        snapshot_apksigner,
        ["version"],
        "apksigner",
        java_runtime=snapshot_java,
    )
    apksigner_version_output = (
        apksigner_version.stdout + apksigner_version.stderr
    ).strip()
    if (
        apksigner_version.returncode
        or not apksigner_version_output
        or len(apksigner_version_output) > 1024
    ):
        raise AndroidReleaseError("apksigner version evidence is unavailable")
    evidence["aapt"]["version"] = aapt_version_output
    evidence["apksigner"]["version"] = apksigner_version_output
    evidence["apksigner"]["runtime_version"] = java_version_output
    _require_tool_evidence_matches_policy(evidence, tool_policy)
    bundle = {
        "aapt": snapshot_aapt,
        "apksigner": snapshot_apksigner,
        "java": snapshot_java,
        "evidence": evidence,
        "material_identity": {
            name: _tool_material_identity(name, item)
            for name, item in evidence.items()
        },
    }
    _assert_tool_bundle_unchanged(bundle)
    return bundle


def _assert_tool_bundle_unchanged(bundle: dict[str, Any]) -> None:
    aapt = Path(bundle["aapt"])
    apksigner = Path(bundle["apksigner"])
    java = Path(bundle["java"])
    try:
        runtime = java_runtime_inventory(java)
        observed = {
            "aapt": {
                "sha256": file_sha256(aapt),
                "implementation_sha256": file_sha256(aapt.parent / "lib64" / "libc++.so"),
            },
            "apksigner": {
                "sha256": file_sha256(apksigner),
                "implementation_sha256": file_sha256(apksigner.parent / "lib" / "apksigner.jar"),
                "runtime_sha256": file_sha256(java),
                "runtime_file_count": runtime["file_count"],
                "runtime_tree_sha256": runtime["tree_sha256"],
            },
        }
    except (OSError, AndroidReleaseError) as error:
        raise AndroidReleaseError(
            "Android verifier snapshot changed during inspection"
        ) from error
    if {
        name: _tool_material_identity(name, item) for name, item in observed.items()
    } != bundle["material_identity"]:
        raise AndroidReleaseError("Android verifier snapshot changed during inspection")


def _tool_identity(name: str, evidence: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "aapt": {"sha256", "version", "implementation_sha256"},
        "apksigner": {
            "sha256", "version", "implementation_sha256",
            "runtime_sha256", "runtime_version", "runtime_file_count",
            "runtime_tree_sha256",
        },
    }[name]
    return {key: evidence.get(key) for key in sorted(expected)}


def _require_tool_evidence_matches_policy(
    observed: dict[str, Any], policy: dict[str, Any]
) -> None:
    if set(observed) != {"aapt", "apksigner"} or set(policy) != {"aapt", "apksigner"}:
        raise AndroidReleaseError("Android release verification tool policy is incomplete")
    for name in ("aapt", "apksigner"):
        if _tool_identity(name, observed[name]) != _tool_identity(name, policy[name]):
            raise AndroidReleaseError(
                f"Android {name} verifier differs from the pinned release policy"
            )


def verify_java_runtime(root: Path) -> dict[str, Any]:
    """Authenticate the complete configured Java runtime, then query its version."""
    root = root.resolve()
    policy, _ = _policy(root)
    expected = policy["verification_tools"]["apksigner"]
    source_java = locate_java_runtime()
    with tempfile.TemporaryDirectory(prefix="david-pi-java-verify-") as temporary:
        snapshot_java, inventory = _snapshot_java_runtime(
            source_java,
            Path(temporary) / "runtime",
            expected=expected,
        )
        observed = {
            "runtime_sha256": file_sha256(snapshot_java),
            "runtime_file_count": inventory["file_count"],
            "runtime_tree_sha256": inventory["tree_sha256"],
        }
        if any(observed[key] != expected[key] for key in observed):
            raise AndroidReleaseError(
                "Android Java runtime differs from the pinned release policy"
            )
        version = _run_android_tool(snapshot_java, ["-version"], "java")
        version_output = (version.stdout + version.stderr).strip()
        if version.returncode or version_output != expected["runtime_version"]:
            raise AndroidReleaseError(
                "Android Java runtime version differs from the pinned release policy"
            )
        after = java_runtime_inventory(snapshot_java)
        if (
            after["file_count"] != inventory["file_count"]
            or after["tree_sha256"] != inventory["tree_sha256"]
            or file_sha256(snapshot_java) != observed["runtime_sha256"]
        ):
            raise AndroidReleaseError("Android Java runtime changed during verification")
        return {
            "runtime_path": str(source_java),
            "runtime_root_path": str(source_java.parent.parent),
            **observed,
            "runtime_version": version_output,
        }


def _forbidden_packaged_secret(name: str) -> bool:
    if "\\" in name:
        return True
    path = Path(name)
    parts = tuple(part.casefold() for part in path.parts)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        return True
    basename = parts[-1]
    return (
        any(part in FORBIDDEN_ANDROID_DIRECTORIES for part in parts[:-1])
        or basename in FORBIDDEN_ANDROID_FILES
        or basename == ".env"
        or basename.startswith(".env.")
        or Path(basename).suffix.casefold() in FORBIDDEN_ANDROID_SUFFIXES
    )


def _verify_apk_archive(apk: Path) -> None:
    try:
        with zipfile.ZipFile(apk) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)):
                raise AndroidReleaseError("Android release APK contains duplicate archive entries")
            if any(_forbidden_packaged_secret(name) for name in names):
                raise AndroidReleaseError("Android release APK contains a forbidden secret-like entry")
            for entry in entries:
                with archive.open(entry, "r") as handle:
                    if _stream_contains_credential(handle, assignments=True):
                        raise AndroidReleaseError(
                            "Android release APK contains credential-like content"
                        )
            bad_entry = archive.testzip()
            if bad_entry is not None:
                raise AndroidReleaseError("Android release APK archive integrity check failed")
    except AndroidReleaseError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise AndroidReleaseError("Android release APK archive is invalid") from error


def _snapshot_regular_file(
    source: Path,
    destination: Path,
    *,
    label: str = "Android release APK",
    executable: bool = False,
) -> tuple[str, int]:
    if not hasattr(os, "O_NOFOLLOW"):
        raise AndroidReleaseError(f"This platform cannot safely inspect {label}")
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise AndroidReleaseError(f"{label} is missing or unsafe") from error
    digest = hashlib.sha256()
    size = 0
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise AndroidReleaseError(f"{label} is missing or unsafe")
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            while True:
                block = os.read(source_fd, 1024 * 1024)
                if not block:
                    break
                digest.update(block)
                size += len(block)
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    if written < 1:
                        raise AndroidReleaseError(f"{label} snapshot failed")
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
        after = os.fstat(source_fd)
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
        os.chmod(destination, 0o500 if executable else 0o400)
    except OSError as error:
        raise AndroidReleaseError(f"{label} snapshot failed") from error
    finally:
        os.close(source_fd)
    return digest.hexdigest(), size


def inspect_apk(
    apk: Path, *, build_tools_version: str, tool_policy: dict[str, Any]
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="david-pi-apk-inspect-") as temporary:
        snapshot = Path(temporary) / "release.apk"
        artifact_hash, artifact_size = _snapshot_regular_file(apk, snapshot)
        return _inspect_apk_snapshot(
            snapshot,
            artifact_hash,
            artifact_size,
            build_tools_version=build_tools_version,
            tool_policy=tool_policy,
        )


def _inspect_apk_snapshot(
    apk: Path,
    artifact_hash: str,
    artifact_size: int,
    *,
    build_tools_version: str,
    tool_policy: dict[str, Any],
) -> dict[str, Any]:
    _verify_apk_archive(apk)
    with tempfile.TemporaryDirectory(prefix="david-pi-android-tools-") as temporary:
        bundle = _snapshot_verification_tools(
            Path(temporary),
            build_tools_version=build_tools_version,
            tool_policy=tool_policy,
        )
        return _inspect_apk_with_tool_bundle(
            apk,
            artifact_hash,
            artifact_size,
            bundle,
        )


def _inspect_apk_with_tool_bundle(
    apk: Path,
    artifact_hash: str,
    artifact_size: int,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    aapt = Path(bundle["aapt"])
    apksigner = Path(bundle["apksigner"])
    java = Path(bundle["java"])
    verification_tools = bundle["evidence"]
    badging = _run_android_tool(aapt, ["dump", "badging", str(apk)], "aapt")
    if badging.returncode:
        raise AndroidReleaseError("aapt could not inspect the Android release APK")
    package = BADGING.search(badging.stdout)
    if not package:
        raise AndroidReleaseError("Android release package metadata is missing")
    if re.search(r"(?mi)^application-debuggable(?:\s|$)", badging.stdout):
        raise AndroidReleaseError("Android release APK is debuggable")
    if re.search(
        r"(?mi)^application-(?:testonly|test-only)(?:\s|$)", badging.stdout
    ) or re.search(
        r"(?i)\b(?:android:)?testOnly\s*(?:=|:)\s*['\"]?(?:true|1)(?:['\"]|\b)",
        badging.stdout,
    ):
        raise AndroidReleaseError("Android release APK is marked testOnly")
    manifest = _run_android_tool(
        aapt, ["dump", "xmltree", str(apk), "AndroidManifest.xml"], "aapt"
    )
    if manifest.returncode:
        raise AndroidReleaseError("aapt could not inspect the signed Android manifest")
    if re.search(
        r"(?mi)A:\s+android:debuggable\b.*(?:0xffffffff|=\s*true\b|=\s*\"true\")",
        manifest.stdout,
    ):
        raise AndroidReleaseError("Android release APK is debuggable")
    if re.search(
        r"(?mi)A:\s+android:testOnly\b.*(?:0xffffffff|=\s*true\b|=\s*\"true\")",
        manifest.stdout,
    ):
        raise AndroidReleaseError("Android release APK is marked testOnly")
    manifest_names = [
        match.group(2)
        for line in manifest.stdout.splitlines()
        if (match := MANIFEST_STRING_ATTRIBUTE.match(line)) and match.group(1) == "name"
    ]
    if FIREBASE_INIT_PROVIDER in manifest_names:
        raise AndroidReleaseError("Android release APK retains automatic Firebase initialization")
    resources = _run_android_tool(aapt, ["dump", "resources", str(apk)], "aapt")
    if resources.returncode:
        raise AndroidReleaseError("aapt could not inspect Android release resources")
    configured_firebase_resources = {
        match.group(1)
        for match in re.finditer(
            r"(?m)^\s*spec resource [^\n:]+:[^/\s]+/([^:\s]+):",
            resources.stdout,
        )
        if match.group(1) in FIREBASE_RESOURCE_NAMES
    }
    if configured_firebase_resources:
        raise AndroidReleaseError("Android release APK contains Firebase client resources")
    embedded_source_hashes: list[str] = []
    firebase_states: list[str] = []
    lines = manifest.stdout.splitlines()
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("E: meta-data"):
            continue
        indentation = len(line) - len(line.lstrip())
        block_lines = [line]
        for following in lines[index + 1:]:
            following_indent = len(following) - len(following.lstrip())
            if following.lstrip().startswith("E:") and following_indent <= indentation:
                break
            block_lines.append(following)
        attributes: dict[str, list[str]] = {"name": [], "value": []}
        for block_line in block_lines:
            attribute = MANIFEST_STRING_ATTRIBUTE.match(block_line)
            if attribute:
                attributes[attribute.group(1)].append(attribute.group(2))
        for metadata_name, collected in (
            (RELEASE_SOURCE_METADATA, embedded_source_hashes),
            (FIREBASE_STATE_METADATA, firebase_states),
        ):
            if metadata_name not in attributes["name"]:
                continue
            if attributes["name"] != [metadata_name] or len(attributes["value"]) != 1:
                raise AndroidReleaseError(
                    f"Android release metadata {metadata_name} is ambiguous"
                )
            collected.append(attributes["value"][0])
    if (
        len(embedded_source_hashes) != 1
        or not SHA256.fullmatch(embedded_source_hashes[0])
    ):
        raise AndroidReleaseError("Android release APK lacks its signed source-tree hash")
    if firebase_states != [FIREBASE_UNCONFIGURED]:
        raise AndroidReleaseError("Android release Firebase client state is not explicit")
    verified = _run_android_tool(
        apksigner,
        ["verify", "--verbose", "--print-certs", str(apk)],
        "apksigner",
        java_runtime=java,
    )
    if verified.returncode:
        raise AndroidReleaseError("Android release APK signature verification failed")
    signer = SIGNER_DIGEST.findall(verified.stdout)
    signer_count = SIGNER_COUNT.findall(verified.stdout)
    if len(signer) != 1 or signer_count != ["1"]:
        raise AndroidReleaseError("Android release must have exactly one verified signer")
    schemes = {
        name.replace(".", ""): result == "true"
        for name, result in SCHEME.findall(verified.stdout)
    }
    expected_schemes = {"v1", "v2", "v3", "v31", "v4"}
    if set(schemes) != expected_schemes or not schemes["v2"]:
        raise AndroidReleaseError("Android release must have complete v2+ signing evidence")
    _assert_tool_bundle_unchanged(bundle)
    return {
        "application_id": package.group(1),
        "version_code": int(package.group(2)),
        "version_name": package.group(3),
        "source_tree_sha256": embedded_source_hashes[0],
        "firebase_client_configured": False,
        "verification_tools": verification_tools,
        "artifact": {
            "sha256": artifact_hash,
            "size_bytes": artifact_size,
        },
        "signing": {
            "certificate_sha256": signer[0],
            "signer_count": 1,
            "schemes": schemes,
        },
    }


def _policy(root: Path) -> tuple[dict[str, Any], dict[str, tuple[str, Path]]]:
    policy = _load_object(root / "config" / "android-release.json", "Android release policy")
    _require_exact_keys(
        policy,
        {
            "schema_version", "kind", "application_id", "artifact",
            "attestation", "source_build_file",
            "release_signer_certificate_sha256", "minimum_version_code",
            "expected_version_code", "expected_version_name",
            "android_build_tools_version", "verification_tools",
        },
        "Android release policy",
    )
    if (
        not _plain_int(policy.get("schema_version"))
        or policy.get("schema_version") != 1
        or policy.get("kind") != "david-pi-android-release-policy"
    ):
        raise AndroidReleaseError("Android release policy schema is unsupported")
    paths = {
        label: _safe_relative(root, policy.get(field), f"Android {label}")
        for label, field in (
            ("artifact", "artifact"),
            ("attestation", "attestation"),
            ("source", "source_build_file"),
        )
    }
    expected_paths = {
        "artifact": EXPECTED_ARTIFACT_PATH,
        "attestation": EXPECTED_ATTESTATION_PATH,
        "source": EXPECTED_SOURCE_BUILD_PATH,
    }
    if any(paths[label][0] != expected for label, expected in expected_paths.items()):
        raise AndroidReleaseError("Android release policy paths are not the fixed release paths")
    application_id = policy.get("application_id")
    if not isinstance(application_id, str) or not application_id:
        raise AndroidReleaseError("Android application ID policy is invalid")
    _require_sha256(
        policy.get("release_signer_certificate_sha256"),
        "Android release signer policy",
    )
    if policy.get("android_build_tools_version") != ANDROID_BUILD_TOOLS_VERSION:
        raise AndroidReleaseError("Android build-tools policy is unsupported")
    minimum_version_code = _require_uint(
        policy.get("minimum_version_code"),
        "Android minimum release version code",
        minimum=1,
    )
    expected_version_code = _require_uint(
        policy.get("expected_version_code"),
        "Android expected release version code",
        minimum=minimum_version_code,
    )
    expected_version_name = policy.get("expected_version_name")
    if not isinstance(expected_version_name, str) or not expected_version_name:
        raise AndroidReleaseError("Android expected release version name is invalid")
    verification_tools = policy.get("verification_tools")
    if not isinstance(verification_tools, dict) or set(verification_tools) != {
        "aapt", "apksigner",
    }:
        raise AndroidReleaseError("Android release verification tool policy is incomplete")
    expected_keys = {
        "aapt": {"sha256", "version", "implementation_sha256"},
        "apksigner": {
            "sha256", "version", "implementation_sha256",
            "runtime_sha256", "runtime_version", "runtime_file_count",
            "runtime_tree_sha256",
        },
    }
    for name, keys in expected_keys.items():
        evidence = verification_tools.get(name)
        if not isinstance(evidence, dict) or set(evidence) != keys:
            raise AndroidReleaseError("Android release verification tool policy is invalid")
        _require_sha256(evidence.get("sha256"), f"Android {name} tool policy hash")
        if "implementation_sha256" in keys:
            _require_sha256(
                evidence.get("implementation_sha256"),
                f"Android {name} implementation policy hash",
            )
        if (
            not isinstance(evidence.get("version"), str)
            or not evidence["version"]
            or len(evidence["version"]) > 1024
        ):
            raise AndroidReleaseError("Android release verification tool policy is invalid")
        if name == "apksigner" and (
            not isinstance(evidence.get("runtime_version"), str)
            or not evidence["runtime_version"]
            or len(evidence["runtime_version"]) > 4096
            or not _plain_int(evidence.get("runtime_file_count"))
            or evidence["runtime_file_count"] < 1
        ):
            raise AndroidReleaseError("Android release verification tool policy is invalid")
        if name == "apksigner":
            _require_sha256(
                evidence.get("runtime_sha256"),
                "Android Java runtime policy hash",
            )
            _require_sha256(
                evidence.get("runtime_tree_sha256"),
                "Android Java runtime inventory policy hash",
            )
    return policy, paths


def build_attestation(root: Path) -> dict[str, Any]:
    root = root.resolve()
    policy, paths = _policy(root)
    observed = inspect_apk(
        paths["artifact"][1],
        build_tools_version=policy["android_build_tools_version"],
        tool_policy=policy["verification_tools"],
    )
    source = source_version(paths["source"][1])
    source_inputs = android_source_inventory(root)
    if observed["application_id"] != policy["application_id"] or source["application_id"] != policy["application_id"]:
        raise AndroidReleaseError("Android application ID differs from release policy")
    for field in ("version_code", "version_name"):
        if observed[field] != source[field]:
            raise AndroidReleaseError(f"Android APK {field} differs from source")
    if (
        observed["version_code"] < policy["minimum_version_code"]
        or observed["version_code"] != policy["expected_version_code"]
        or observed["version_name"] != policy["expected_version_name"]
    ):
        raise AndroidReleaseError("Android APK version differs from release policy")
    if observed["source_tree_sha256"] != source_inputs["tree_sha256"]:
        raise AndroidReleaseError(
            "Android APK signed source-tree claim differs from the release input tree"
        )
    if observed["signing"]["certificate_sha256"] != policy["release_signer_certificate_sha256"]:
        raise AndroidReleaseError("Android APK signer differs from the established release signer")
    return {
        "schema_version": 1,
        "kind": "david-pi-android-release-attestation",
        "provenance_model": "signed_builder_declaration",
        "reproducible_build": False,
        "android_build_tools_version": policy["android_build_tools_version"],
        "verification_tools": observed["verification_tools"],
        "application_id": observed["application_id"],
        "version_code": observed["version_code"],
        "version_name": observed["version_name"],
        "firebase_client_configured": observed["firebase_client_configured"],
        "version_policy": {
            "minimum_version_code": policy["minimum_version_code"],
            "expected_version_code": policy["expected_version_code"],
            "expected_version_name": policy["expected_version_name"],
        },
        "artifact": {
            "path": paths["artifact"][0],
            **observed["artifact"],
        },
        "signing": observed["signing"],
        "source": {
            "build_file": paths["source"][0],
            "build_file_sha256": file_sha256(paths["source"][1]),
            "embedded_tree_sha256": observed["source_tree_sha256"],
            "release_inputs": source_inputs,
            **source,
        },
    }


def verified_android_release(
    root: Path, *, require_source: bool, require_tools: bool
) -> dict[str, Any]:
    """Validate the tracked attestation and return content-neutral release facts."""
    root = root.resolve()
    policy, paths = _policy(root)
    attestation = _load_object(paths["attestation"][1], "Android release attestation")
    _require_exact_keys(
        attestation,
        {
            "schema_version", "kind", "provenance_model", "reproducible_build",
            "android_build_tools_version", "verification_tools", "application_id",
            "version_code", "version_name", "firebase_client_configured",
            "version_policy", "artifact", "signing", "source",
        },
        "Android release attestation",
    )
    if (
        not _plain_int(attestation.get("schema_version"))
        or attestation.get("schema_version") != 1
        or attestation.get("kind") != "david-pi-android-release-attestation"
        or attestation.get("provenance_model") != "signed_builder_declaration"
        or attestation.get("reproducible_build") is not False
        or attestation.get("android_build_tools_version")
        != policy["android_build_tools_version"]
    ):
        raise AndroidReleaseError("Android release attestation schema is unsupported")
    artifact = attestation.get("artifact")
    signing = attestation.get("signing")
    source = attestation.get("source")
    verification_tools = attestation.get("verification_tools")
    if not all(
        isinstance(value, dict)
        for value in (artifact, signing, source, verification_tools)
    ):
        raise AndroidReleaseError("Android release attestation is incomplete")
    _require_exact_keys(
        artifact, {"path", "sha256", "size_bytes"}, "Android artifact evidence"
    )
    _require_exact_keys(
        signing,
        {"certificate_sha256", "signer_count", "schemes"},
        "Android signing evidence",
    )
    _require_exact_keys(
        source,
        {
            "build_file", "build_file_sha256", "embedded_tree_sha256",
            "release_inputs", "application_id", "version_code", "version_name",
        },
        "Android source evidence",
    )
    if set(verification_tools) != {"aapt", "apksigner"}:
        raise AndroidReleaseError("Android release verification tool evidence is incomplete")
    expected_evidence_keys = {
        "aapt": {
            "path", "sha256", "version", "implementation_path",
            "implementation_sha256",
        },
        "apksigner": {
            "path", "sha256", "version", "implementation_path",
            "implementation_sha256", "runtime_path", "runtime_sha256",
            "runtime_version", "runtime_root_path", "runtime_file_count",
            "runtime_tree_sha256",
        },
    }
    for name, evidence in verification_tools.items():
        if (
            not isinstance(evidence, dict)
            or set(evidence) != expected_evidence_keys[name]
            or not isinstance(evidence.get("path"), str)
            or not evidence["path"]
            or not isinstance(evidence.get("version"), str)
            or not evidence["version"]
            or len(evidence["version"]) > 1024
        ):
            raise AndroidReleaseError("Android release verification tool evidence is invalid")
        _require_sha256(evidence.get("sha256"), f"Android {name} tool hash")
        if "implementation_path" in expected_evidence_keys[name]:
            if (
                not isinstance(evidence.get("implementation_path"), str)
                or not evidence["implementation_path"]
            ):
                raise AndroidReleaseError("Android release verification tool evidence is invalid")
            _require_sha256(
                evidence.get("implementation_sha256"),
                f"Android {name} implementation hash",
            )
        if name == "apksigner":
            if (
                not isinstance(evidence.get("runtime_path"), str)
                or not evidence["runtime_path"]
                or not isinstance(evidence.get("runtime_version"), str)
                or not evidence["runtime_version"]
                or len(evidence["runtime_version"]) > 4096
                or not isinstance(evidence.get("runtime_root_path"), str)
                or not evidence["runtime_root_path"]
                or not _plain_int(evidence.get("runtime_file_count"))
                or evidence["runtime_file_count"] < 1
            ):
                raise AndroidReleaseError(
                    "Android release verification tool evidence is invalid"
                )
            _require_sha256(
                evidence.get("runtime_sha256"), "Android Java runtime hash"
            )
            _require_sha256(
                evidence.get("runtime_tree_sha256"),
                "Android Java runtime inventory hash",
            )
    _require_tool_evidence_matches_policy(
        verification_tools, policy["verification_tools"]
    )
    expected_hash = _require_sha256(artifact.get("sha256"), "Android APK hash")
    expected_size = _require_uint(
        artifact.get("size_bytes"), "Android APK size"
    )
    expected_signer = _require_sha256(
        signing.get("certificate_sha256"), "Android signer identity"
    )
    if artifact.get("path") != paths["artifact"][0]:
        raise AndroidReleaseError("Android attestation names the wrong artifact")
    with tempfile.TemporaryDirectory(prefix="david-pi-apk-verify-") as temporary:
        actual_hash, actual_size = _snapshot_regular_file(
            paths["artifact"][1], Path(temporary) / "release.apk"
        )
    if actual_hash != expected_hash:
        raise AndroidReleaseError("Android release APK hash differs from its attestation")
    if expected_size != actual_size:
        raise AndroidReleaseError("Android release APK size differs from its attestation")
    if (
        attestation.get("application_id") != policy["application_id"]
        or source.get("application_id") != policy["application_id"]
        or expected_signer != policy["release_signer_certificate_sha256"]
        or not _plain_int(signing.get("signer_count"))
        or signing.get("signer_count") != 1
        or not isinstance(signing.get("schemes"), dict)
        or set(signing["schemes"]) != {"v1", "v2", "v3", "v31", "v4"}
        or any(not isinstance(value, bool) for value in signing["schemes"].values())
        or signing["schemes"].get("v2") is not True
    ):
        raise AndroidReleaseError("Android release identity differs from policy")
    if not _plain_int(attestation.get("version_code")) or attestation["version_code"] < 1:
        raise AndroidReleaseError("Android attested version code is invalid")
    if not isinstance(attestation.get("version_name"), str) or not attestation["version_name"]:
        raise AndroidReleaseError("Android attested version name is invalid")
    for field in ("version_code", "version_name"):
        if attestation.get(field) != source.get(field):
            raise AndroidReleaseError(f"Android attested {field} is inconsistent")
    if attestation.get("firebase_client_configured") is not False:
        raise AndroidReleaseError("Android Firebase client configuration state is invalid")
    expected_version_policy = {
        "minimum_version_code": policy["minimum_version_code"],
        "expected_version_code": policy["expected_version_code"],
        "expected_version_name": policy["expected_version_name"],
    }
    _require_exact_keys(
        attestation.get("version_policy"),
        set(expected_version_policy),
        "Android version policy evidence",
    )
    if attestation.get("version_policy") != expected_version_policy:
        raise AndroidReleaseError("Android attested version policy is inconsistent")
    if (
        attestation["version_code"] < policy["minimum_version_code"]
        or attestation["version_code"] != policy["expected_version_code"]
        or attestation["version_name"] != policy["expected_version_name"]
    ):
        raise AndroidReleaseError("Android attested version differs from release policy")
    if source.get("build_file") != paths["source"][0]:
        raise AndroidReleaseError("Android attestation names the wrong source build file")
    _require_sha256(source.get("build_file_sha256"), "Android source build hash")
    embedded_tree = _require_sha256(
        source.get("embedded_tree_sha256"), "Android embedded source tree hash"
    )
    release_inputs = source.get("release_inputs")
    if (
        not isinstance(release_inputs, dict)
        or set(release_inputs) != {"root", "file_count", "tree_sha256", "files"}
        or release_inputs.get("root") != ANDROID_SOURCE_ROOT.as_posix()
        or not _plain_int(release_inputs.get("file_count"))
        or release_inputs.get("file_count", 0) < 1
        or not isinstance(release_inputs.get("files"), list)
        or release_inputs.get("file_count") != len(release_inputs.get("files"))
    ):
        raise AndroidReleaseError("Android source input inventory is invalid")
    _require_uint(
        release_inputs.get("file_count"),
        "Android source input file count",
        minimum=1,
    )
    _require_sha256(release_inputs.get("tree_sha256"), "Android source tree hash")
    if embedded_tree != release_inputs["tree_sha256"]:
        raise AndroidReleaseError("Android embedded source tree hash is inconsistent")
    inventory_paths: list[str] = []
    inventory_digest = hashlib.sha256()
    for item in release_inputs["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise AndroidReleaseError("Android source input inventory is invalid")
        relative, _ = _safe_relative(root, item.get("path"), "Android source input")
        if not _included_android_path(Path(relative)):
            raise AndroidReleaseError("Android source input inventory is invalid")
        inventory_paths.append(relative)
        item_hash = _require_sha256(item.get("sha256"), "Android source input hash")
        _require_uint(item.get("size_bytes"), "Android source input size")
        encoded = relative.encode("utf-8")
        inventory_digest.update(len(encoded).to_bytes(4, "big"))
        inventory_digest.update(encoded)
        inventory_digest.update(item["size_bytes"].to_bytes(8, "big"))
        inventory_digest.update(bytes.fromhex(item_hash))
    if inventory_paths != sorted(set(inventory_paths)):
        raise AndroidReleaseError("Android source input inventory is not canonical")
    if inventory_digest.hexdigest() != release_inputs["tree_sha256"]:
        raise AndroidReleaseError("Android source input inventory digest is inconsistent")
    if require_source:
        current_source = source_version(paths["source"][1])
        if file_sha256(paths["source"][1]) != source["build_file_sha256"]:
            raise AndroidReleaseError("Android source build file differs from its attestation")
        if any(current_source[field] != source.get(field) for field in current_source):
            raise AndroidReleaseError("Android source version differs from its attestation")
        if android_source_inventory(root) != release_inputs:
            raise AndroidReleaseError("Android release source tree differs from its attestation")
    if require_tools:
        observed = inspect_apk(
            paths["artifact"][1],
            build_tools_version=policy["android_build_tools_version"],
            tool_policy=policy["verification_tools"],
        )
        comparable = {
            "application_id": attestation["application_id"],
            "version_code": attestation["version_code"],
            "version_name": attestation["version_name"],
            "source_tree_sha256": source["embedded_tree_sha256"],
            "firebase_client_configured": attestation["firebase_client_configured"],
            "artifact": {"sha256": artifact["sha256"], "size_bytes": artifact["size_bytes"]},
            "signing": signing,
        }
        observed_without_tools = dict(observed)
        observed_tools = observed_without_tools.pop("verification_tools")
        if observed_without_tools != comparable:
            raise AndroidReleaseError("Android APK metadata differs from its attestation")
        for name in ("aapt", "apksigner"):
            if _tool_identity(name, observed_tools[name]) != _tool_identity(
                name, verification_tools[name]
            ):
                raise AndroidReleaseError(
                    "Android APK verifier identity differs from its attestation"
                )
    return attestation
