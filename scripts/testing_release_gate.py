#!/usr/bin/env python3
"""Require technical beta acceptance without pretending field testing is complete."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from stable_release_gate import ROOT, source_digest

BETA_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)-beta\.[1-9][0-9]*")
REQUIRED = {
    "vm-debian13-amd64": "full-vm",
    "vm-ubuntu2404-amd64": "full-vm",
    "install-resume-reboot": "full-vm",
    "household-admission-isolation": "integration",
    "local-modules-enable-disable": "integration",
    "storage-update-recovery": "full-vm",
    "independent-backup-clean-restore": "full-vm",
    "browser-setup-accessibility": "browser",
    "android-signed-emulator-smoke": "emulator",
}
PENDING = {
    "hardware-pi4", "hardware-pi5", "android-pair-backup-offline-reconnect",
    "android-signed-update", "newcomer-unaided-install",
}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def text_list(value, *, nonempty=False):
    return isinstance(value, list) and (bool(value) or not nonempty) and all(text(item) for item in value)


def validate(document, root, *, require_android=True):
    errors = []
    if not isinstance(document, dict):
        return ["testing acceptance must be an object"]
    if (type(document.get("schema_version")) is not int or document["schema_version"] != 1
            or document.get("kind") != "testing-release-acceptance"):
        errors.append("unsupported testing acceptance schema")
    version = (root / "VERSION").read_text().strip()
    if not BETA_VERSION.fullmatch(version):
        errors.append("testing publication requires a version such as 10.0.0-beta.1")
    if document.get("version") != version:
        errors.append("testing evidence version differs from VERSION")
    expected_source = source_digest(root)
    if document.get("source_sha256") != expected_source:
        errors.append("testing evidence differs from current public source")
    if not text(document.get("reviewer")) or not text(document.get("reviewed_at")):
        errors.append("testing evidence requires reviewer and review timestamp")
    if not text_list(document.get("known_limitations"), nonempty=True):
        errors.append("testing release must explain its known limitations")
    pending = document.get("pending_acceptance")
    if (not isinstance(pending, list) or not all(isinstance(item, str) for item in pending)
            or len(pending) != len(PENDING) or set(pending) != PENDING):
        errors.append("physical-device and newcomer acceptance must be explicitly pending")
    checks = document.get("checks")
    checks = checks if isinstance(checks, list) else []
    if len(checks) != len(REQUIRED) or any(not isinstance(check, dict) or check.get("id") not in REQUIRED for check in checks):
        errors.append("testing acceptance must contain exactly the required technical checks")
    for name, environment in REQUIRED.items():
        matches = [check for check in checks if isinstance(check, dict) and check.get("id") == name]
        if len(matches) != 1:
            errors.append(f"{name}: exactly one receipt is required")
            continue
        check = matches[0]
        if check.get("status") != "pass" or check.get("environment") != environment:
            errors.append(f"{name}: actual {environment} pass is required")
        if check.get("source_sha256") != expected_source:
            errors.append(f"{name}: receipt belongs to another source")
        if check.get("implementation") != "actual":
            errors.append(f"{name}: mocked implementation cannot satisfy acceptance")
        for field in ("performed_by", "completed_at", "notes", "fixture_scope"):
            if not text(check.get(field)):
                errors.append(f"{name}: {field} must be recorded")
        if not text_list(check.get("limitations")):
            errors.append(f"{name}: limitations must be recorded as a list")
        if not re.fullmatch(r"[a-f0-9]{64}", str(check.get("evidence_sha256", ""))):
            errors.append(f"{name}: retained evidence hash is required")
        if environment == "full-vm":
            for field in ("actual_services", "actual_filesystems", "default_timings"):
                if check.get(field) is not True:
                    errors.append(f"{name}: {field} must pass without test substitutions")
        if name.startswith("vm-") and check.get("real_tailscale") is not True:
            errors.append(f"{name}: real Tailscale enrollment and access are required")
        if name == "household-admission-isolation" and check.get("real_admission_smoke") is not True:
            errors.append(f"{name}: real admission smoke must accompany fixture isolation checks")
        if name == "android-signed-emulator-smoke" and check.get("android_apk_sha256") != document.get("android_apk_sha256"):
            errors.append(f"{name}: receipt must identify the same signed APK")
    if require_android:
        apk = root / "artifacts/android/david-pi-backup.apk"
        if not apk.is_file():
            errors.append("signed Android release artifact is absent")
        elif document.get("android_apk_sha256") != hashlib.sha256(apk.read_bytes()).hexdigest():
            errors.append("Android artifact differs from the tested APK")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--evidence", type=Path, default=ROOT / "docs/release-evidence/testing.json")
    parser.add_argument("--source-digest", action="store_true")
    args = parser.parse_args()
    if args.source_digest:
        print(source_digest(args.root))
        return 0
    try:
        errors = validate(json.loads(args.evidence.read_text()), args.root)
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print("Testing publication blocked:\n" + "\n".join("- " + error for error in errors))
        return 1
    print("Technical beta receipts match this source/APK. Physical-device and newcomer acceptance remain pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
