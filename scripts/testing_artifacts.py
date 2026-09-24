"""Content verification shared by the local testing-release builder and publisher."""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def sha(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_assets(directory, expected):
    require(directory.is_dir() and not directory.is_symlink(), "Unsafe release assets directory")
    require({p.name for p in directory.iterdir()} == set(expected), "Release asset inventory differs from the explicit allowlist")
    for name, digest in expected.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Release assets must be regular files without symbolic links")
        require(sha(path) == digest, "Release asset bytes differ from verified evidence")


def verify_source_archive(path, version, files):
    with tarfile.open(path, "r:gz") as archive:
        found = set()
        prefix = "david-pi-" + version + "/"
        for entry in archive:
            require(entry.isfile() and entry.name.startswith(prefix), "Unsafe source archive member")
            relative = entry.name[len(prefix):]
            require(relative in files and relative not in found, "Source archive inventory differs")
            require(hashlib.file_digest(archive.extractfile(entry), "sha256").hexdigest() == files[relative], "Source archive bytes differ")
            found.add(relative)
        require(found == set(files), "Source archive is incomplete")


def private_markers(path, root):
    # These values stay in memory. Receipts contain only counts, never the text.
    values = [str(root).encode(), str(Path.home()).encode()]
    if path:
        values += [line.strip().encode() for line in path.read_text().splitlines() if line.strip()]
    return list(set(values))


def screen_stream(stream, markers):
    overlap = max(map(len, markers), default=1) - 1
    tail = b""
    while block := stream.read(1024 * 1024):
        text = tail + block
        require(not any(marker in text for marker in markers), "Private marker found in candidate artifact; no marker value was logged")
        tail = text[-overlap:] if overlap else b""


def screen_tar(archive, markers):
    count = 0
    for entry in archive:
        screen_stream(io.BytesIO(" ".join((entry.name, entry.linkname, entry.uname, entry.gname)).encode()), markers)
        if entry.isfile():
            screen_stream(archive.extractfile(entry), markers)
            count += 1
    return count


def verify_oci(path, architecture, markers=()):
    """Verify every OCI blob, identify one platform, and screen every layer.

    No extraction or execution is used. Uncompressed layer hashes must match
    the runtime config, so the exported layers bind to the inspected image.
    """
    with tarfile.open(path, "r:*") as archive:
        members = {entry.name: entry for entry in archive}
        require(len(members) == len(archive.getmembers()), "Duplicate OCI archive entry")
        for name, entry in members.items():
            require(entry.isdir() or entry.isfile(), "Unsafe OCI archive entry")
            if name.startswith("blobs/sha256/") and entry.isfile():
                require(re.fullmatch(r"blobs/sha256/[a-f0-9]{64}", name), "Invalid OCI blob name")
                require(hashlib.file_digest(archive.extractfile(entry), "sha256").hexdigest() == name.rsplit("/", 1)[1], "OCI blob checksum mismatch")

        def blob(digest):
            require(isinstance(digest, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", digest), "Invalid OCI descriptor digest")
            name = "blobs/sha256/" + digest.split(":")[1]
            require(name in members and members[name].isfile(), "OCI descriptor blob is missing")
            return archive.extractfile(members[name])

        def document(digest):
            with blob(digest) as handle:
                payload = handle.read(4 * 1024 * 1024 + 1)
                require(len(payload) <= 4 * 1024 * 1024, "Oversized OCI metadata")
                screen_stream(io.BytesIO(payload), markers)
                return json.loads(payload)

        require("index.json" in members, "Docker export must use an OCI-capable image store")
        index = json.load(archive.extractfile("index.json"))
        candidates = []
        visited = set()

        def visit(descriptor):
            digest = descriptor.get("digest")
            require(digest not in visited, "Duplicate or cyclic OCI descriptor")
            visited.add(digest)
            value = document(digest)
            if "manifests" in value:
                for child in value["manifests"]:
                    visit(child)
            elif "config" in value:
                config = document(value["config"]["digest"])
                if config.get("architecture") == architecture and config.get("os") == "linux":
                    candidates.append((digest, value, config))

        for descriptor in index.get("manifests", []):
            visit(descriptor)
        require(len(candidates) == 1, "OCI archive must contain exactly one requested platform")
        digest, manifest, config = candidates[0]
        layers = manifest["layers"]
        require(len(layers) == len(config.get("rootfs", {}).get("diff_ids", [])), "OCI layer inventory differs from config")
        counts = []
        for layer, diff_id in zip(layers, config["rootfs"]["diff_ids"]):
            # tarfile's compression wrapper exposes the original uncompressed
            # tar bytes through its file object, including tar padding.
            import gzip
            with blob(layer["digest"]) as handle:
                media = layer.get("mediaType", "")
                require(media.endswith(".tar") or media.endswith(".tar+gzip") or media.endswith(".tar.gzip"), "Unsupported OCI layer compression")
                stream = gzip.GzipFile(fileobj=handle) if media.endswith(("+gzip", ".gzip")) else handle
                require("sha256:" + hashlib.file_digest(stream, "sha256").hexdigest() == diff_id, "OCI uncompressed layer hash differs")
            with blob(layer["digest"]) as handle, tarfile.open(fileobj=handle, mode="r|*") as contents:
                counts.append(screen_tar(contents, markers))
        return {"manifest_digest": digest, "config_digest": manifest["config"]["digest"],
                "layers": layers, "diff_ids": config["rootfs"]["diff_ids"],
                "architecture": architecture, "os": "linux", "regular_files_screened": sum(counts)}


def remote_platform(document, architecture, expected):
    """Require the same tested config and compressed layers after a Docker push."""
    require(document.get("config", {}).get("digest") == expected["config_digest"], "Remote image config differs from tested image")
    require(document.get("layers") == expected["layers"], "Remote image layers differ from tested image")
    require(expected["architecture"] == architecture, "Remote platform selection differs")


def remote_index(document, expected):
    require(re.fullmatch(r"sha256:[a-f0-9]{64}", str(document.get("digest", ""))), "Registry did not return a canonical index digest")
    manifests = document.get("manifests", [])
    found = {}
    for item in manifests:
        platform = item.get("platform", {})
        arch = platform.get("architecture")
        require(platform.get("os") == "linux" and arch in expected and arch not in found, "Published index has unexpected or duplicate platforms")
        found[arch] = item.get("digest")
    require(found == expected, "Published index differs from verified platform manifests")
    return document["digest"]
