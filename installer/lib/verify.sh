#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_check() {
  local description="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf 'PASS  %s\n' "$description"
  else
    printf 'FAIL  %s\n' "$description"
    DP_VERIFY_FAILURES=$((DP_VERIFY_FAILURES + 1))
  fi
}

dp_check_output() {
  local description="$1" pattern="$2"; shift 2
  if "$@" 2>/dev/null | grep -Eq "$pattern"; then
    printf 'PASS  %s\n' "$description"
  else
    printf 'FAIL  %s\n' "$description"
    DP_VERIFY_FAILURES=$((DP_VERIFY_FAILURES + 1))
  fi
}

dp_status() {
  dp_load_state
  local source='unmounted' free='unknown' portal='missing' tail='disconnected' backup='not configured'
  mountpoint -q /srv/data && source="$(findmnt -n -o SOURCE,FSTYPE /srv/data | xargs)"
  mountpoint -q /srv/data && free="$(df -h --output=avail,pcent /srv/data | tail -1 | xargs)"
  if command -v docker >/dev/null 2>&1 && docker inspect family-photo-portal >/dev/null 2>&1; then
    portal="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' family-photo-portal)"
  fi
  command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1 && tail="connected"
  [[ "${BACKUP_CONFIGURED:-no}" == yes ]] && backup="$(mountpoint -q "$DP_INDEPENDENT" && echo mounted || echo unavailable)"
  printf 'David-Pi %s\n  Portal: %s\n  Data: %s (%s free)\n  Tailscale: %s\n  Independent backup: %s\n' \
    "$DP_VERSION" "$portal" "$source" "$free" "$tail" "$backup"
}

dp_verify() {
  dp_load_state
  DP_VERIFY_FAILURES=0
  echo "David-Pi verification"
  dp_check "external/trial data mount is active" mountpoint -q /srv/data
  dp_check "storage sentinel is valid" sh -c "test \"\$(cat '$DP_DATA/.david-pi-storage' 2>/dev/null)\" = '$DP_SENTINEL_VALUE'"
  dp_check "portal container exists" docker inspect family-photo-portal
  dp_check_output "portal container is healthy" '^healthy$' docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' family-photo-portal
  dp_check_output "portal process runs as UID 10001" '^10001$' docker exec family-photo-portal sh -c 'id -u'
  dp_check_output "portal has no effective Linux capabilities" '^CapEff:[[:space:]]+0000000000000000$' docker exec family-photo-portal sh -c "grep '^CapEff:' /proc/1/status"
  dp_check_output "portal has no-new-privileges" '^NoNewPrivs:[[:space:]]+1$' docker exec family-photo-portal sh -c "grep '^NoNewPrivs:' /proc/1/status"
  dp_check_output "portal root filesystem is read-only" '^true$' docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' family-photo-portal
  dp_check_output "portal publishes loopback port 8090 only" '^127\.0\.0\.1:8090$' docker inspect -f '{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostIp}}:{{(index (index .NetworkSettings.Ports "8000/tcp") 0).HostPort}}' family-photo-portal
  dp_check "loopback health endpoint responds" curl -fsS --max-time 8 http://127.0.0.1:8090/health
  if command -v tailscale >/dev/null 2>&1; then
    dp_check "Tailscale is connected" tailscale status
    dp_check_output "Tailscale Serve targets loopback" '127\.0\.0\.1:8090' tailscale serve status
    if tailscale funnel status 2>&1 | grep -Eqi 'funnel on|public'; then
      echo 'FAIL  Tailscale Funnel is disabled'; DP_VERIFY_FAILURES=$((DP_VERIFY_FAILURES + 1))
    else
      echo 'PASS  Tailscale Funnel is disabled'
    fi
  fi
  if [[ "${INSTALL_PIHOLE:-no}" == yes ]]; then
    dp_check_output "Pi-hole container is healthy" '^healthy$' docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' pihole
    dp_check_output "Pi-hole admin is loopback-only" '^127\.0\.0\.1:8081$' docker inspect -f '{{(index (index .NetworkSettings.Ports "80/tcp") 0).HostIp}}:{{(index (index .NetworkSettings.Ports "80/tcp") 0).HostPort}}' pihole
  fi
  if [[ "${BACKUP_CONFIGURED:-no}" == yes ]]; then
    dp_check "independent backup mount is active" mountpoint -q "$DP_INDEPENDENT"
    local primary_parent backup_parent
    primary_parent="$(dp_physical_parent "$(findmnt -n -o SOURCE /srv/data)")"
    backup_parent="$(dp_physical_parent "$(findmnt -n -o SOURCE "$DP_INDEPENDENT")")"
    if [[ -n "$primary_parent" && -n "$backup_parent" && "$primary_parent" != "$backup_parent" ]]; then
      echo 'PASS  primary and backup use different physical devices'
    else
      echo 'FAIL  primary and backup use different physical devices'; DP_VERIFY_FAILURES=$((DP_VERIFY_FAILURES + 1))
    fi
  fi
  dp_check "runtime image does not contain /app/.env" sh -c "! docker run --rm --entrypoint sh \"\$(docker inspect -f '{{.Config.Image}}' family-photo-portal)\" -c 'test -e /app/.env'"
  dp_check "Docker socket is not mounted" sh -c "! docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' family-photo-portal | grep -qx /var/run/docker.sock"
  echo "Result: $DP_VERIFY_FAILURES failure(s)"
  (( DP_VERIFY_FAILURES == 0 ))
}
