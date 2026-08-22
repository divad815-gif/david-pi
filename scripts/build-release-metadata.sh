#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
OUT="${1:-$ROOT/dist}"
REPOSITORY="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
IMAGE_DIGEST="${DAVID_PI_IMAGE_DIGEST:?DAVID_PI_IMAGE_DIGEST is required}"
VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION")"
ARCHIVE="david-pi-$VERSION.tar.gz"

[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/david-pi$ ]] || { echo "Invalid GITHUB_REPOSITORY" >&2; exit 1; }
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || { echo "Invalid DAVID_PI_IMAGE_DIGEST" >&2; exit 1; }
[[ -f "$OUT/$ARCHIVE" ]] || { echo "Missing release archive" >&2; exit 1; }

archive_sha="$(sha256sum "$OUT/$ARCHIVE" | awk '{print $1}')"
sed "s#__GITHUB_REPOSITORY__#$REPOSITORY#g" "$ROOT/install.sh" > "$OUT/install.sh"
chmod 0755 "$OUT/install.sh"
sha256sum "$OUT/install.sh" > "$OUT/install.sh.sha256"
cat > "$OUT/release-manifest.txt" <<EOF
VERSION=$VERSION
ARCHIVE=$ARCHIVE
ARCHIVE_SHA256=$archive_sha
IMAGE=ghcr.io/${REPOSITORY,,}@$IMAGE_DIGEST
EOF
chmod 0644 "$OUT/release-manifest.txt"
printf '%s\n' "$OUT/release-manifest.txt"
