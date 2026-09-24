#!/usr/bin/env python3
"""Generate a deterministic, content-neutral CycloneDX inventory for a built image."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
PINNED_REQUIREMENT = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^]]+\])?==([^\s;]+)"
)


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", str(value).strip()).lower()


def direct_requirements(path: Path) -> set[tuple[str, str]]:
    direct: set[tuple[str, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PINNED_REQUIREMENT.match(line)
        if match:
            direct.add((normalize_name(match.group(1)), match.group(2)))
    return direct


def python_components(path: Path, direct: set[tuple[str, str]]) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Python package inventory must be a list")
    components = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("name") or not item.get("version"):
            raise ValueError("Python package inventory contains an invalid entry")
        name = normalize_name(item["name"])
        version = str(item["version"])
        component = {
            "type": "library",
            "bom-ref": f"pkg:pypi/{quote(name)}@{quote(version)}",
            "name": name,
            "version": version,
            "purl": f"pkg:pypi/{quote(name)}@{quote(version)}",
        }
        if (name, version) in direct:
            component["properties"] = [{"name": "david-pi:dependency-scope", "value": "direct"}]
        components.append(component)
    return components


def os_components(path: Path, package_type: str) -> list[dict]:
    namespaces = {
        "apk": "pkg:apk/alpine",
        "deb": "pkg:deb/debian",
    }
    try:
        namespace = namespaces[package_type]
    except KeyError as error:
        raise ValueError(f"unsupported OS package type: {package_type}") from error
    components = []
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not all(fields):
            raise ValueError("OS package inventory contains an invalid entry")
        name, version, architecture = fields
        purl = f"{namespace}/{quote(name)}@{quote(version)}?arch={quote(architecture)}"
        components.append({
            "type": "library", "bom-ref": purl, "name": name,
            "version": version, "purl": purl,
            "properties": [{"name": "david-pi:package-architecture", "value": architecture}],
        })
    return components


def debian_components(path: Path) -> list[dict]:
    """Compatibility helper for the established unit-level SBOM contract."""
    return os_components(path, "deb")


def build_document(components: list[dict], image: str, revision: str) -> dict:
    ordered = sorted(components, key=lambda item: item["bom-ref"])
    canonical = json.dumps(ordered, separators=(",", ":"), sort_keys=True).encode("utf-8")
    inventory_digest = hashlib.sha256(canonical).hexdigest()
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"david-pi:{revision}:{inventory_digest}")
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.5.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": f"david-pi:{revision}",
                "name": "david-pi",
                "version": revision,
                "properties": [
                    {"name": "david-pi:image-reference", "value": image},
                    {"name": "david-pi:inventory-sha256", "value": inventory_digest},
                ],
            }
        },
        "components": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-packages", type=Path, required=True)
    parser.add_argument("--os-packages", type=Path, required=True)
    parser.add_argument("--os-package-type", choices=("apk", "deb"), default="deb")
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements.txt")
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    direct = direct_requirements(args.requirements)
    components = python_components(args.python_packages, direct)
    components.extend(os_components(args.os_packages, args.os_package_type))
    document = build_document(components, args.image, args.revision)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
