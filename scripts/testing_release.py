#!/usr/bin/env python3
"""Build/test locally, then explicitly publish those same images as a testing release.

No signing is performed. Build creates private candidates; publish defaults to
a dry run and requires reviewed technical acceptance before any remote write.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.android_release import AndroidReleaseError, verified_android_release
from android_image_release import verified_candidate_android_release
from stable_release_gate import source_digest, source_files
from testing_release_gate import BETA_VERSION, validate
from testing_artifacts import (ReleaseError, private_markers, remote_index, remote_platform,
                               require, screen_stream, screen_tar, sha, verify_assets, verify_oci, verify_source_archive)

ARCHITECTURES = ("amd64", "arm64")
APK_FILES = ("david-pi-backup.apk", "david-pi-backup.manifest.json")
FILTER = "SlideshowFfmpegTest or audiobook_cleanup_uses_kernel_renameat2 or audiobook_cleanup_fails_closed_without_renameat2_interface or audiobook_cleanup_syscall_collision_preserves_both_files or audiobook_unlink_preserves_staging_without_renameat2_interface or audiobook_cleanup_unsupported_platform_never_calls_raw_syscall or audiobook_cleanup_release_image_matches_expected_abi"


def command(*args, log=None, timeout=1800, env=None):
    if log:
        with log.open("w") as handle:
            result = subprocess.run(args, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, env=env)
        require(result.returncode == 0, f"Command failed; inspect private log {log.name}")
        return ""
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    require(result.returncode == 0, f"{Path(args[0]).name} failed; rerun the read-only command locally for details")
    return result.stdout.strip()


def write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def now():
    return datetime.now(timezone.utc).isoformat()


def checkout():
    require(not command("git", "status", "--porcelain"), "Commit the reviewed source before building or publishing")
    revision = command("git", "rev-parse", "HEAD")
    version = (ROOT / "VERSION").read_text().strip()
    require(BETA_VERSION.fullmatch(version), "This tool publishes only explicitly versioned beta releases")
    return {"revision": revision, "version": version, "source_sha256": source_digest(ROOT)}


def screen_source(markers):
    command(sys.executable, "scripts/check-public-release.py", *(["--private-markers", str(markers)] if markers else []))
    command(sys.executable, "scripts/check_tracked_secrets.py")


def inspect(image):
    value = json.loads(command("docker", "image", "inspect", image))
    require(len(value) == 1, "Ambiguous local image identity")
    return value[0]


def inspect_build(image, arch, identity, base):
    value = inspect(image)
    labels = value.get("Config", {}).get("Labels", {})
    require(value.get("Architecture") == arch and value.get("Os") == "linux", "Candidate architecture differs")
    expected = {"org.opencontainers.image.version": identity["version"],
                "org.opencontainers.image.revision": identity["revision"],
                "org.opencontainers.image.base.name": base,
                "org.opencontainers.image.base.digest": base.split("@", 1)[1]}
    require(all(labels.get(key) == val for key, val in expected.items()), "Candidate build labels differ from exact source/base")
    return value


def build(args):
    identity = checkout()
    require(re.fullmatch(r"python@sha256:[a-f0-9]{64}", args.python_base), "Use a digest-pinned official Python base")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/david-pi", args.repository), "Invalid candidate repository")
    require(not args.output.exists(), "Use a new private output directory; old evidence is preserved")
    require(args.output.resolve().is_relative_to((ROOT / "work").resolve()), "Candidate output must be inside ignored work/")
    args.output.mkdir(parents=True, mode=0o700)
    out = args.output.resolve()
    screen_source(args.private_markers)
    android = verified_android_release(ROOT, require_source=True, require_tools=True)
    markers = private_markers(args.private_markers, ROOT)
    files = {p.as_posix(): sha(ROOT / p) for p in source_files(ROOT)}
    write(out / "source-inputs.json", {**identity, "files": files})
    assets = out / "assets"
    assets.mkdir()
    source = assets / f'david-pi-{identity["version"]}.tar.gz'
    command(sys.executable, "scripts/package_source.py", "--output", str(source))
    verify_source_archive(source, identity["version"], files)
    context_parent = out / "context"
    context_parent.mkdir()
    with tarfile.open(source) as archive:
        screen_tar(archive, markers)
        archive.extractall(context_parent, filter="data")
    context = context_parent / ("david-pi-" + identity["version"])
    (context / "artifacts/android").mkdir(parents=True, exist_ok=True)
    for name in APK_FILES:
        shutil.copyfile(ROOT / "artifacts/android" / name, assets / name)
        shutil.copyfile(assets / name, context / "artifacts/android" / name)
    result = {"schema_version": 1, "kind": "private-testing-candidate", **identity,
              "created_at": now(), "repository": args.repository, "python_base": args.python_base, "android_apk_sha256": android["artifact"]["sha256"],
              "android_source_sha256": android["source"]["embedded_tree_sha256"], "architectures": {},
              "limitations": ["ARM64 container execution can use software emulation; it is not physical Pi acceptance.",
                              "Local build/runtime receipts do not replace full VM, household, browser, or signed emulator receipts."]}
    for arch in ARCHITECTURES:
        print(f"Building and checking {arch}; detailed output is retained privately.", flush=True)
        arch_out = out / arch
        arch_out.mkdir()
        require(all(sha(context / name) == digest for name, digest in files.items()), "Extracted build source changed")
        platform = "linux/" + arch
        tag = f'david-pi:testing-{identity["revision"][:12]}-{arch}'
        test_tag = tag + "-test"
        command("docker", "pull", "--platform", platform, args.python_base, log=arch_out / "base-pull.log")
        command(sys.executable, "scripts/base_image_provenance.py", "--reference", args.python_base,
                "--architecture", arch, "--output", str(assets / f"base-{arch}.json"), log=arch_out / "base-verification.log")
        common = ["docker", "buildx", "build", "--progress=plain", "--platform", platform,
                  "--provenance=false", "--sbom=false", "--load", "--build-arg", "PYTHON_BASE_IMAGE=" + args.python_base]
        command(*common, "--target", "runtime", "--build-arg", "PYTHON_BASE_DIGEST=" + args.python_base.split("@", 1)[1],
                "--build-arg", "DAVID_PI_VERSION=" + identity["version"], "--build-arg", "DAVID_PI_VCS_REF=" + identity["revision"],
                "--metadata-file", str(arch_out / "build.json"), "-t", tag, str(context), log=arch_out / "build.log")
        runtime = inspect_build(tag, arch, identity, args.python_base)
        # Freeze the runtime ID before building its test-only extension.
        runtime_id = runtime["Id"]
        command(*common, "--target", "runtime-test", "--label", "org.opencontainers.image.revision=" + identity["revision"],
                "-t", test_tag, str(context), log=arch_out / "test-build.log")
        test_image = inspect(test_tag)
        layers = runtime["RootFS"]["Layers"]
        require(test_image["Architecture"] == arch and test_image["RootFS"]["Layers"][:len(layers)] == layers, "Test image is not an extension of the exact runtime layers")
        tests = ["docker", "run", "--rm", "--pull=never", "--platform", platform, "--network=none", "--read-only",
                 "--tmpfs", "/tmp/test:rw,size=128m,mode=0700,uid=10001,gid=10001", "--entrypoint", "python",
                 "-e", "TMPDIR=/tmp/test", "-e", "PHOTO_DATA=/tmp/test", "-e", "DAVID_PI_PLATFORM_DATA=/tmp/test/platform",
                 "-e", "DAVID_PI_EXPECT_RENAMEAT2_ARCH=" + ("x86_64" if arch == "amd64" else "aarch64"),
                 test_image["Id"], "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_slideshow_ffmpeg.py", "tests/test_app.py", "-k", FILTER]
        command(*tests, log=arch_out / "media-abi-tests.log")
        runtime_command = ["docker", "run", "--rm", "--pull=never", "--platform", platform, "--network=none", "--read-only"]
        command(*runtime_command, "--user", "10002:10001", "--entrypoint", "python", runtime_id,
                "-c", 'import modules.maintenance_worker; print("Maintenance UID import passed")', log=arch_out / "maintenance.log")
        verified_candidate_android_release(runtime_id, ROOT)
        command(*runtime_command, "--entrypoint", "python", runtime_id, "-c",
                'import importlib.metadata as m,json; print(json.dumps(sorted([{ "name":d.metadata["Name"], "version":d.version} for d in m.distributions()], key=lambda x:x["name"].lower())))', log=arch_out / "python.json")
        command(*runtime_command, "--entrypoint", "awk", runtime_id, "-F:",
                '$1=="P"{p=$2} $1=="V"{v=$2} $1=="A"{a=$2} $0==""{if(p&&v&&a) print p "\t" v "\t" a; p=v=a=""} END{if(p&&v&&a) print p "\t" v "\t" a}',
                "/lib/apk/db/installed", log=arch_out / "os.tsv")
        oci = arch_out / "image.oci.tar"
        command("docker", "image", "save", "-o", str(oci), runtime_id, log=arch_out / "export.log")
        exported = verify_oci(oci, arch, markers)
        require(exported["diff_ids"] == layers, "Exported image differs from tested runtime")
        command(sys.executable, "scripts/generate_sbom.py", "--python-packages", str(arch_out / "python.json"),
                "--os-packages", str(arch_out / "os.tsv"), "--os-package-type", "apk", "--image", "david-pi@" + exported["manifest_digest"],
                "--revision", identity["revision"], "--output", str(assets / f"sbom-{arch}.cdx.json"), log=arch_out / "sbom.log")
        result["architectures"][arch] = {"status": "pass", "local_image_id": runtime_id, "test_image_id": test_image["Id"],
            "oci": str(oci.relative_to(out)), "oci_sha256": sha(oci), "image": exported,
            "test_log_sha256": sha(arch_out / "media-abi-tests.log"), "maintenance_log_sha256": sha(arch_out / "maintenance.log"),
            "application_android_verified": True, "production_test_layers_verified": True}
        write(arch_out / "runtime-receipt.json", {**identity, **result["architectures"][arch],
              "scope": "Local candidate; not registry download or physical hardware acceptance"})
        downloads = arch_out / "private-downloads"
        downloads.mkdir()
        os.link(source, downloads / source.name)
        environment = dict(os.environ, GITHUB_REPOSITORY=args.repository, DAVID_PI_IMAGE_DIGEST=exported["manifest_digest"])
        command("bash", "scripts/build-release-metadata.sh", str(downloads), env=environment)
        print(f"{arch} private bootstrap files and exact OCI are ready. Use only with verified local OCI preload.", flush=True)
    require(checkout() == identity, "Source changed while building; discard this candidate")
    require(all(sha(context / name) == digest for name, digest in files.items()), "Extracted build source changed during testing")
    require(verified_android_release(ROOT, require_source=True, require_tools=True) == android, "Android changed while building")
    result["files"] = {p.relative_to(out).as_posix(): sha(p) for p in sorted(out.rglob("*")) if p.is_file() and not p.is_relative_to(context_parent)}
    write(out / "candidate.json", result)
    print("Private candidates verified. Full beta acceptance and an explicit publication step are still required.")


def candidate(args):
    record = json.loads((args.output / "candidate.json").read_text())
    kind = "testing-candidate-transfer" if getattr(args, "transfer", False) else "private-testing-candidate"
    require(record.get("schema_version") == 1 and record.get("kind") == kind, "Unsupported candidate record")
    current = checkout()
    require(all(record.get(key) == value for key, value in current.items()), "Candidate differs from the current clean source")
    screen_source(args.private_markers)
    android = verified_android_release(ROOT, require_source=True, require_tools=True)
    require(record["android_apk_sha256"] == android["artifact"]["sha256"], "Candidate uses another Android APK")
    require(record["android_source_sha256"] == android["source"]["embedded_tree_sha256"], "Candidate uses another Android source")
    require(set(record.get("architectures", {})) == set(ARCHITECTURES), "Both tested architectures are required")
    for name, digest in record["files"].items():
        path = args.output / name
        require(path.resolve().is_relative_to(args.output.resolve()) and path.is_file() and not path.is_symlink(), "Unsafe candidate evidence path")
        require(sha(path) == digest, "Candidate artifact or retained test evidence changed")
    verify_assets(args.output / "assets", asset_hashes(record))
    verify_source_archive(args.output / "assets" / f'david-pi-{current["version"]}.tar.gz', current["version"],
                          {p.as_posix(): sha(ROOT / p) for p in source_files(ROOT)})
    markers = private_markers(args.private_markers, ROOT)
    for arch, item in record["architectures"].items():
        require(item["status"] == "pass" and item["application_android_verified"] is True and item["production_test_layers_verified"] is True, "Candidate runtime receipt is incomplete")
        require(verify_oci(args.output / item["oci"], arch, markers) == item["image"], "Candidate OCI receipt differs")
        runtime = inspect_build(item["local_image_id"], arch, current, record["python_base"])
        require(runtime["RootFS"]["Layers"] == item["image"]["diff_ids"], "Local image no longer matches tested layers")
        verified_candidate_android_release(item["local_image_id"], ROOT)
    evidence = json.loads(args.evidence.read_text())
    errors = validate(evidence, ROOT)
    require(not errors, "Testing acceptance is incomplete: " + "; ".join(errors))
    with args.evidence.open("rb") as handle:
        screen_stream(handle, markers)
    return record, evidence


def asset_hashes(record):
    names = {f'david-pi-{record["version"]}.tar.gz', *APK_FILES,
             *[f"base-{arch}.json" for arch in ARCHITECTURES],
             *[f"sbom-{arch}.cdx.json" for arch in ARCHITECTURES]}
    require(all("assets/" + name in record.get("files", {}) for name in names), "Candidate is missing required release assets")
    return {name: record["files"]["assets/" + name] for name in names}


def release_notes(record, evidence, repository):
    version = record["version"]
    return (f"David-Pi {version} — testing prerelease\n\n"
            "This beta is for willing testers using a separate test machine and test content. "
            "It is not a stable release or an upgrade for an existing household server.\n\n"
            f"Start with [the beta instructions](https://github.com/{repository}/blob/v{version}/docs/TESTING.md). "
            "Use this release's installer; it selects this exact beta. Normal stable installers and update checks do not select betas.\n\n"
            "Known limitations:\n\n" + "".join("- " + item + "\n" for item in evidence["known_limitations"]) +
            "\nPhysical Raspberry Pi, physical Android, modern offline Android, and unaided newcomer acceptance remain pending. "
            "Please follow the feedback checklist in the beta instructions.\n\n"
            f"Public source SHA-256: `{record['source_sha256']}`\n\n"
            f"Signed APK SHA-256: `{record['android_apk_sha256']}`\n")


def public_assets(args, record, evidence, image_digest):
    assets = args.output / "publication-assets"
    require(not assets.exists(), "Publication assets already exist; review the earlier attempt before retrying")
    original = asset_hashes(record)
    verify_assets(args.output / "assets", original)
    assets.mkdir()
    for name in sorted(original):
        shutil.copyfile(args.output / "assets" / name, assets / name)
    verify_assets(assets, original)
    environment = dict(os.environ, GITHUB_REPOSITORY=args.repository, DAVID_PI_IMAGE_DIGEST=image_digest)
    command("bash", "scripts/build-release-metadata.sh", str(assets), env=environment)
    shutil.copyfile(args.evidence, assets / "testing-acceptance.json")
    public = {key: record[key] for key in ("schema_version", "revision", "version", "source_sha256", "android_apk_sha256", "android_source_sha256", "python_base", "limitations")}
    public["kind"] = "testing-release-runtime"
    public["image"] = "ghcr.io/" + args.repository.lower() + "@" + image_digest
    public["architectures"] = {arch: {key: value for key, value in item.items() if key not in {"oci", "oci_sha256"}} for arch, item in record["architectures"].items()}
    write(assets / "runtime-verification.json", public)
    (assets / "TESTING.md").write_text((ROOT / "docs/TESTING.md").read_text())
    (assets / "release-notes.md").write_text(release_notes(record, evidence, args.repository))
    (assets / "david-pi-backup.apk.sha256").write_text(sha(assets / APK_FILES[0]) + "  " + APK_FILES[0] + "\n")
    names = set(original) | {"install.sh", "install.sh.sha256", "release-manifest.txt", "testing-acceptance.json",
                             "runtime-verification.json", "TESTING.md", "release-notes.md", "david-pi-backup.apk.sha256"}
    files = {name: sha(assets / name) for name in names}
    verify_assets(assets, files)
    (assets / "SHA256SUMS").write_text("".join(digest + "  " + name + "\n" for name, digest in sorted(files.items())))
    files["SHA256SUMS"] = sha(assets / "SHA256SUMS")
    verify_assets(assets, files)
    markers = private_markers(args.private_markers, ROOT)
    for path in assets.iterdir():
        if path.suffix not in {".gz", ".apk"}:
            with path.open("rb") as handle:
                screen_stream(handle, markers)
    return assets, files


def publish(args):
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/david-pi", args.repository), "Invalid publication repository")
    record, evidence = candidate(args)  # All gates precede every remote write.
    require(record.get("repository", "").lower() == args.repository.lower(), "Publication repository differs from the prepared candidate")
    tag = "v" + record["version"]
    registry = "ghcr.io/" + args.repository.lower()
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "repository": args.repository, "tag": tag,
                          "source_revision": record["revision"], "android_apk_sha256": record["android_apk_sha256"],
                          "image_tags": [registry + ":" + tag + "-" + arch for arch in ARCHITECTURES] + [registry + ":" + tag],
                          "prerelease": True, "latest": False, "remote_writes": False}, indent=2))
        return
    # A missing release/tag is acceptable; authentication/network failures are
    # never treated as absence. GitHub API must explicitly report a 404.
    command("gh", "auth", "status")
    repo = json.loads(command("gh", "api", "repos/" + args.repository))
    require(repo.get("full_name", "").lower() == args.repository.lower() and repo.get("private") is False, "Select the intended existing public repository")
    staged = getattr(args, "transfer", False)
    if staged:
        draft = json.loads(command("gh", "api", "repos/" + args.repository + "/releases/tags/" + tag))
        require(draft.get("draft") is True and draft.get("prerelease") is True and draft.get("target_commitish") == record["revision"], "Staged release is not the exact private beta draft")
        require({item.get("name") for item in draft.get("assets", [])} == {"testing-candidate.tar"}, "Unexpected staged draft assets")
    else:
        for endpoint in ("releases/tags/" + tag, "git/ref/tags/" + tag):
            result = subprocess.run(["gh", "api", "repos/" + args.repository + "/" + endpoint], cwd=ROOT, capture_output=True, text=True)
            require(result.returncode != 0 and "(HTTP 404)" in result.stderr, "Release/tag already exists or absence could not be verified; never overwrite a beta")
    # The exact source commit must already be reachable in this GitHub repository.
    remote_commit = json.loads(command("gh", "api", "repos/" + args.repository + "/commits/" + record["revision"]))
    require(remote_commit.get("sha") == record["revision"], "Exact candidate source is not on GitHub")
    remote = {}
    state = {"source": record["revision"], "tag": tag, "repository": args.repository,
             "stage": "registry-publishing", "prerelease": True, "latest": False, "completed": False}
    write(args.output / "publication-receipt.json", state)
    for arch, item in record["architectures"].items():
        destination = registry + ":" + tag + "-" + arch
        # Refuse existing version tags, including a failed prior publication.
        absent = subprocess.run(["docker", "buildx", "imagetools", "inspect", destination], cwd=ROOT, capture_output=True, text=True)
        require(absent.returncode != 0 and ("not found" in absent.stderr.lower() or "manifest unknown" in absent.stderr.lower()), "Registry tag exists or its absence could not be verified")
        command("docker", "tag", item["local_image_id"], destination)
        command("docker", "push", "--platform", "linux/" + arch, destination, log=args.output / f"publish-{arch}.log")
        descriptor = json.loads(command("docker", "buildx", "imagetools", "inspect", destination, "--format", "{{json .Manifest}}"))
        digest = descriptor.get("digest", "")
        require(re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "Registry platform digest is missing")
        manifest = json.loads(command("docker", "buildx", "imagetools", "inspect", registry + "@" + digest, "--raw"))
        remote_platform(manifest, arch, item["image"])
        require(digest == item["image"]["manifest_digest"], "Registry changed the tested platform manifest; publication stopped")
        remote[arch] = digest
    index_tag = registry + ":" + tag
    absent = subprocess.run(["docker", "buildx", "imagetools", "inspect", index_tag], cwd=ROOT, capture_output=True, text=True)
    require(absent.returncode != 0 and ("not found" in absent.stderr.lower() or "manifest unknown" in absent.stderr.lower()), "Registry version index already exists or absence could not be verified")
    command("docker", "buildx", "imagetools", "create", "--tag", index_tag, *[registry + "@" + remote[arch] for arch in ARCHITECTURES])
    descriptor = json.loads(command("docker", "buildx", "imagetools", "inspect", index_tag, "--format", "{{json .Manifest}}"))
    digest = remote_index(descriptor, remote)
    # A newly created GHCR package can still be private even when its source
    # repository is public. Check every manifest with an empty Docker config;
    # friends must not need the maintainer's registry credentials.
    with tempfile.TemporaryDirectory(prefix="beta-anonymous-registry-", dir=args.output) as temporary:
        for anonymous_digest in [digest, *remote.values()]:
            command("docker", "--config", temporary, "manifest", "inspect", registry + "@" + anonymous_digest)
    assets, asset_files = public_assets(args, record, evidence, digest)
    verify_assets(assets, asset_files)
    if staged:
        command("gh", "release", "upload", tag, *[str(assets / name) for name in sorted(asset_files)], "--repo", args.repository)
        command("gh", "release", "edit", tag, "--repo", args.repository, "--draft=true", "--prerelease", "--latest=false",
                "--title", "David-Pi " + record["version"] + " (testing beta)", "--notes-file", str(assets / "release-notes.md"))
        command("gh", "release", "delete-asset", tag, "testing-candidate.tar", "--repo", args.repository, "--yes")
    else:
        command("gh", "release", "create", tag, *[str(assets / name) for name in sorted(asset_files)], "--repo", args.repository,
                "--draft", "--prerelease", "--latest=false", "--target", record["revision"], "--title", "David-Pi " + record["version"] + " (testing beta)", "--notes-file", str(assets / "release-notes.md"))
    state.update(stage="private-draft", image=registry + "@" + digest)
    write(args.output / "publication-receipt.json", state)
    with tempfile.TemporaryDirectory(prefix="beta-download-check-", dir=args.output) as temporary:
        command("gh", "release", "download", tag, "--repo", args.repository, "--dir", temporary)
        downloaded = Path(temporary)
        verify_assets(downloaded, asset_files)
    require(checkout()["source_sha256"] == record["source_sha256"], "Source changed during publication; draft remains private")
    command("gh", "release", "edit", tag, "--repo", args.repository, "--draft=false", "--prerelease", "--latest=false")
    state.update(stage="published-downloads-unverified")
    write(args.output / "publication-receipt.json", state)
    release = json.loads(command("gh", "api", "repos/" + args.repository + "/releases/tags/" + tag))
    require(release.get("prerelease") is True and release.get("draft") is False, "GitHub release flags differ")
    require({item.get("name") for item in release.get("assets", [])} == set(asset_files), "Public release asset inventory differs")
    # curl --disable suppresses user .curlrc configuration. No GitHub token or
    # authenticated gh download is used for the final newcomer download check.
    with tempfile.TemporaryDirectory(prefix="beta-public-download-", dir=args.output) as temporary:
        for name in sorted(asset_files):
            url = "https://github.com/" + args.repository + "/releases/download/" + tag + "/" + name
            command("curl", "--disable", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https",
                    "--tlsv1.2", "--connect-timeout", "15", "--max-time", "300", url, "--output", str(Path(temporary) / name))
        verify_assets(Path(temporary), asset_files)
    state.update(stage="published-downloads-verified", url=release["html_url"], completed=True, completed_at=now(), assets=asset_files)
    write(args.output / "publication-receipt.json", state)
    print(release["html_url"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("build", "publish"):
        item = sub.add_parser(name)
        item.add_argument("--output", type=Path, required=True)
        item.add_argument("--private-markers", type=Path)
        if name == "build":
            item.add_argument("--python-base", required=True)
            item.add_argument("--repository", default="divad815-gif/david-pi")
        else:
            item.add_argument("--repository", required=True)
            item.add_argument("--evidence", type=Path, default=ROOT / "docs/release-evidence/testing.json")
            item.add_argument("--execute", action="store_true", help="Actually publish after the same checks as the default dry run")
    args = parser.parse_args()
    args.output = args.output.resolve()
    try:
        build(args) if args.action == "build" else publish(args)
    except (ReleaseError, AndroidReleaseError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("Testing release stopped: " + str(error), file=sys.stderr)
        if args.action == "publish" and args.execute and (args.output / "publication-receipt.json").exists():
            print("Remote artifacts may already exist. Inspect the private publication receipt; do not overwrite or expose a draft by hand.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
