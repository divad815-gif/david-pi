#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_update() {
  dp_require_root
  dp_load_state
  local archive='' expected='' image='' previous_image='' work candidate rc admin_user
  while (($#)); do
    case "$1" in
      --from) archive="${2:-}"; shift 2 ;;
      --sha256) expected="${2:-}"; shift 2 ;;
      --image) image="${2:-}"; shift 2 ;;
      *) dp_die "Usage: sudo david-pi update --from RELEASE.tgz --sha256 HASH --image GHCR_IMAGE@sha256:DIGEST" ;;
    esac
  done
  [[ -f "$archive" && "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || dp_die "A local release archive and SHA-256 are required"
  dp_validate_image_reference "$image" || dp_die "A pinned GHCR image reference is required"
  [[ "$(sha256sum "$archive" | awk '{print $1}')" == "${expected,,}" ]] || dp_die "Release checksum mismatch"
  work="$(mktemp -d /tmp/david-pi-update.XXXXXX)"
  trap 'rm -rf -- "$work"' RETURN
  tar -xzf "$archive" -C "$work" --no-same-owner
  candidate="$(find "$work" -maxdepth 3 -type f -name david-pi -printf '%h\n' | head -1)"
  [[ -n "$candidate" && -f "$candidate/VERSION" ]] || dp_die "Archive is not a David-Pi release"
  DAVID_PI_ALLOW_NON_PI=1 DAVID_PI_CLI_ROOT="$candidate" source "$candidate/installer/lib/preflight.sh"
  DAVID_PI_ALLOW_NON_PI=1 dp_preflight || dp_die "Updated release preflight failed"

  # Pull and verify availability before changing installed source or services.
  docker pull "$image"
  previous_image="${DAVID_PI_IMAGE:-$DP_IMAGE}"
  admin_user="${ADMIN_USER:-}"
  [[ -n "$admin_user" ]] || dp_die "Installed administrator identity is missing from state"

  install -d -m 0700 "$work/old-cli" "$work/old-compose"
  cp -a /usr/local/lib/david-pi/. "$work/old-cli/"
  cp -a "$DP_COMPOSE/." "$work/old-compose/"
  dp_backup_file /usr/local/lib/david-pi
  dp_backup_file "$DP_COMPOSE/compose.yaml"
  dp_backup_file "$DP_COMPOSE/Dockerfile"

  set +e
  (
    # This image override is deliberately scoped to candidate deployment.
    # shellcheck disable=SC2030
    export DAVID_PI_IMAGE="$image"
    rsync -a --delete --exclude .git "$candidate/" /usr/local/lib/david-pi/
    chmod 0755 /usr/local/lib/david-pi/david-pi
    ln -sfn /usr/local/lib/david-pi/david-pi /usr/local/bin/david-pi
    rsync -a --delete \
      --exclude .git --exclude installer --exclude clients --exclude tests \
      --exclude '__pycache__' --exclude '*.pyc' --exclude .env --exclude secrets \
      --exclude 'static/apk/*.apk' "$candidate/" "$DP_COMPOSE/"
    local env_tmp
    env_tmp="$(mktemp "$DP_COMPOSE/.env.XXXXXX")"
    awk -v value="$image" '/^DAVID_PI_IMAGE=/{print "DAVID_PI_IMAGE=" value; found=1; next} {print} END {if (!found) print "DAVID_PI_IMAGE=" value}' "$DP_COMPOSE/.env" > "$env_tmp"
    chown "$admin_user:$admin_user" "$env_tmp"; chmod 0600 "$env_tmp"; mv -f "$env_tmp" "$DP_COMPOSE/.env"
    dp_save_state DAVID_PI_IMAGE "$image"
    # Deliberately local to this candidate-deployment subshell.
    # shellcheck disable=SC2030
    export DAVID_PI_CLI_ROOT=/usr/local/lib/david-pi
    source /usr/local/lib/david-pi/installer/lib/setup.sh
    dp_install_services
    systemctl restart david-pi-portal.service
    source /usr/local/lib/david-pi/installer/lib/verify.sh
    dp_verify
  )
  rc=$?
  set -e
  if (( rc != 0 )); then
    dp_log "Update verification failed; restoring the previous application release"
    rsync -a --delete "$work/old-cli/" /usr/local/lib/david-pi/
    rsync -a --delete "$work/old-compose/" "$DP_COMPOSE/"
    dp_save_state DAVID_PI_IMAGE "$previous_image"
    chmod 0755 /usr/local/lib/david-pi/david-pi
    ln -sfn /usr/local/lib/david-pi/david-pi /usr/local/bin/david-pi
    # Reload helpers from the restored release, not the failed candidate.
    # shellcheck disable=SC2031
    export DAVID_PI_CLI_ROOT=/usr/local/lib/david-pi
    source /usr/local/lib/david-pi/installer/lib/setup.sh
    dp_install_services
    systemctl restart david-pi-portal.service
    dp_die "Update failed and the previous release was restored; production data was unchanged"
  fi
  dp_log "Updated David-Pi to $(tr -d '[:space:]' < /usr/local/lib/david-pi/VERSION)"
}

dp_backup() {
  dp_require_root
  systemctl start david-pi-backup.service
  if systemctl is-enabled -q david-pi-data-backup.timer 2>/dev/null; then
    systemctl start david-pi-data-backup.service
  else
    echo "Database/source backup completed. Independent data backup is not configured."
  fi
}

dp_restore_test() {
  dp_require_root
  local latest work failures=0
  latest="$(find "$DP_BACKUPS" -mindepth 1 -maxdepth 1 -type d -name '*-daily' -printf '%T@ %p\n' | sort -nr | head -1 | cut -d' ' -f2-)"
  [[ -n "$latest" ]] || dp_die "No daily backup set was found"
  work="$(mktemp -d /tmp/david-pi-restore-test.XXXXXX)"
  trap 'rm -rf -- "$work"' RETURN
  tar -tzf "$latest/source-with-secrets.tgz" >/dev/null || failures=$((failures + 1))
  while IFS= read -r -d '' db; do
    sqlite3 "file:$db?mode=ro" 'PRAGMA quick_check;' | grep -qx ok || failures=$((failures + 1))
  done < <(find "$latest/databases" -type f -name '*.db' -print0)
  local count
  count="$(find "$latest/databases" -type f -name '*.db' | wc -l)"
  echo "Restore test: archive readable; $count database copy/copies checked; $failures failure(s)."
  (( failures == 0 && count > 0 ))
}

dp_repair() {
  dp_require_root
  dp_load_state
  source "$DP_ROOT/installer/lib/setup.sh"
  dp_install_services
  systemctl restart david-pi-portal.service
  source "$DP_ROOT/installer/lib/verify.sh"
  dp_verify
}

dp_support_bundle() {
  dp_require_root
  local out="${1:-/tmp/david-pi-support-$(date -u +%Y%m%dT%H%M%SZ).tgz}" work
  work="$(mktemp -d /tmp/david-pi-support.XXXXXX)"
  trap 'rm -rf -- "$work"' RETURN
  {
    uname -a; cat /etc/os-release; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN
    findmnt /srv/data 2>/dev/null || true; df -hT / /srv/data 2>/dev/null || true
    docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}' 2>/dev/null || true
    systemctl --failed --no-pager 2>/dev/null || true
  } | dp_redact_stream > "$work/summary.txt"
  tailscale serve status 2>&1 | dp_redact_stream > "$work/tailscale-serve.txt" || true
  journalctl -u david-pi-portal.service -n 150 --no-pager 2>&1 | dp_redact_stream > "$work/portal-journal.txt" || true
  tar -czf "$out" -C "$work" .
  chmod 0600 "$out"
  echo "$out"
}

dp_uninstall_app() {
  dp_require_root
  local confirm
  read -r -p "Type REMOVE APP to stop and remove David-Pi application services while preserving all data: " confirm
  [[ "$confirm" == 'REMOVE APP' ]] || dp_die "Confirmation did not match; nothing changed"
  systemctl disable --now david-pi-portal.service david-pi-backup.timer david-pi-server-status.timer \
    david-pi-pihole-summary.timer david-pi-assistant-diagnostics.timer david-pi-safe-shutdown.path 2>/dev/null || true
  (cd "$DP_COMPOSE" && docker compose down) || true
  echo "Application services stopped. /srv/data, backups, Tailscale, SSH, and Pi-hole were preserved."
}
