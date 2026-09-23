#!/usr/bin/env python3
"""Send a content-free Healthchecks.io ping from a protected URL file."""

from __future__ import annotations

import os
import re
import stat
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


STATES = frozenset({"start", "success", "failure"})
PING_HOSTS = frozenset({"hc-ping.com"})
PING_PATH = re.compile(r"/[A-Za-z0-9_-]{20,128}/?\Z")


def load_ping_url(path: Path, expected_uid: int = 0) -> str:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("ping configuration is not a regular file")
        if (
            metadata.st_uid != expected_uid
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            raise PermissionError("ping configuration ownership or mode is unsafe")
        contents = os.read(descriptor, 4097)
        if len(contents) > 4096 or os.read(descriptor, 1):
            raise ValueError("ping configuration exceeds its size limit")
        try:
            lines = contents.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ValueError("ping configuration is not UTF-8") from error
    finally:
        os.close(descriptor)
    if len(lines) != 1 or not lines[0].strip():
        raise ValueError("ping configuration must contain exactly one URL")
    value = lines[0].strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PING_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not PING_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("ping configuration URL is not an approved endpoint")
    return value


def state_url(base_url: str, state: str) -> str:
    if state not in STATES:
        raise ValueError("unsupported healthcheck state")
    parsed = urlsplit(base_url)
    suffix = {"start": "/start", "success": "", "failure": "/fail"}[state]
    path = parsed.path.rstrip("/") + suffix
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def send_ping(base_url: str, state: str, opener=urllib.request.urlopen) -> None:
    request = urllib.request.Request(
        state_url(base_url, state),
        data=b"",
        method="POST",
        headers={"User-Agent": "david-pi-healthcheck/1"},
    )
    with opener(request, timeout=10) as response:
        status = int(getattr(response, "status", 0))
        if status < 200 or status >= 300:
            raise RuntimeError("healthcheck endpoint returned a non-success status")


def configured_url_path(environment=os.environ) -> Path:
    configured = environment.get("DAVID_PI_HEALTHCHECK_URL_FILE", "")
    if not configured:
        raise ValueError("healthcheck URL file is not configured")
    path = Path(configured)
    if path.is_absolute():
        return path
    if path.name != configured or configured in {".", ".."}:
        raise ValueError("credential name is invalid")
    credential_directory = environment.get("CREDENTIALS_DIRECTORY", "")
    if not credential_directory:
        raise ValueError("credential directory is unavailable")
    directory = Path(credential_directory)
    if not directory.is_absolute():
        raise ValueError("credential directory is invalid")
    return directory / path


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in STATES:
        print("usage: david-pi-healthcheck-ping {start|success|failure}", file=sys.stderr)
        return 64
    try:
        base_url = load_ping_url(configured_url_path())
    except (OSError, ValueError):
        print("healthcheck ping configuration is unavailable", file=sys.stderr)
        return 78
    try:
        send_ping(base_url, argv[1])
    except Exception as error:  # Deliberately omit exception text; it can contain the secret URL.
        print(
            f"healthcheck ping failed state={argv[1]} type={type(error).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
