#!/usr/bin/env python3
"""Transfer already-tested beta images to GitHub Actions without signing or rebuilding."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

from testing_artifacts import ReleaseError, require, sha, screen_stream, private_markers, verify_oci
from testing_release import (APK_FILES, ARCHITECTURES, ROOT, asset_hashes, candidate, checkout,
                             command, publish, write)
from testing_release_gate import BETA_VERSION


def transfer_names(version):
    return {"candidate.json", "evidence.json", f"assets/david-pi-{version}.tar.gz",
            *["assets/" + name for name in APK_FILES],
            *[f"assets/base-{arch}.json" for arch in ARCHITECTURES],
            *[f"assets/sbom-{arch}.cdx.json" for arch in ARCHITECTURES],
            *[f"{arch}/image.oci.tar" for arch in ARCHITECTURES]}


def pack_transfer(args, record):
    destination = args.output / "testing-candidate.tar"
    temporary = args.output / "testing-candidate.tar.new"
    require(not temporary.exists(), "An interrupted transfer build exists; inspect it before retrying")
    paths = {"assets/" + name: args.output / "assets" / name for name in asset_hashes(record)}
    paths.update({f"{arch}/image.oci.tar": args.output / record["architectures"][arch]["oci"] for arch in ARCHITECTURES})
    paths["evidence.json"] = args.evidence
    copied = {key: copy.deepcopy(record[key]) for key in (
        "schema_version", "revision", "version", "source_sha256", "created_at", "repository", "python_base",
        "android_apk_sha256", "android_source_sha256", "architectures", "limitations")}
    copied["kind"] = "testing-candidate-transfer"
    copied["local_candidate_receipt_sha256"] = sha(args.output / "candidate.json")
    copied["files"] = {name: sha(path) for name, path in paths.items()}
    for arch in ARCHITECTURES:
        copied["architectures"][arch]["oci"] = f"{arch}/image.oci.tar"
    payload = (json.dumps(copied, indent=2, sort_keys=True) + "\n").encode()
    screen_stream(io.BytesIO(payload), private_markers(args.private_markers, ROOT))
    require(set(paths) | {"candidate.json"} == transfer_names(record["version"]), "Unexpected transfer asset selection")
    require(sum(path.stat().st_size for path in paths.values()) + len(payload) < 1900 * 1024**2, "Transfer exceeds the bounded GitHub asset size")
    with tarfile.open(temporary, "w") as archive:
        for name in sorted(set(paths) | {"candidate.json"}):
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.mtime = 0
            if name == "candidate.json":
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                info.size = paths[name].stat().st_size
                with paths[name].open("rb") as stream:
                    archive.addfile(info, stream)
    if destination.exists():
        require(not destination.is_symlink() and sha(destination) == sha(temporary), "Prior transfer bundle differs; preserve it and use a new candidate directory")
        temporary.unlink()
    else:
        temporary.replace(destination)
    return destination


def extract_transfer(bundle, output, expected_sha, identity):
    require(re.fullmatch(r"[a-f0-9]{64}", expected_sha) and sha(bundle) == expected_sha, "Staged bundle differs from the annotated tag checksum")
    require(not output.exists(), "Use an empty transfer extraction directory")
    with tarfile.open(bundle) as archive:
        entries = archive.getmembers()
        require(len(entries) == len(transfer_names(identity["version"])), "Unexpected transfer file count")
        require(all(entry.isfile() for entry in entries), "Transfer contains a symlink, directory or special file")
        require({entry.name for entry in entries} == transfer_names(identity["version"]), "Transfer paths differ from the allowlist")
        require(sum(entry.size for entry in entries) < 1900 * 1024**2, "Transfer is too large")
        metadata = archive.getmember("candidate.json")
        require(metadata.size < 1024**2, "Transfer metadata is too large")
        record = json.load(archive.extractfile(metadata))
        require(record.get("kind") == "testing-candidate-transfer" and record.get("schema_version") == 1, "Unexpected transfer schema")
        require(all(record.get(key) == value for key, value in identity.items()), "Transfer source differs from checked out tag")
        require(set(record.get("architectures", {})) == set(ARCHITECTURES), "Transfer must contain exactly both supported architectures")
        require(all(record["architectures"][arch].get("oci") == f"{arch}/image.oci.tar" for arch in ARCHITECTURES), "Transfer image paths differ from the allowlist")
        require(set(record.get("files", {})) == transfer_names(identity["version"]) - {"candidate.json"}, "Transfer checksum inventory differs")
        import hashlib
        for entry in entries:
            if entry.name != "candidate.json":
                require(hashlib.file_digest(archive.extractfile(entry), "sha256").hexdigest() == record["files"][entry.name], "Transfer file checksum differs")
        output.mkdir(parents=True, mode=0o700)
        archive.extractall(output, filter="data")
    return record


def stage(args):
    record, _ = candidate(args)  # Technical gate, exact source/APK, and all OCI checks first.
    require(record["repository"].lower() == args.repository.lower(), "Staging repository differs from candidate")
    bundle = pack_transfer(args, record)
    note = {"kind": "testing-release-transfer", "version": record["version"], "revision": record["revision"], "transfer_sha256": sha(bundle)}
    write(args.output / "testing-tag-message.json", note)
    print(json.dumps({**note, "mode": "stage" if args.execute else "dry-run", "remote_writes": bool(args.execute)}, indent=2))
    if not args.execute:
        return
    for endpoint in ("releases/tags/v" + record["version"], "git/ref/tags/v" + record["version"]):
        result = subprocess.run(["gh", "api", "repos/" + args.repository + "/" + endpoint], cwd=ROOT, capture_output=True, text=True)
        require(result.returncode != 0 and "(HTTP 404)" in result.stderr, "Existing release/tag or unverified absence; staging will not overwrite it")
    remote = json.loads(command("gh", "api", "repos/" + args.repository + "/commits/" + record["revision"]))
    require(remote.get("sha") == record["revision"], "Candidate source is not available on GitHub")
    command("gh", "release", "create", "v" + record["version"], str(bundle), "--repo", args.repository,
            "--draft", "--prerelease", "--latest=false", "--target", record["revision"],
            "--title", "David-Pi " + record["version"] + " — private staging",
            "--notes", "Private transfer of technically accepted candidates. Do not expose manually; the beta tag workflow verifies and replaces staging assets.")
    write(args.output / "staging-receipt.json", {**note, "repository": args.repository, "stage": "private-draft-awaiting-annotated-tag"})
    print("Private draft staged. Review the tag message, then explicitly create and push the annotated beta tag to start promotion.")


def tag_identity(tag):
    identity = checkout()
    require(tag == "v" + identity["version"] and BETA_VERSION.fullmatch(identity["version"]), "Only the exact canonical beta tag can promote")
    ref = "refs/tags/" + tag
    require(command("git", "cat-file", "-t", ref) == "tag", "Promotion requires an annotated tag binding the staged checksum")
    require(command("git", "rev-parse", ref + "^{commit}") == identity["revision"], "Tag and checkout differ")
    raw = command("git", "cat-file", "tag", ref)
    header, body = raw.split("\n\n", 1)
    require("object " + identity["revision"] in header.splitlines() and "type commit" in header.splitlines(), "Tag must directly identify the candidate commit")
    note = json.loads(body)
    require(set(note) == {"kind", "version", "revision", "transfer_sha256"} and note["kind"] == "testing-release-transfer", "Unexpected tag handoff metadata")
    require(note["version"] == identity["version"] and note["revision"] == identity["revision"], "Tag handoff source differs")
    require(re.fullmatch(r"[a-f0-9]{64}", str(note["transfer_sha256"])), "Tag handoff checksum is invalid")
    return identity, note


def promote(args):
    identity, note = tag_identity(args.tag)
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/david-pi", args.repository), "Invalid repository")
    args.output.mkdir(parents=True, mode=0o700)
    draft = json.loads(command("gh", "api", "repos/" + args.repository + "/releases/tags/" + args.tag))
    require(draft.get("draft") is True and draft.get("prerelease") is True and draft.get("target_commitish") == identity["revision"], "Promotion requires the exact private staged draft")
    require({item.get("name") for item in draft.get("assets", [])} == {"testing-candidate.tar"}, "Unexpected staging assets")
    command("gh", "release", "download", args.tag, "--repo", args.repository, "--pattern", "testing-candidate.tar", "--dir", str(args.output))
    record = extract_transfer(args.output / "testing-candidate.tar", args.output / "candidate", note["transfer_sha256"], identity)
    args.output = args.output / "candidate"
    args.evidence = args.output / "evidence.json"
    args.transfer = True
    args.execute = True
    (ROOT / "artifacts/android").mkdir(parents=True, exist_ok=True)
    for name in APK_FILES:
        shutil.copyfile(args.output / "assets" / name, ROOT / "artifacts/android" / name)
    # Inspect all bytes before importing the original OCI; no Docker build occurs.
    for arch, item in record["architectures"].items():
        path = args.output / arch / "image.oci.tar"
        require(verify_oci(path, arch) == item["image"], "Transferred OCI differs from the tested platform receipt")
        command("docker", "image", "load", "-i", str(path))
    publish(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "promote"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--private-markers", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    args.output = args.output.resolve()
    try:
        if args.action == "stage":
            require(args.evidence is not None, "Stage requires reviewed beta evidence")
            stage(args)
        else:
            require(args.tag is not None, "Promotion requires the pushed annotated tag")
            promote(args)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        print("Testing transfer stopped: " + str(error), file=sys.stderr)
        print("A draft or partial registry publication may remain; inspect retained receipts before retrying.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
