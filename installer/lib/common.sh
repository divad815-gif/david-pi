#!/usr/bin/env bash
set -Eeuo pipefail

DP_ROOT="${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}"
DP_VERSION="$(tr -d '[:space:]' < "$DP_ROOT/VERSION")"
DP_IMAGE="david-pi:$DP_VERSION"
DP_ETC="${DAVID_PI_ETC:-/etc/david-pi}"
DP_STATE="$DP_ETC/install-state.env"
DP_COMPOSE="/srv/compose/photo-portal"
DP_DATA="/srv/data/family-photos"
DP_BACKUPS="/srv/backups/photo-portal"
DP_INDEPENDENT="/srv/backup-data"
DP_SENTINEL_VALUE="david-pi-family-storage-v1"
DP_LOG="/var/log/david-pi-installer.log"

dp_require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Run this command with sudo." >&2
    exit 1
  fi
}

dp_log() {
  local message="$*"
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$message" | tee -a "$DP_LOG"
}

dp_die() {
  dp_log "ERROR: $*"
  exit 1
}

dp_prompt() {
  local variable="$1" prompt="$2" default="${3:-}" value
  if [[ -n "${!variable:-}" ]]; then return 0; fi
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " value
    printf -v "$variable" '%s' "${value:-$default}"
  else
    read -r -p "$prompt: " value
    printf -v "$variable" '%s' "$value"
  fi
}

dp_yes_no() {
  local variable="$1" prompt="$2" default="${3:-no}" value suffix='[y/N]'
  [[ "$default" == yes ]] && suffix='[Y/n]'
  if [[ -n "${!variable:-}" ]]; then return 0; fi
  read -r -p "$prompt $suffix: " value
  value="${value,,}"
  if [[ -z "$value" ]]; then value="$default"; fi
  case "$value" in y|yes) printf -v "$variable" yes ;; *) printf -v "$variable" no ;; esac
}

dp_secret_prompt() {
  local variable="$1" prompt="$2" value
  if [[ -n "${!variable:-}" ]]; then return 0; fi
  read -r -s -p "$prompt: " value
  echo
  printf -v "$variable" '%s' "$value"
}

dp_save_state() {
  local key="$1" value="$2"
  install -d -m 0700 "$DP_ETC"
  touch "$DP_STATE"
  chmod 0600 "$DP_STATE"
  local tmp
  tmp="$(mktemp "$DP_ETC/.state.XXXXXX")"
  grep -v "^${key}=" "$DP_STATE" > "$tmp" || true
  printf '%s=%q\n' "$key" "$value" >> "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$DP_STATE"
}

dp_load_state() {
  if [[ -f "$DP_STATE" ]]; then
    # State contains installer choices only, never user-supplied secrets.
    # shellcheck disable=SC1090
    source "$DP_STATE"
  fi
}

dp_mark_phase() {
  dp_save_state "PHASE_$1" complete
}

dp_phase_complete() {
  local key="PHASE_$1"
  [[ "${!key:-}" == complete ]]
}

dp_run_timeout() {
  local seconds="$1"; shift
  timeout --foreground "$seconds" "$@"
}

dp_backup_file() {
  local path="$1" stamp backup_root
  [[ -e "$path" ]] || return 0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_root="$DP_ETC/rollback/$stamp"
  install -d -m 0700 "$backup_root"
  cp -a --parents "$path" "$backup_root/"
  dp_log "Preserved $path under $backup_root"
}

dp_atomic_write() {
  local target="$1" mode="$2" owner="$3" group="$4" tmp
  install -d -m 0755 "$(dirname "$target")"
  tmp="$(mktemp "$(dirname "$target")/.david-pi.XXXXXX")"
  cat > "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner:$group" "$tmp"
  mv -f "$tmp" "$target"
}

dp_physical_parent() {
  local source="$1"
  lsblk -ndo PKNAME "$source" 2>/dev/null | head -n1
}

dp_redact_stream() {
  sed -E \
    -e 's/((TOKEN|PASSWORD|SECRET|KEY|AUTHORIZATION)[A-Za-z0-9_ -]*[=:])[[:space:]]*[^[:space:]]+/\1[REDACTED]/Ig' \
    -e 's#https?://[^/@[:space:]]+:[^/@[:space:]]+@#https://[REDACTED]@#g'
}

dp_validate_image_reference() {
  local image="$1"
  [[ "$image" =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+@sha256:[0-9a-f]{64}$ ]] ||
    [[ "$image" =~ ^david-pi:[0-9]+\.[0-9]+\.[0-9]+([-.][a-zA-Z0-9_.-]+)?$ ]]
}

dp_select_image() {
  DAVID_PI_IMAGE="${DAVID_PI_IMAGE_OVERRIDE:-${DAVID_PI_IMAGE:-$DP_IMAGE}}"
  dp_validate_image_reference "$DAVID_PI_IMAGE" || dp_die "Invalid or unpinned David-Pi image reference"
  export DAVID_PI_IMAGE
}
