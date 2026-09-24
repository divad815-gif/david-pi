#!/usr/bin/env python3
"""Publish verified beta OCI content; leave the private release for local finalization.

This publisher runs outside the immutable product checkout. It retains that
checkout's validators and changes only draft discovery and the explicit stop
before GitHub asset generation or release edits. No signing or rebuilding occurs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


class RegistryReady(Exception):
    """The verified registry phase completed; local release work remains."""


def require(condition, message):
    if not condition:
        raise ValueError(message)


def draft_command(original, repository, tag, revision, release_id):
    """Adapt only the selected read; all other commands retain their implementation."""
    selected = ("gh", "api", f"repos/{repository}/releases/tags/{tag}")
    endpoint = f"repos/{repository}/releases/{release_id}"
    api_url = "https://api.github.com/" + endpoint

    def command(*args, **kwargs):
        if args != selected:
            return original(*args, **kwargs)
        require(not kwargs, "Unexpected options on selected release lookup")
        view = json.loads(original("gh", "release", "view", tag, "--repo", repository,
                                   "--json", "apiUrl,tagName,targetCommitish"))
        require(view.get("apiUrl") == api_url and view.get("tagName") == tag
                and view.get("targetCommitish") == revision,
                "Draft discovery differs from the selected release/source")
        raw = original("gh", "api", endpoint)
        release = json.loads(raw)
        require(type(release.get("id")) is int and release["id"] == release_id
                and release.get("url") == api_url and release.get("tag_name") == tag
                and release.get("target_commitish") == revision,
                "Release ID lookup differs from the selected release/source")
        # Existing promote/publish checks still require private/prerelease state
        # and the exact staging asset. Discovery does not waive those checks.
        return raw

    return command


def registry_boundary(args, record, evidence, image_digest, pins, write):
    """Called only where the original publisher would begin GitHub asset work."""
    require(record["revision"] == pins.expected_revision,
            "Registry receipt source changed")
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", image_digest),
            "Registry index digest is invalid")
    require(record.get("local_candidate_receipt_sha256") == pins.candidate_receipt_sha256,
            "Transferred candidate receipt differs from the reviewed candidate")
    acceptance_sha = hashlib.sha256(args.evidence.read_bytes()).hexdigest()
    require(acceptance_sha == pins.acceptance_sha256,
            "Acceptance evidence differs from the reviewed file")
    receipt = {
        "schema_version": 1,
        "kind": "testing-registry-transfer",
        "stage": "registry-verified-awaiting-local-finalization",
        "completed": False,
        "registry_completed": True,
        "release_exposed": False,
        "source": record["revision"],
        "source_sha256": record["source_sha256"],
        "tag": pins.tag,
        "repository": pins.repository,
        "release_id": pins.release_id,
        "image": "ghcr.io/" + pins.repository.lower() + "@" + image_digest,
        "platforms": {arch: item["image"]["manifest_digest"]
                      for arch, item in record["architectures"].items()},
        "transfer_sha256": pins.expected_transfer_sha256,
        "local_candidate_receipt_sha256": pins.candidate_receipt_sha256,
        "acceptance_sha256": acceptance_sha,
        "publisher_revision": pins.publisher_revision,
        "publisher_driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Exact OCI publication and anonymous manifest checks only. The private staging release and asset are unchanged; local asset/download verification and final exposure remain required.",
    }
    write(args.output / "publication-receipt.json", receipt)
    raise RegistryReady


def run(args):
    source = args.source.resolve()
    require(re.fullmatch(r"[A-Za-z0-9_.-]+/david-pi", args.repository), "Invalid repository")
    require(args.release_id > 0, "Invalid selected release ID")
    for name in ("expected_revision", "publisher_revision"):
        require(re.fullmatch(r"[a-f0-9]{40}", getattr(args, name)), "Invalid source revision")
    for name in ("expected_transfer_sha256", "candidate_receipt_sha256", "acceptance_sha256"):
        require(re.fullmatch(r"[a-f0-9]{64}", getattr(args, name)), "Invalid reviewed checksum")
    publisher = Path(__file__).resolve().parents[1]
    require(source != publisher, "Use a separate immutable product checkout")
    publisher_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=publisher, text=True).strip()
    require(publisher_revision == args.publisher_revision, "Publisher checkout differs")
    sys.path.insert(0, str(source / "scripts"))
    import testing_transfer as transfer
    import testing_release as release
    require(Path(transfer.__file__).resolve().parent == source / "scripts"
            and Path(release.__file__).resolve().parent == source / "scripts"
            and transfer.ROOT == source and release.ROOT == source,
            "Publication modules must come from the immutable product checkout")
    identity, note = transfer.tag_identity(args.tag)
    require(identity["revision"] == args.expected_revision
            and note["transfer_sha256"] == args.expected_transfer_sha256,
            "Annotated beta tag differs from the reviewed transfer/source")
    original = release.command
    adapter = draft_command(original, args.repository, args.tag,
                            args.expected_revision, args.release_id)
    transfer.command = adapter
    release.command = adapter
    release.public_assets = lambda a, r, e, d: registry_boundary(a, r, e, d, args, release.write)
    args.output = args.output.resolve()
    args.private_markers = None
    try:
        transfer.promote(args)
    except RegistryReady:
        print("Exact registry content verified. Private release left unchanged for local finalization.")
        return 0
    raise ValueError("Registry-only publisher did not stop at its required boundary")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    for name in ("repository", "tag", "expected-revision", "expected-transfer-sha256",
                 "candidate-receipt-sha256", "acceptance-sha256", "publisher-revision"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--release-id", type=int, required=True)
    try:
        return run(parser.parse_args())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("Registry publication stopped: " + str(error), file=sys.stderr)
        print("The release remains private; inspect retained registry progress before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
