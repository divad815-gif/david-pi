#!/usr/bin/env bash
set -Eeuo pipefail

# The release workflow replaces this token in the published installer asset.
REPOSITORY="${DAVID_PI_REPOSITORY:-__GITHUB_REPOSITORY__}"
RELEASE_VERSION="${DAVID_PI_VERSION:-latest}"
TEST_MODE="${DAVID_PI_BOOTSTRAP_TEST_MODE:-0}"
WORK=''

say() { printf '%s\n' "$*"; }
fail() { printf 'David-Pi bootstrap: %s\n' "$*" >&2; exit 1; }
cleanup() { [[ -z "$WORK" ]] || rm -rf -- "$WORK"; }
trap cleanup EXIT INT TERM HUP

[[ "$REPOSITORY" != *'__'* ]] || fail "this source template is not a rendered release asset; set DAVID_PI_REPOSITORY=OWNER/david-pi or download install.sh from a release"
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/david-pi$ ]] || fail "invalid GitHub repository name"

if [[ "$TEST_MODE" != 1 ]]; then
  [[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run this installer with sudo"
  [[ "$(uname -s)" == Linux ]] || fail "native Linux is required"
  [[ -d /run/systemd/system && "$(ps -p 1 -o comm=)" == systemd ]] || fail "systemd must be PID 1; Windows and WSL are not supported"
  if grep -qiE '(microsoft|wsl)' /proc/sys/kernel/osrelease /proc/version 2>/dev/null; then
    fail "WSL is not a supported David-Pi host"
  fi
  [[ -r /etc/os-release ]] || fail "cannot identify this operating system"
  # shellcheck disable=SC1091
  source /etc/os-release
  case "${ID:-}:${VERSION_ID:-}" in
    debian:13|raspbian:13|ubuntu:24.04) ;;
    *) fail "supported hosts are Debian 13 or Ubuntu 24.04 AMD64 and Raspberry Pi OS Debian 13 ARM64" ;;
  esac
  case "$(uname -m)" in x86_64|aarch64) ;; *) fail "only amd64 and arm64 are supported" ;; esac
  memory_kib="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
  (( memory_kib >= 3670016 )) || fail "at least 3.5 GiB RAM is required"
  free_kib="$(df -Pk / | awk 'NR==2{print $4}')"
  (( free_kib >= 8388608 )) || fail "at least 8 GiB free space is required on the OS filesystem"
  for command in curl sha256sum tar awk sed find python3; do
    command -v "$command" >/dev/null || fail "required command is missing: $command"
  done
fi

if [[ -n "${DAVID_PI_DOWNLOAD_BASE:-}" ]]; then
  DOWNLOAD_BASE="${DAVID_PI_DOWNLOAD_BASE%/}"
  [[ "$DOWNLOAD_BASE" == https://* || "$TEST_MODE" == 1 ]] || fail "custom download base must use HTTPS"
elif [[ "$RELEASE_VERSION" == latest ]]; then
  DOWNLOAD_BASE="https://github.com/$REPOSITORY/releases/latest/download"
elif [[ "$RELEASE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][A-Za-z0-9_.-]+)?$ ]]; then
  DOWNLOAD_BASE="https://github.com/$REPOSITORY/releases/download/v$RELEASE_VERSION"
else
  fail "invalid DAVID_PI_VERSION"
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/david-pi-bootstrap.XXXXXX")"
chmod 0700 "$WORK"
CURL=(curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 300)
if [[ "$TEST_MODE" == 1 ]]; then CURL=(curl --fail --location --silent --show-error --connect-timeout 5 --max-time 30); fi

say "Downloading release metadata from GitHub..."
"${CURL[@]}" "$DOWNLOAD_BASE/release-manifest.txt" -o "$WORK/release-manifest.txt"
(( $(wc -c < "$WORK/release-manifest.txt") <= 4096 )) || fail "release manifest is unexpectedly large"

manifest_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$WORK/release-manifest.txt" | head -n1
}

VERSION="$(manifest_value VERSION)"
ARCHIVE="$(manifest_value ARCHIVE)"
ARCHIVE_SHA256="$(manifest_value ARCHIVE_SHA256)"
IMAGE="$(manifest_value IMAGE)"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.][A-Za-z0-9_.-]+)?$ ]] || fail "manifest has an invalid version"
[[ "$ARCHIVE" == "david-pi-$VERSION.tar.gz" ]] || fail "manifest has an invalid archive name"
[[ "$ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] || fail "manifest has an invalid archive checksum"
[[ "$IMAGE" =~ ^ghcr\.io/[a-z0-9_.-]+/david-pi@sha256:[0-9a-f]{64}$ ]] || fail "manifest image is not an immutable GHCR digest"
if [[ "$RELEASE_VERSION" != latest ]]; then [[ "$VERSION" == "$RELEASE_VERSION" ]] || fail "manifest version mismatch"; fi

say "Downloading David-Pi $VERSION..."
"${CURL[@]}" "$DOWNLOAD_BASE/$ARCHIVE" -o "$WORK/$ARCHIVE"
actual="$(sha256sum "$WORK/$ARCHIVE" | awk '{print $1}')"
[[ "$actual" == "$ARCHIVE_SHA256" ]] || fail "release archive checksum mismatch; nothing was extracted or run"

# Reject path traversal before extraction. The release archive intentionally
# contains no device files or absolute paths.
while IFS= read -r member; do
  [[ "$member" != /* && "$member" != ../* && "$member" != */../* && "$member" != *'/..' ]] ||
    fail "unsafe path in release archive"
done < <(tar -tzf "$WORK/$ARCHIVE")

mkdir -m 0700 "$WORK/extracted"
python3 - "$WORK/$ARCHIVE" "$WORK/extracted" <<'PYARCHIVE'
import pathlib,sys,tarfile
with tarfile.open(sys.argv[1], 'r:gz') as bundle:
    members=bundle.getmembers()
    seen=set()
    if len(members)>100000 or sum(m.size for m in members)>4*1024**3:
        raise SystemExit('Release archive is too large')
    for member in members:
        path=pathlib.PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()) or member.name in seen:
            raise SystemExit('Unsafe release archive member')
        seen.add(member.name)
    bundle.extractall(sys.argv[2],members=members,filter='data')
PYARCHIVE
ROOT="$WORK/extracted/david-pi-$VERSION"
[[ -x "$ROOT/david-pi" && "$(tr -d '[:space:]' < "$ROOT/VERSION")" == "$VERSION" ]] || fail "release contents are incomplete"

say "Release verified: $VERSION"
say "Container image pinned to: $IMAGE"
if [[ "${DAVID_PI_BOOTSTRAP_VERIFY_ONLY:-0}" == 1 ]]; then
  say "Verification-only mode complete; setup was not started."
  exit 0
fi

# Keep a verified copy of the installer available if guided setup deliberately
# stops on a blank disk.  The user must be able to run `david-pi
# prepare-storage` and then resume without downloading an unverified or
# short-lived copy of the CLI.  The completed application install replaces
# this symlink with /usr/local/lib/david-pi/david-pi.
if [[ "$TEST_MODE" == 1 ]]; then
  BOOTSTRAP_ROOT="${DAVID_PI_BOOTSTRAP_INSTALL_ROOT:-$WORK/persistent-bootstrap}"
  CLI_LINK="${DAVID_PI_BOOTSTRAP_CLI_LINK:-$WORK/bin/david-pi}"
else
  BOOTSTRAP_ROOT="/usr/local/lib/david-pi-bootstrap-$VERSION"
  CLI_LINK="/usr/local/bin/david-pi"
fi
BOOTSTRAP_STAGE="${BOOTSTRAP_ROOT}.new.$$"
rm -rf -- "$BOOTSTRAP_STAGE"
mkdir -p -- "$(dirname "$BOOTSTRAP_ROOT")" "$(dirname "$CLI_LINK")"
cp -a -- "$ROOT" "$BOOTSTRAP_STAGE"
rm -rf -- "$BOOTSTRAP_ROOT"
mv -- "$BOOTSTRAP_STAGE" "$BOOTSTRAP_ROOT"
chmod 0755 "$BOOTSTRAP_ROOT/david-pi"
ln -sfn -- "$BOOTSTRAP_ROOT/david-pi" "$CLI_LINK"

export DAVID_PI_IMAGE_OVERRIDE="$IMAGE"
export DAVID_PI_REPOSITORY="$REPOSITORY"
"$BOOTSTRAP_ROOT/david-pi" setup

# On success dp_install_application has installed the final CLI and repointed
# the link.  Remove only this versioned bootstrap copy; failed or interrupted
# setup exits before this point so its recovery CLI remains available.
if [[ "$(readlink -f "$CLI_LINK")" != "$BOOTSTRAP_ROOT/david-pi" ]]; then
  rm -rf -- "$BOOTSTRAP_ROOT"
fi
