import json
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from flask import Flask
from jinja2 import Environment, FileSystemLoader, select_autoescape

import modules.android_release as android_release_module
from modules.android_release import (
    AndroidReleaseError,
    android_source_inventory,
    available_android_release,
    build_attestation,
    verify_java_runtime,
    verified_android_release,
)
import scripts.android_image_release as android_image_release


ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ID = "com.davidpi.backup"
SIGNER = "6" * 64
VERSION_CODE = 25
VERSION_NAME = "1.1.5-complete-reconciliation"
APK_PATH = Path("artifacts/android/david-pi-backup.apk")
ATTESTATION_PATH = Path("artifacts/android/david-pi-backup.manifest.json")


def write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def executable(path: Path, value: str) -> Path:
    write(path, value)
    path.chmod(0o755)
    return path


def release_fixture(
    root: Path,
    monkeypatch,
    *,
    version_code=VERSION_CODE,
    source_version_code=VERSION_CODE,
    signer=SIGNER,
    embedded_tree=None,
    badging_extra="",
    manifest_extra="",
    resources_extra="",
    firebase_state="unconfigured",
    apk_entries=(),
):
    build_file = root / "clients/android/app/build.gradle.kts"
    write(
        build_file,
        "android {\n"
        "  defaultConfig {\n"
        f'    applicationId = "{APPLICATION_ID}"\n'
        f"    versionCode = {source_version_code}\n"
        f'    versionName = "{VERSION_NAME}"\n'
        "  }\n"
        "}\n",
    )
    write(
        root / "clients/android/app/src/main/java/com/davidpi/backup/Main.kt",
        "package com.davidpi.backup\nclass Main\n",
    )
    write(root / "clients/android/app/src/main/res/values/strings.xml", "<resources/>\n")
    write(root / "clients/android/app/src/release/AndroidManifest.xml", "<manifest/>\n")
    write(root / "clients/android/app/schemas/database/2.json", "{}\n")
    write(root / "clients/android/gradle/wrapper/gradle-wrapper.properties", "distributionUrl=test\n")
    apk = root / APK_PATH
    apk.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(apk, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"fixture")
        for name, content in apk_entries:
            archive.writestr(name, content)
    policy = {
        "schema_version": 1,
        "kind": "david-pi-android-release-policy",
        "application_id": APPLICATION_ID,
        "artifact": APK_PATH.as_posix(),
        "attestation": ATTESTATION_PATH.as_posix(),
        "source_build_file": "clients/android/app/build.gradle.kts",
        "release_signer_certificate_sha256": SIGNER,
        "minimum_version_code": VERSION_CODE,
        "expected_version_code": VERSION_CODE,
        "expected_version_name": VERSION_NAME,
    }
    write(root / "app.py", "APP = 'fixture'\n")
    write(root / "requirements.txt", "flask==3.1.2\n")
    write(root / "docker-entrypoint.sh", "#!/bin/sh\nexec \"$@\"\n")
    write(root / "modules/runtime.py", "VALUE = 'fixture'\n")
    write(root / "templates/index.html", "fixture\n")
    write(root / "static/app.js", "const fixture = true;\n")
    write(root / "assets/readme.txt", "fixture\n")
    write(root / "knowledge/readme.md", "fixture\n")
    tree_hash = embedded_tree or android_source_inventory(root)["tree_sha256"]
    tools = root / "tools"
    aapt = executable(
        tools / "aapt",
        "#!/bin/sh\n"
        "if [ \"$1\" = version ]; then\n"
        "printf '%s\\n' 'Android Asset Packaging Tool, fixture'\n"
        "elif [ \"$2\" = xmltree ]; then\n"
        "cat <<'EOF'\n"
        "N: android=http://schemas.android.com/apk/res/android\n"
        "E: manifest\n"
        "  E: application\n"
        f"{manifest_extra}"
        "    E: meta-data\n"
        "      A: android:name(0x01010003)=\"com.davidpi.backup.RELEASE_SOURCE_SHA256\"\n"
        f"      A: android:value(0x01010024)=\"{tree_hash}\"\n"
        "    E: meta-data\n"
        "      A: android:name(0x01010003)=\"com.davidpi.backup.RELEASE_FIREBASE_CLIENT_STATE\"\n"
        f"      A: android:value(0x01010024)=\"{firebase_state}\"\n"
        "EOF\n"
        "elif [ \"$2\" = resources ]; then\n"
        "cat <<'EOF'\n"
        f"{resources_extra}"
        "EOF\n"
        "else\n"
        "cat <<'EOF'\n"
        f"package: name='{APPLICATION_ID}' versionCode='{version_code}' "
        f"versionName='{VERSION_NAME}'{badging_extra}\n"
        "EOF\n"
        "fi\n",
    )
    aapt_implementation = tools / "lib64" / "libc++.so"
    write(aapt_implementation, b"fixture-aapt-implementation")
    apksigner = executable(
        tools / "apksigner",
        "#!/bin/sh\n"
        "if [ \"$1\" = version ]; then printf '%s\\n' 'fixture-0.9'; exit 0; fi\n"
        "printf '%s\\n' 'Verifies' "
        "'Verified using v1 scheme (JAR signing): false' "
        "'Verified using v2 scheme (APK Signature Scheme v2): true' "
        "'Verified using v3 scheme (APK Signature Scheme v3): false' "
        "'Verified using v3.1 scheme (APK Signature Scheme v3.1): false' "
        "'Verified using v4 scheme (APK Signature Scheme v4): false' "
        "'Number of signers: 1' "
        f"'Signer #1 certificate SHA-256 digest: {signer}'\n",
    )
    apksigner_implementation = tools / "lib" / "apksigner.jar"
    write(apksigner_implementation, b"fixture-apksigner-implementation")
    java = executable(
        tools / "bin" / "java",
        "#!/bin/sh\n"
        "if [ \"$1\" = -version ]; then\n"
        "  printf '%s\\n' 'fixture java 17' >&2\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1\" != -jar ]; then exit 91; fi\n"
        "shift 2\n"
        "if [ \"$1\" = version ]; then printf '%s\\n' 'fixture-0.9'; exit 0; fi\n"
        "printf '%s\\n' 'Verifies' "
        "'Verified using v1 scheme (JAR signing): false' "
        "'Verified using v2 scheme (APK Signature Scheme v2): true' "
        "'Verified using v3 scheme (APK Signature Scheme v3): false' "
        "'Verified using v3.1 scheme (APK Signature Scheme v3.1): false' "
        "'Verified using v4 scheme (APK Signature Scheme v4): false' "
        "'Number of signers: 1' "
        f"'Signer #1 certificate SHA-256 digest: {signer}'\n",
    )
    write(tools / "conf/security/java.security", "fixture-runtime-config\n")
    runtime_inventory = android_release_module.java_runtime_inventory(java)
    policy.update(
        {
            "android_build_tools_version": "35.0.0",
            "verification_tools": {
                "aapt": {
                    "sha256": android_release_module.file_sha256(aapt),
                    "implementation_sha256": android_release_module.file_sha256(
                        aapt_implementation
                    ),
                    "version": "Android Asset Packaging Tool, fixture",
                },
                "apksigner": {
                    "sha256": android_release_module.file_sha256(apksigner),
                    "implementation_sha256": android_release_module.file_sha256(
                        apksigner_implementation
                    ),
                    "runtime_sha256": android_release_module.file_sha256(java),
                    "runtime_version": "fixture java 17",
                    "runtime_file_count": runtime_inventory["file_count"],
                    "runtime_tree_sha256": runtime_inventory["tree_sha256"],
                    "version": "fixture-0.9",
                },
            },
        }
    )
    write(root / "config/android-release.json", json.dumps(policy))
    monkeypatch.setenv("ANDROID_AAPT", str(aapt))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(apksigner))
    monkeypatch.setenv("ANDROID_JAVA", str(java))
    return apk


def attest(root: Path, monkeypatch, **values):
    release_fixture(root, monkeypatch, **values)
    document = build_attestation(root)
    write(root / ATTESTATION_PATH, json.dumps(document, sort_keys=True))
    return document


def candidate_image_fixture(root: Path, image_root: Path) -> Path:
    for relative in (
        Path("app.py"), Path("requirements.txt"), APK_PATH, ATTESTATION_PATH,
    ):
        destination = image_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)
    for relative in ("modules", "config", "templates", "static", "assets", "knowledge"):
        shutil.copytree(root / relative, image_root / relative)
    entrypoint = image_root.parent / "docker-entrypoint"
    shutil.copyfile(root / "docker-entrypoint.sh", entrypoint)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return entrypoint


def copy_candidate_member(arguments, image_root: Path, entrypoint: Path) -> None:
    source = arguments[1].split(":", 1)[1]
    destination = Path(arguments[2])
    if source == "/app":
        shutil.copytree(image_root, destination)
    elif source == "/usr/local/bin/docker-entrypoint":
        shutil.copyfile(entrypoint, destination)
    else:
        raise AssertionError(arguments)


def candidate_inspect_json(**overrides) -> str:
    config = {
        "User": "10001:10001",
        "WorkingDir": "/app",
        "Entrypoint": ["/usr/local/bin/docker-entrypoint"],
        "Cmd": [
            "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "2",
            "--timeout", "600", "--worker-tmp-dir", "/dev/shm", "app:app",
        ],
        "Env": [
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
        "ExposedPorts": {"8000/tcp": {}},
        "Healthcheck": None,
        "Volumes": None,
    }
    config.update(overrides)
    return json.dumps([{"Config": config}])


def test_attestation_binds_apk_signer_version_and_complete_source_tree(tmp_path, monkeypatch):
    document = attest(tmp_path, monkeypatch)
    verified = verified_android_release(tmp_path, require_source=True, require_tools=True)
    assert verified == document
    paths = {item["path"] for item in document["source"]["release_inputs"]["files"]}
    assert "clients/android/app/src/main/java/com/davidpi/backup/Main.kt" in paths
    assert "clients/android/app/src/main/res/values/strings.xml" in paths
    assert "clients/android/app/src/release/AndroidManifest.xml" in paths
    assert "clients/android/app/schemas/database/2.json" in paths
    assert "clients/android/gradle/wrapper/gradle-wrapper.properties" in paths
    assert document["signing"]["certificate_sha256"] == SIGNER
    assert document["version_code"] == VERSION_CODE
    assert document["firebase_client_configured"] is False
    assert document["provenance_model"] == "signed_builder_declaration"
    assert document["reproducible_build"] is False


def test_offline_audiobook_v2_schema_is_additive_and_release_attested():
    schema_root = (
        ROOT
        / "clients/android/app/schemas/com.davidpi.backup.offline.OfflineAudiobookDatabase"
    )
    legacy = json.loads((schema_root / "1.json").read_text(encoding="utf-8"))
    current = json.loads((schema_root / "2.json").read_text(encoding="utf-8"))
    legacy_fields = {
        field["columnName"]: field
        for field in legacy["database"]["entities"][0]["fields"]
    }
    current_fields = {
        field["columnName"]: field
        for field in current["database"]["entities"][0]["fields"]
    }

    assert current["database"]["version"] == 2
    assert current_fields.keys() - legacy_fields.keys() == {
        "contentSha256",
        "integrityVerifiedAt",
    }
    assert all(current_fields[name]["notNull"] is False for name in current_fields.keys() - legacy_fields.keys())
    assert all(current_fields[name] == field for name, field in legacy_fields.items())

    inventory = android_source_inventory(ROOT)
    paths = {item["path"] for item in inventory["files"]}
    assert (
        "clients/android/app/schemas/"
        "com.davidpi.backup.offline.OfflineAudiobookDatabase/2.json"
    ) in paths


def test_offline_audiobook_v3_preserves_audio_and_adds_durable_progress_outbox():
    schema_root = ROOT / "clients/android/app/schemas/com.davidpi.backup.offline.OfflineAudiobookDatabase"
    previous = json.loads((schema_root / "2.json").read_text())["database"]["entities"][0]
    current = json.loads((schema_root / "3.json").read_text())["database"]["entities"][0]
    old_fields = {field["columnName"]: field for field in previous["fields"]}
    new_fields = {field["columnName"]: field for field in current["fields"]}
    assert all(new_fields[name] == field for name, field in old_fields.items())
    assert new_fields.keys() - old_fields.keys() == {"progressScope", "progressRevision", "progressSession", "progressSequence", "progressDirty"}
    assert new_fields["progressScope"]["notNull"] is False
    for field in ("progressRevision", "progressSequence", "progressDirty"):
        assert new_fields[field]["defaultValue"] == "0"


def test_verifier_identity_is_portable_across_tool_paths(tmp_path, monkeypatch):
    document = attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    verified = verified_android_release(tmp_path, require_source=True, require_tools=True)
    assert verified == document
    assert verified["verification_tools"]["aapt"]["path"] != str(relocated / "aapt")


def test_verifier_rejects_changed_apksigner_implementation_jar(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    write(relocated / "lib/apksigner.jar", b"different implementation")
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    with pytest.raises(AndroidReleaseError, match="apksigner verifier differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_verifier_rejects_changed_aapt_companion_library(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    write(relocated / "lib64/libc++.so", b"different implementation")
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    with pytest.raises(AndroidReleaseError, match="aapt verifier differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_verifier_rejects_changed_absolute_java_runtime(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    with (relocated / "bin" / "java").open("a", encoding="utf-8") as handle:
        handle.write("# changed runtime\n")
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    with pytest.raises(AndroidReleaseError, match="apksigner verifier differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_verifier_rejects_changed_adjacent_java_runtime_implementation(
    tmp_path, monkeypatch
):
    attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    write(relocated / "lib/runtime-adjacent.so", b"untrusted adjacent runtime")
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    with pytest.raises(AndroidReleaseError, match="apksigner verifier differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_verifier_rejects_injected_java_bin_runtime_implementation(
    tmp_path, monkeypatch
):
    attest(tmp_path, monkeypatch)
    relocated = tmp_path / "relocated-tools"
    shutil.copytree(tmp_path / "tools", relocated)
    write(relocated / "bin/libjli.so", b"same-directory loader injection")
    monkeypatch.setenv("ANDROID_AAPT", str(relocated / "aapt"))
    monkeypatch.setenv("ANDROID_APKSIGNER", str(relocated / "apksigner"))
    monkeypatch.setenv("ANDROID_JAVA", str(relocated / "bin" / "java"))

    with pytest.raises(AndroidReleaseError, match="apksigner verifier differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_verify_java_authenticates_closed_tree_before_executing(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch)
    verified = verify_java_runtime(tmp_path)
    assert verified["runtime_file_count"] >= 3
    assert verified["runtime_version"] == "fixture java 17"

    write(tmp_path / "tools/bin/libjli.so", b"injected before verification")
    monkeypatch.setattr(
        android_release_module,
        "_run_android_tool",
        lambda *args, **kwargs: pytest.fail("untrusted Java was executed before authentication"),
    )
    with pytest.raises(AndroidReleaseError, match="differs from the pinned release policy"):
        verify_java_runtime(tmp_path)


def test_java_inventory_rejects_pre_authentication_byte_exhaustion(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch)
    oversized = tmp_path / "tools/lib/oversized-runtime-input"
    with oversized.open("wb") as handle:
        handle.truncate(android_release_module.MAX_JAVA_RUNTIME_BYTES + 1)
    monkeypatch.setattr(
        android_release_module,
        "_run_android_tool",
        lambda *args, **kwargs: pytest.fail("oversized runtime was executed"),
    )

    with pytest.raises(AndroidReleaseError, match="safe byte limit"):
        verify_java_runtime(tmp_path)


def test_aapt_executes_from_closed_snapshot_without_same_directory_injection(
    tmp_path, monkeypatch
):
    document = attest(tmp_path, monkeypatch)
    write(tmp_path / "tools/libc++.so", b"same-directory loader injection")

    verified = verified_android_release(
        tmp_path, require_source=True, require_tools=True
    )

    assert verified == document


def test_original_tool_paths_can_change_only_after_closed_snapshot(
    tmp_path, monkeypatch
):
    document = attest(tmp_path, monkeypatch)
    source_aapt = tmp_path / "tools/aapt"
    source_java = tmp_path / "tools/bin/java"
    original_run = android_release_module._run_android_tool
    first = True

    def mutate_originals_after_snapshot(tool, arguments, label, **kwargs):
        nonlocal first
        if first:
            first = False
            with source_aapt.open("a", encoding="utf-8") as handle:
                handle.write("# source changed after snapshot\n")
            with source_java.open("a", encoding="utf-8") as handle:
                handle.write("# source changed after snapshot\n")
        assert not Path(tool).is_relative_to(tmp_path / "tools")
        return original_run(tool, arguments, label, **kwargs)

    monkeypatch.setattr(
        android_release_module, "_run_android_tool", mutate_originals_after_snapshot
    )
    assert verified_android_release(
        tmp_path, require_source=True, require_tools=True
    ) == document


def test_verifier_rejects_snapshot_mutation_before_acceptance(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    original_run = android_release_module._run_android_tool
    changed = False

    def mutate_snapshot(tool, arguments, label, **kwargs):
        nonlocal changed
        result = original_run(tool, arguments, label, **kwargs)
        if label == "aapt" and not changed:
            changed = True
            Path(tool).chmod(0o700)
            with Path(tool).open("a", encoding="utf-8") as handle:
                handle.write("# mutated verifier snapshot\n")
        return result

    monkeypatch.setattr(android_release_module, "_run_android_tool", mutate_snapshot)
    with pytest.raises(AndroidReleaseError, match="snapshot changed"):
        verified_android_release(tmp_path, require_source=True, require_tools=True)


def test_apksigner_never_resolves_java_from_hostile_path(tmp_path, monkeypatch):
    document = attest(tmp_path, monkeypatch)
    hostile = tmp_path / "hostile-bin"
    marker = tmp_path / "hostile-java-ran"
    executable(
        hostile / "java",
        "#!/bin/sh\n"
        f"printf ran > '{marker}'\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", str(hostile))

    assert verified_android_release(
        tmp_path, require_source=True, require_tools=True
    ) == document
    assert not marker.exists()


def test_verifier_subprocess_drops_ambient_loader_and_java_injection(tmp_path, monkeypatch):
    probe = executable(
        tmp_path / "environment-probe",
        "#!/bin/sh\n"
        "for name in LD_LIBRARY_PATH LD_PRELOAD LD_AUDIT DYLD_LIBRARY_PATH "
        "DYLD_INSERT_LIBRARIES JAVA_TOOL_OPTIONS JDK_JAVA_OPTIONS _JAVA_OPTIONS CLASSPATH; do\n"
        "  eval value=\"\\${$name-}\"\n"
        "  if [ -n \"$value\" ]; then exit 97; fi\n"
        "done\n"
        "printf '%s\\n' clean-environment\n",
    )
    hostile = {
        "LD_LIBRARY_PATH": "/attacker/lib",
        "LD_PRELOAD": "/attacker/preload.so",
        "LD_AUDIT": "/attacker/audit.so",
        "DYLD_LIBRARY_PATH": "/attacker/dyld",
        "DYLD_INSERT_LIBRARIES": "/attacker/inject.dylib",
        "JAVA_TOOL_OPTIONS": "-javaagent:/attacker/agent.jar",
        "JDK_JAVA_OPTIONS": "-javaagent:/attacker/agent.jar",
        "_JAVA_OPTIONS": "-Xbootclasspath/a:/attacker/classes",
        "CLASSPATH": "/attacker/classes",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    result = android_release_module._run_android_tool(probe, [], "probe")

    assert result.returncode == 0
    assert result.stdout.strip() == "clean-environment"
    assert not result.stderr


def test_runtime_validation_fails_closed_when_apk_differs(tmp_path, monkeypatch):
    apk = release_fixture(tmp_path, monkeypatch)
    write(tmp_path / ATTESTATION_PATH, json.dumps(build_attestation(tmp_path)))
    apk.write_bytes(apk.read_bytes() + b"tamper")
    with pytest.raises(AndroidReleaseError, match="hash differs"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


def test_policy_rejects_unknown_claim_fields(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    policy_path = tmp_path / "config/android-release.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["release_is_reproducible"] = True
    write(policy_path, json.dumps(policy))

    with pytest.raises(AndroidReleaseError, match="policy fields are invalid"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


@pytest.mark.parametrize(
    "section",
    ["top", "artifact", "signing", "source", "version_policy", "release_inputs"],
)
def test_attestation_rejects_unknown_claim_fields(tmp_path, monkeypatch, section):
    document = attest(tmp_path, monkeypatch)
    targets = {
        "top": document,
        "artifact": document["artifact"],
        "signing": document["signing"],
        "source": document["source"],
        "version_policy": document["version_policy"],
        "release_inputs": document["source"]["release_inputs"],
    }
    targets[section]["unverified_claim"] = "must-not-survive"
    write(tmp_path / ATTESTATION_PATH, json.dumps(document))

    with pytest.raises(AndroidReleaseError, match="fields are invalid|inventory is invalid"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["artifact"].__setitem__("size_bytes", 2**80),
        lambda document: document.__setitem__("version_code", True),
        lambda document: document["source"]["release_inputs"]["files"][0].__setitem__(
            "size_bytes", True
        ),
    ],
)
def test_malformed_sidecar_integers_fail_closed_without_portal_error(
    tmp_path, monkeypatch, mutation
):
    document = attest(tmp_path, monkeypatch)
    mutation(document)
    write(tmp_path / ATTESTATION_PATH, json.dumps(document))
    with pytest.raises(AndroidReleaseError, match="invalid"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)
    assert available_android_release(
        tmp_path, logging.getLogger("android-release-malformed-test")
    ) is None


def test_policy_rejects_control_character_paths_and_duplicate_json_keys(
    tmp_path, monkeypatch
):
    release_fixture(tmp_path, monkeypatch)
    policy_path = tmp_path / "config/android-release.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["artifact"] = APK_PATH.as_posix() + "\x00hidden"
    write(policy_path, json.dumps(policy))
    assert available_android_release(
        tmp_path, logging.getLogger("android-release-control-path-test")
    ) is None

    release_fixture(tmp_path, monkeypatch)
    raw = policy_path.read_text(encoding="utf-8")
    raw = raw.replace(
        '"schema_version": 1',
        '"schema_version": 1, "schema_version": 1',
        1,
    )
    write(policy_path, raw)
    with pytest.raises(AndroidReleaseError, match="duplicate JSON keys"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


@pytest.mark.parametrize("relative", [APK_PATH, ATTESTATION_PATH])
def test_runtime_validation_rejects_in_root_release_symlinks(tmp_path, monkeypatch, relative):
    attest(tmp_path, monkeypatch)
    original = tmp_path / relative
    moved = tmp_path / (relative.as_posix() + ".moved")
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(AndroidReleaseError, match="missing or unsafe|missing or invalid"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


def test_stale_runtime_artifact_hides_download_and_shows_truthful_state(tmp_path, monkeypatch):
    apk = release_fixture(tmp_path, monkeypatch)
    write(tmp_path / ATTESTATION_PATH, json.dumps(build_attestation(tmp_path)))
    apk.write_bytes(apk.read_bytes() + b"stale")
    release = available_android_release(tmp_path, logging.getLogger("android-release-test"))
    assert release is None
    environment = Environment(
        loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape()
    )
    html = environment.get_template("device_backup.html").render(
        apk_available=False, apk_sha256=None, apk_version=None, devices=[],
        ios_shortcut_icloud_url="",
        public_installation={}, server_name="John-Pi",
    )
    assert 'href="/device-backup/apk"' not in html
    assert "signed release could not be verified" in html


def test_static_url_cannot_bypass_authenticated_download_gate():
    app = Flask("android-static-gate", static_folder=str(ROOT / "static"), static_url_path="/static")
    assert app.test_client().get("/static/apk/david-pi-backup.apk").status_code == 404


def test_release_validation_fails_on_kotlin_source_drift(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    kotlin = tmp_path / "clients/android/app/src/main/java/com/davidpi/backup/Main.kt"
    kotlin.write_text(kotlin.read_text() + "// drift\n", encoding="utf-8")
    with pytest.raises(AndroidReleaseError, match="source tree differs"):
        verified_android_release(tmp_path, require_source=True, require_tools=False)


def test_nested_build_named_source_input_is_included_but_exact_outputs_are_not(
    tmp_path, monkeypatch
):
    release_fixture(tmp_path, monkeypatch)
    nested = tmp_path / "clients/android/app/src/main/assets/build/real-input.txt"
    output = tmp_path / "clients/android/app/build/generated/output.txt"
    write(nested, "included\n")
    write(output, "excluded\n")
    paths = {item["path"] for item in android_source_inventory(tmp_path)["files"]}
    assert nested.relative_to(tmp_path).as_posix() in paths
    assert output.relative_to(tmp_path).as_posix() not in paths


@pytest.mark.parametrize(
    "name",
    [
        "local.properties", "keystore.properties", "release.jks", "release.p12",
        ".env", ".env.release", "private.pem", "private.key", "private.p8",
        "release.bks", "release.jceks", "release.pk8", "release.pkcs8",
        "google-services.json", "secrets/credential.txt",
    ],
)
def test_source_inventory_rejects_machine_local_inputs(tmp_path, monkeypatch, name):
    release_fixture(tmp_path, monkeypatch)
    write(tmp_path / "clients/android" / name, "not-inspected\n")
    with pytest.raises(AndroidReleaseError, match="machine-local Android signing input|secret directory"):
        android_source_inventory(tmp_path)


def test_source_inventory_rejects_ignored_benign_named_asset(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch)
    write(tmp_path / ".gitignore", "clients/android/app/src/main/assets/config.txt\n")
    write(tmp_path / "clients/android/app/src/main/assets/config.txt", "ordinary text\n")
    __import__("subprocess").run(["git", "init", "-q"], cwd=tmp_path, check=True)
    with pytest.raises(AndroidReleaseError, match="ignored build input"):
        android_source_inventory(tmp_path)


def test_source_inventory_rejects_credential_content_under_benign_name(
    tmp_path, monkeypatch
):
    release_fixture(tmp_path, monkeypatch)
    write(
        tmp_path / "clients/android/app/src/main/assets/config.txt",
        'client_secret = "this-is-a-real-looking-client-secret"\n',
    )
    with pytest.raises(AndroidReleaseError, match="credential-like content"):
        android_source_inventory(tmp_path)


def test_source_inventory_rejects_literal_kotlin_signing_secret_but_allows_lookup(
    tmp_path, monkeypatch
):
    release_fixture(tmp_path, monkeypatch)
    build_file = tmp_path / "clients/android/app/build.gradle.kts"
    safe = build_file.read_text(encoding="utf-8") + (
        '\nstorePassword = requiredSigningValue("storePassword")\n'
    )
    build_file.write_text(safe, encoding="utf-8")
    android_source_inventory(tmp_path)
    build_file.write_text(
        safe + '\nkeyPassword = "literal-production-password" // forbidden\n',
        encoding="utf-8",
    )
    with pytest.raises(AndroidReleaseError, match="credential-like content"):
        android_source_inventory(tmp_path)


def test_source_inventory_rejects_symlink_even_at_excluded_output_root(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch)
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / "clients/android/.gradle").symlink_to(target, target_is_directory=True)
    with pytest.raises(AndroidReleaseError, match="symlink directory"):
        android_source_inventory(tmp_path)


def test_source_inventory_propagates_walk_errors_fail_closed(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch)

    def failing_walk(_root, *, topdown, onerror, followlinks):
        assert topdown is True and followlinks is False
        onerror(PermissionError("fixture unreadable"))
        return []

    monkeypatch.setattr(android_release_module.os, "walk", failing_walk)
    with pytest.raises(AndroidReleaseError, match="unreadable"):
        android_source_inventory(tmp_path)


def test_release_build_rejects_stale_apk_version(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch, version_code=VERSION_CODE - 1)
    with pytest.raises(AndroidReleaseError, match="version_code differs"):
        build_attestation(tmp_path)


def test_release_policy_rejects_source_and_apk_downgrade_together(tmp_path, monkeypatch):
    release_fixture(
        tmp_path,
        monkeypatch,
        version_code=VERSION_CODE - 1,
        source_version_code=VERSION_CODE - 1,
    )
    with pytest.raises(AndroidReleaseError, match="version differs from release policy"):
        build_attestation(tmp_path)


def test_release_build_rejects_nonestablished_signer(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch, signer="7" * 64)
    with pytest.raises(AndroidReleaseError, match="established release signer"):
        build_attestation(tmp_path)


def test_release_build_rejects_forged_embedded_source_hash(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch, embedded_tree="1" * 64)
    with pytest.raises(AndroidReleaseError, match="signed source-tree claim"):
        build_attestation(tmp_path)


@pytest.mark.parametrize(
    "badging_extra",
    ["\napplication-debuggable", "\napplication-testOnly", "\napplication-test-only", " testOnly='1'"],
)
def test_release_build_rejects_debuggable_and_testonly_badging(
    tmp_path, monkeypatch, badging_extra
):
    release_fixture(tmp_path, monkeypatch, badging_extra=badging_extra)
    with pytest.raises(AndroidReleaseError, match="debuggable|testOnly"):
        build_attestation(tmp_path)


@pytest.mark.parametrize(
    ("manifest_extra", "message"),
    [
        ("    A: android:debuggable(0x0101000f)=(type 0x12)0xffffffff\n", "debuggable"),
        ("    A: android:testOnly(0x01010272)=(type 0x12)0xffffffff\n", "testOnly"),
    ],
)
def test_release_build_rejects_debuggable_and_testonly_manifest(
    tmp_path, monkeypatch, manifest_extra, message
):
    release_fixture(tmp_path, monkeypatch, manifest_extra=manifest_extra)
    with pytest.raises(AndroidReleaseError, match=message):
        build_attestation(tmp_path)


def test_release_build_requires_explicit_unconfigured_firebase_state(tmp_path, monkeypatch):
    release_fixture(tmp_path, monkeypatch, firebase_state="configured")
    with pytest.raises(AndroidReleaseError, match="Firebase client state"):
        build_attestation(tmp_path)


def test_release_build_rejects_firebase_init_provider(tmp_path, monkeypatch):
    release_fixture(
        tmp_path,
        monkeypatch,
        manifest_extra=(
            "    E: provider\n"
            '      A: android:name(0x01010003)="com.google.firebase.provider.FirebaseInitProvider"\n'
        ),
    )
    with pytest.raises(AndroidReleaseError, match="automatic Firebase initialization"):
        build_attestation(tmp_path)


def test_release_build_rejects_packaged_firebase_client_resources(tmp_path, monkeypatch):
    release_fixture(
        tmp_path,
        monkeypatch,
        resources_extra=(
            "      spec resource 0x7f100001 "
            "com.davidpi.backup:string/google_app_id: flags=0x00000000\n"
        ),
    )
    with pytest.raises(AndroidReleaseError, match="Firebase client resources"):
        build_attestation(tmp_path)


def test_release_build_rejects_secret_like_apk_archive_entry(tmp_path, monkeypatch):
    release_fixture(
        tmp_path,
        monkeypatch,
        apk_entries=(("assets/private/signing.pem", b"not-inspected"),),
    )
    with pytest.raises(AndroidReleaseError, match="forbidden secret-like entry"):
        build_attestation(tmp_path)


def test_release_build_rejects_credential_content_in_benign_apk_entry(
    tmp_path, monkeypatch
):
    release_fixture(
        tmp_path,
        monkeypatch,
        apk_entries=(("assets/config.txt", b"client_secret=real-looking-secret-value"),),
    )
    with pytest.raises(AndroidReleaseError, match="credential-like content"):
        build_attestation(tmp_path)


@pytest.mark.parametrize(
    "metadata_name",
    [
        "attacker.com.davidpi.backup.RELEASE_SOURCE_SHA256",
        "attacker.com.davidpi.backup.RELEASE_FIREBASE_CLIENT_STATE",
    ],
)
def test_release_build_does_not_accept_near_collision_metadata_names(
    tmp_path, monkeypatch, metadata_name
):
    source_name = "com.davidpi.backup.RELEASE_SOURCE_SHA256"
    firebase_name = "com.davidpi.backup.RELEASE_FIREBASE_CLIENT_STATE"
    manifest_extra = (
        "    E: meta-data\n"
        f'      A: android:name(0x01010003)="{metadata_name}"\n'
        f'      A: android:value(0x01010024)="{"1" * 64 if "SOURCE" in metadata_name else "unconfigured"}"\n'
    )
    apk = release_fixture(tmp_path, monkeypatch, manifest_extra=manifest_extra)
    tool = Path(__import__("os").environ["ANDROID_AAPT"])
    text = tool.read_text(encoding="utf-8")
    exact_name = source_name if "SOURCE" in metadata_name else firebase_name
    text = text.replace(
        f'      A: android:name(0x01010003)="{exact_name}"\n',
        "",
        1,
    )
    tool.write_text(text, encoding="utf-8")
    policy_path = tmp_path / "config/android-release.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["verification_tools"]["aapt"]["sha256"] = (
        android_release_module.file_sha256(tool)
    )
    write(policy_path, json.dumps(policy))
    with pytest.raises(AndroidReleaseError, match="source-tree hash|Firebase client state"):
        build_attestation(tmp_path)


@pytest.mark.parametrize(
    "metadata_name",
    [
        "com.davidpi.backup.RELEASE_SOURCE_SHA256",
        "com.davidpi.backup.RELEASE_FIREBASE_CLIENT_STATE",
    ],
)
def test_release_build_rejects_duplicate_metadata_names(tmp_path, monkeypatch, metadata_name):
    manifest_extra = (
        "    E: meta-data\n"
        f'      A: android:name(0x01010003)="{metadata_name}"\n'
        f'      A: android:value(0x01010024)="{"1" * 64 if "SOURCE" in metadata_name else "unconfigured"}"\n'
    )
    release_fixture(tmp_path, monkeypatch, manifest_extra=manifest_extra)
    with pytest.raises(AndroidReleaseError, match="source-tree hash|Firebase client state"):
        build_attestation(tmp_path)


def test_sidecar_inventory_digest_cannot_be_forged(tmp_path, monkeypatch):
    document = attest(tmp_path, monkeypatch)
    document["source"]["release_inputs"]["files"][0]["sha256"] = "9" * 64
    write(tmp_path / ATTESTATION_PATH, json.dumps(document))
    with pytest.raises(AndroidReleaseError, match="inventory digest"):
        verified_android_release(tmp_path, require_source=False, require_tools=False)


def test_candidate_image_bundle_is_extracted_without_execution_and_matches_host(
    tmp_path, monkeypatch
):
    expected = attest(tmp_path, monkeypatch)
    image_root = tmp_path / "candidate-image"
    entrypoint = candidate_image_fixture(tmp_path, image_root)
    commands = []
    container_id = "a" * 64

    def fake_docker(*arguments):
        commands.append(arguments)
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json()
        if arguments[0] == "create":
            return container_id
        if arguments[0] == "cp":
            copy_candidate_member(arguments, image_root, entrypoint)
            return ""
        if arguments[:3] == ("rm", "-f", "-v"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    assert android_image_release.verified_candidate_android_release(
        "registry.invalid/david-pi@sha256:" + "b" * 64, tmp_path
    ) == expected
    assert commands[0][:2] == ("image", "inspect")
    assert commands[1][0] == "create"
    assert [command[0] for command in commands].count("cp") == 2
    assert commands[-1] == ("rm", "-f", "-v", container_id)
    assert all(command[0] != "run" for command in commands)


def test_candidate_image_bundle_rejects_symlinked_member_before_parsing(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    container_id = "c" * 64

    def fake_docker(*arguments):
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json()
        if arguments[0] == "create":
            return container_id
        if arguments[0] == "cp":
            destination = Path(arguments[2])
            if arguments[1].endswith(":/app"):
                destination.mkdir()
                (destination / "config").symlink_to(tmp_path / "config", target_is_directory=True)
            else:
                shutil.copyfile(tmp_path / "docker-entrypoint.sh", destination)
            return ""
        if arguments[:3] == ("rm", "-f", "-v"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match="symlink"):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)


def test_candidate_image_bundle_rejects_legacy_public_apk(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    image_root = tmp_path / "candidate-image"
    entrypoint = candidate_image_fixture(tmp_path, image_root)
    write(image_root / "static/apk/david-pi-backup.apk", b"legacy-public-copy")
    container_id = "d" * 64

    def fake_docker(*arguments):
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json()
        if arguments[0] == "create":
            return container_id
        if arguments[0] == "cp":
            copy_candidate_member(arguments, image_root, entrypoint)
            return ""
        if arguments[:3] == ("rm", "-f", "-v"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match="legacy public APK path"):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)


def test_candidate_image_bundle_rejects_stale_runtime_source(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    image_root = tmp_path / "candidate-image"
    entrypoint = candidate_image_fixture(tmp_path, image_root)
    write(image_root / "modules/runtime.py", "VALUE = 'stale candidate'\n")
    container_id = "e" * 64

    def fake_docker(*arguments):
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json()
        if arguments[0] == "create":
            return container_id
        if arguments[0] == "cp":
            copy_candidate_member(arguments, image_root, entrypoint)
            return ""
        if arguments[:3] == ("rm", "-f", "-v"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match="runtime source differs"):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)


def test_candidate_image_bundle_rejects_ignored_runtime_file(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    write(tmp_path / ".gitignore", "dist/\n")
    image_root = tmp_path / "candidate-image"
    entrypoint = candidate_image_fixture(tmp_path, image_root)
    write(tmp_path / "static/dist/extra.js", "globalThis.injected = true;\n")
    write(image_root / "static/dist/extra.js", "globalThis.injected = true;\n")
    container_id = "f" * 64

    def fake_docker(*arguments):
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json()
        if arguments[0] == "create":
            return container_id
        if arguments[0] == "cp":
            copy_candidate_member(arguments, image_root, entrypoint)
            return ""
        if arguments[:3] == ("rm", "-f", "-v"):
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match="not tracked by Git"):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)


def test_candidate_image_bundle_rejects_live_unsafe_runtime_config(tmp_path, monkeypatch):
    attest(tmp_path, monkeypatch)
    commands = []

    def fake_docker(*arguments):
        commands.append(arguments)
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json(User="0")
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match="user is unsafe"):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)
    assert all(command[0] != "create" for command in commands)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"Healthcheck": {"Test": ["CMD", "/evil"]}}, "healthcheck"),
        ({"Volumes": {"/app": {}}}, "volumes"),
    ],
)
def test_candidate_image_bundle_rejects_hidden_runtime_execution_or_mounts(
    tmp_path, monkeypatch, override, message
):
    attest(tmp_path, monkeypatch)

    def fake_docker(*arguments):
        if arguments[:2] == ("image", "inspect"):
            return candidate_inspect_json(**override)
        raise AssertionError(arguments)

    monkeypatch.setattr(android_image_release, "_docker", fake_docker)
    with pytest.raises(AndroidReleaseError, match=message):
        android_image_release.verified_candidate_android_release("candidate:test", tmp_path)


def test_release_contract_forbids_machine_local_config_and_broad_artifact_copy():
    gradle = (ROOT / "clients/android/app/build.gradle.kts").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert 'rootProject.file("local.properties").exists()' in gradle
    assert "preReleaseBuild" in gradle and "validateDavidPiReleaseSourceSha256" in gradle
    assert "startsWith(repositoryRoot)" in gradle
    assert "signing inputs must stay outside the repository" in gradle
    assert "COPY --chown=10001:10001 artifacts ./artifacts" not in dockerfile
    assert "source=artifacts,target=/release-artifacts,readonly" in dockerfile
    assert "for name in david-pi-backup.apk david-pi-backup.manifest.json" in dockerfile
    assert 'cp "/release-artifacts/android/$name" "/app/artifacts/android/$name"' in dockerfile
    for pattern in (
        "**/local.properties", "**/keystore.properties", "**/*.jks",
        "**/*.keystore", "**/*.p12", "**/*.pfx", "**/.env", "**/.env.*",
        "**/secrets/", "**/*.secret", "**/*.key", "**/*.pem", "**/*.p8",
    ):
        assert pattern in dockerignore
    wrapper = (ROOT / "clients/android/gradle/wrapper/gradle-wrapper.properties").read_text(
        encoding="utf-8"
    )
    assert "distributionSha256Sum=31c55713e40233a8303827ceb42ca48a47267a0ad4bab9177123121e71524c26" in wrapper
