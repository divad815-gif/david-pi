#!/usr/bin/env python3
"""Fail a release when private or generated material enters the public tree."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = re.compile(
    r"(^\.env$|keystore\.properties$|\.(jks|p12|pfx|pem|key|db|sqlite|sqlite3)$)",
    re.IGNORECASE,
)
FORBIDDEN_CONTENT = (
    "tail" + "47b786",
    "100.101" + ".50.45",
    "192.168" + ".1.2",
    "David" + " Shepherd",
    "Diana" + " Hernandez",
    "BEGIN OPENSSH" + " PRIVATE KEY",
    "BEGIN RSA" + " PRIVATE KEY",
    "BEGIN EC" + " PRIVATE KEY",
)
SKIP_PARTS = {".git", ".gradle", "build", "__pycache__", ".pytest_cache"}


def main() -> int:
    failures: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        relative = path.relative_to(ROOT)
        if FORBIDDEN_NAMES.search(path.name) and not path.name.endswith(".example"):
            failures.append(f"forbidden filename: {relative}")
        if path.stat().st_size > 25 * 1024 * 1024:
            failures.append(f"unexpected large file: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for marker in FORBIDDEN_CONTENT:
            if marker.lower() in text.lower():
                failures.append(f"private marker in {relative}: {marker}")
    if failures:
        print("Public release check failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in failures), file=sys.stderr)
        return 1
    print("Public release check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
