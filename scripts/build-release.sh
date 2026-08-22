#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
OUT="${1:-$ROOT/dist}"
STAGE="$(mktemp -d)"
trap 'rm -rf -- "$STAGE"' EXIT

python3 "$ROOT/scripts/check-public-release.py"
mkdir -p "$OUT" "$STAGE/david-pi-$VERSION"
rsync -a \
  --exclude .git --exclude dist --exclude .env --exclude '*.db' --exclude '*.sqlite*' \
  --exclude '*.jks' --exclude keystore.properties --exclude local.properties \
  --exclude .gradle --exclude build --exclude __pycache__ --exclude .pytest_cache \
  "$ROOT/" "$STAGE/david-pi-$VERSION/"
tar --sort=name --mtime='UTC 2020-01-01' --owner=0 --group=0 --numeric-owner \
  -czf "$OUT/david-pi-$VERSION.tar.gz" -C "$STAGE" "david-pi-$VERSION"
(cd "$OUT" && sha256sum "david-pi-$VERSION.tar.gz" > "david-pi-$VERSION.tar.gz.sha256")
echo "$OUT/david-pi-$VERSION.tar.gz"

