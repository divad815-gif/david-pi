#!/usr/bin/env python3
"""Create or verify the tracked Android APK release attestation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.android_release import (  # noqa: E402
    AndroidReleaseError,
    android_source_inventory,
    build_attestation,
    verify_java_runtime,
    verified_android_release,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("source-hash", "verify-java", "create", "verify")
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "source-hash":
            print(android_source_inventory(root)["tree_sha256"])
        elif args.command == "verify-java":
            verified = verify_java_runtime(root)
            print(
                "verified Java runtime "
                f"files:{verified['runtime_file_count']} "
                f"sha256:{verified['runtime_tree_sha256']}"
            )
        elif args.command == "create":
            if args.output is None:
                parser.error("create requires --output")
            document = build_attestation(root)
            output = args.output if args.output.is_absolute() else root / args.output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(output)
        else:
            verified = verified_android_release(
                root, require_source=True, require_tools=True
            )
            print(
                "verified "
                f"{verified['application_id']} "
                f"v{verified['version_code']} ({verified['version_name']}) "
                f"sha256:{verified['artifact']['sha256']}"
            )
    except AndroidReleaseError as error:
        print(f"Android release verification failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
