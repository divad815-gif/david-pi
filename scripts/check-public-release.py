#!/usr/bin/env python3
"""Fail a release when private or generated material enters the public tree."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = re.compile(
    r"(^\.env$|keystore\.properties$|\.(jks|p12|pfx|pem|key|db|sqlite|sqlite3)$)",
    re.IGNORECASE,
)
FORBIDDEN_CONTENT = (
    "BEGIN OPENSSH" + " PRIVATE KEY",
    "BEGIN RSA" + " PRIVATE KEY",
    "BEGIN EC" + " PRIVATE KEY",
)
SKIP_PARTS = {".git", ".gradle", "build", "__pycache__", ".pytest_cache"}


def private_content(text: str) -> str:
    """Include constant Python string joins without evaluating source code."""
    def joined_string(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = joined_string(node.left), joined_string(node.right)
            if left is not None and right is not None:
                return left + right
        return None

    candidates = [text]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return text.casefold()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp):
            value = joined_string(node)
            if value is not None:
                candidates.append(value)
    return "\n".join(candidates).casefold()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-markers", type=Path,
        help="Optional local UTF-8 file of private text to exclude, one value per line; keep it outside public source.",
    )
    args = parser.parse_args(argv)
    failures: list[str] = []
    raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
    names = sorted(set(raw.decode().split("\0")) - {""})
    private_markers: list[str] = []
    if args.private_markers is not None:
        try:
            if args.private_markers.resolve() in {(ROOT / name).resolve() for name in names}:
                raise ValueError("private marker file is included in public source")
            private_markers = [line.strip().casefold() for line in args.private_markers.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not private_markers:
                raise ValueError("private marker file has no entries")
        except (OSError, UnicodeError, ValueError):
            print("Public release check failed: private marker file must be readable, nonempty, and excluded from public source.", file=sys.stderr)
            return 1
    for name in names:
        relative = Path(name)
        path = ROOT / relative
        if not path.is_file():
            continue
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
                failures.append(f"credential marker in {relative}")
        if private_markers:
            searchable = private_content(text)
            if any(marker in searchable for marker in private_markers):
                failures.append(f"private marker in {relative}")
    if failures:
        print("Public release check failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in failures), file=sys.stderr)
        return 1
    print("Public release check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
