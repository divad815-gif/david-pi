#!/usr/bin/env python3
"""Shared, content-neutral helpers for release evidence and promotion gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
DOMAIN_COMPONENT = r"(?:[a-z0-9]|[a-z0-9][a-z0-9-]*[a-z0-9])"
REGISTRY = rf"{DOMAIN_COMPONENT}(?:\.{DOMAIN_COMPONENT})*(?::[0-9]{{1,5}})?"
NAME_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*"
REPOSITORY_NAME = re.compile(
    rf"^(?:{REGISTRY}/)?{NAME_COMPONENT}(?:/{NAME_COMPONENT})*$"
)


class ContractError(ValueError):
    """A release artifact does not satisfy the promotion contract."""


def image_digest(reference: str) -> str:
    """Return the sha256 digest from an immutable OCI/Docker reference."""
    if not isinstance(reference, str) or reference != reference.strip():
        raise ContractError("image reference must be a non-empty string without surrounding whitespace")
    if any(character.isspace() for character in reference):
        raise ContractError("image reference must not contain whitespace")
    if reference.count("@sha256:") != 1:
        raise ContractError("image reference must use exactly one @sha256 digest")
    name, digest = reference.rsplit("@sha256:", 1)
    if not name or len(name) > 255 or not REPOSITORY_NAME.fullmatch(name):
        raise ContractError(
            "image repository must use a lowercase registry/path name without a mutable tag"
        )
    if not SHA256_HEX.fullmatch(digest):
        raise ContractError("image reference must end in @sha256 followed by 64 lowercase hex characters")
    return f"sha256:{digest}"


def image_repository(reference: str) -> str:
    """Validate an immutable reference and return its repository portion."""
    image_digest(reference)
    return reference.split("@", 1)[0]


def is_digest_reference(reference: Any) -> bool:
    try:
        image_digest(reference)
    except (ContractError, TypeError):
        return False
    return True


def is_sha256_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", value))


def is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_HEX.fullmatch(value))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path, description: str = "JSON evidence") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"{description} is unavailable or invalid: {error}") from error
    if not isinstance(value, dict):
        raise ContractError(f"{description} must be a JSON object")
    return value


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def relative_evidence_path(path: Path, parent: Path) -> str:
    try:
        relative = path.resolve().relative_to(parent.resolve())
    except ValueError as error:
        raise ContractError("promotion evidence must live beside or below its manifest") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ContractError("promotion evidence path is invalid")
    return relative.as_posix()


def resolve_evidence_path(parent: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ContractError("evidence path is missing")
    candidate = Path(relative)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError("evidence path must be a safe relative path")
    resolved_parent = parent.resolve()
    resolved = (parent / candidate).resolve()
    try:
        resolved.relative_to(resolved_parent)
    except ValueError as error:
        raise ContractError("evidence path escapes the evidence directory") from error
    return resolved
