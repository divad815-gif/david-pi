#!/usr/bin/env python3
"""Fail when tracked source contains a likely credential rather than a placeholder."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "", ".conf", ".css", ".html", ".hujson", ".java", ".js", ".json",
    ".kts", ".md", ".mjs", ".properties", ".py", ".service", ".sh",
    ".timer", ".txt", ".xml", ".yaml", ".yml",
}
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(\.env(?:\..*)?|secrets?(?:/|$)|keystore\.properties$)|"
    r"\.(?:db|db-wal|db-shm|jks|key|p12|pem|pfx)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)


def source_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    failures: list[str] = []
    files = source_files()
    for relative in files:
        if FORBIDDEN_NAMES.search(relative) and relative != ".env.example":
            failures.append(f"forbidden tracked path: {relative}")
            continue
        path = ROOT / relative
        if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            failures.append(f"credential-like content: {relative}")
    if failures:
        print("Tracked-source secret scan failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Source secret scan passed ({len(files)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
