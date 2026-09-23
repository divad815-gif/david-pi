"""Bounded byte-range and HLS helpers for private MyTube playback."""
from __future__ import annotations

import re
from pathlib import Path

from flask import Response, jsonify, request


RANGE_PATTERN = re.compile(r"^bytes=(\d*)-(\d*)$")
SAFE_HLS_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,159}$")
CHUNK_BYTES = 1024 * 1024
MAX_PLAYLIST_BYTES = 1024 * 1024


def safe_regular_file(root: Path, relative_name: str) -> Path:
    """Resolve one stored object without accepting traversal or symlinks."""
    name = str(relative_name or "")
    if not name or Path(name).name != name or name in {".", ".."} or "\x00" in name:
        raise FileNotFoundError("invalid stored object")
    candidate = root / name
    if candidate.is_symlink() or not candidate.is_file():
        raise FileNotFoundError("stored object unavailable")
    resolved = candidate.resolve()
    if root.resolve() not in resolved.parents:
        raise FileNotFoundError("stored object outside library")
    return resolved


def _parsed_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = RANGE_PATTERN.fullmatch(value.strip())
    if not match or "," in value:
        raise ValueError("range_invalid")
    first, last = match.groups()
    if not first and not last:
        raise ValueError("range_invalid")
    if first:
        start = int(first)
        end = int(last) if last else size - 1
        if start >= size or end < start:
            raise ValueError("range_unsatisfiable")
        return start, min(end, size - 1)
    suffix = int(last)
    if suffix <= 0:
        raise ValueError("range_unsatisfiable")
    return max(0, size - suffix), size - 1


def ranged_response(path: Path, mimetype: str, *, etag: str | None = None):
    """Return a single-range response without loading the video into memory."""
    size = path.stat().st_size
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Vary": "Cookie",
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
    if request.method == "HEAD":
        headers["Content-Length"] = str(size)
        return Response(status=200, headers=headers, mimetype=mimetype)
    try:
        selected = _parsed_range(request.headers.get("Range"), size)
    except ValueError:
        headers["Content-Range"] = f"bytes */{size}"
        return Response(status=416, headers=headers)

    start, end = selected or (0, size - 1)
    length = max(0, end - start + 1)

    def generate():
        remaining = length
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                block = source.read(min(CHUNK_BYTES, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    status = 206 if selected else 200
    headers["Content-Length"] = str(length)
    if selected:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return Response(generate(), status=status, headers=headers, mimetype=mimetype)


def hls_file(generation_root: Path, name: str, *, playlist: bool = False):
    """Serve a validated immutable HLS generation member."""
    if not SAFE_HLS_NAME.fullmatch(str(name or "")):
        return jsonify(error="Playback file not found."), 404
    try:
        path = safe_regular_file(generation_root, name)
    except FileNotFoundError:
        return jsonify(error="Playback file not found."), 404
    if playlist:
        if path.stat().st_size > MAX_PLAYLIST_BYTES:
            return jsonify(error="Playback file not found."), 404
        response = Response(path.read_bytes(), mimetype="application/vnd.apple.mpegurl")
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie"
        return response
    response = ranged_response(path, "video/iso.segment")
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response
