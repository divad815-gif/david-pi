#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
OUT="${1:-$ROOT/dist}"
python3 "$ROOT/scripts/check-public-release.py"
python3 "$ROOT/scripts/package_source.py" --output "$OUT/david-pi-$VERSION.tar.gz"
(cd "$OUT" && sha256sum "david-pi-$VERSION.tar.gz" > "david-pi-$VERSION.tar.gz.sha256")
