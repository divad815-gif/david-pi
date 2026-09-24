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
[[ "$VERSION" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-beta\.[1-9][0-9]*)?$ ]] || { echo "Invalid stable or testing release version" >&2; exit 1; }
[[ -f "$OUT/$ARCHIVE" ]] || { echo "Missing release archive" >&2; exit 1; }

archive_sha="$(sha256sum "$OUT/$ARCHIVE" | awk '{print $1}')"
installer_version=latest
if [[ "$VERSION" == *-beta.* ]]; then
  grep -q '__RELEASE_VERSION__' "$ROOT/install.sh" || { echo "Bootstrap cannot pin a testing release" >&2; exit 1; }
  installer_version="$VERSION"
fi
sed -e "s#__GITHUB_REPOSITORY__#$REPOSITORY#g" -e "s#__RELEASE_VERSION__#$installer_version#g" "$ROOT/install.sh" > "$OUT/install.sh"
chmod 0755 "$OUT/install.sh"
(cd "$OUT" && sha256sum install.sh > install.sh.sha256)
cat > "$OUT/release-manifest.txt" <<EOF
VERSION=$VERSION
ARCHIVE=$ARCHIVE
ARCHIVE_SHA256=$archive_sha
IMAGE=ghcr.io/${REPOSITORY,,}@$IMAGE_DIGEST
DATA_SCHEMA_VERSION=1
ROLLBACK_MIN_DATA_SCHEMA=1
EOF
chmod 0644 "$OUT/release-manifest.txt"
printf '%s\n' "$OUT/release-manifest.txt"
