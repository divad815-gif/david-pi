#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/setup.sh"

dp_prepare_recovery() {
  dp_require_root
  (( $# == 0 )) || dp_die "Recovery preparation has no options; use the verified release installer with --prepare-recovery"
  umask 077
  # Check the verified selection and clean target before installing packages.
  python3 "$DP_ROOT/installer/host.py" --etc "$DP_ETC" recovery-preflight
  dp_preflight || dp_die "Preflight failed; recovery preparation did not change services"
  if systemctl is-active -q david-pi-portal.service; then
    dp_die "An existing David-Pi service is running; it was preserved. Use a clean replacement machine"
  fi
  if [[ -e /usr/local/lib/david-pi && ! -L /usr/local/lib/david-pi ]]; then
    dp_die "An existing host-code directory was preserved. Use a clean replacement machine"
  fi
  install -d -m 0700 "$DP_ETC"
  touch "$DP_LOG"; chmod 0600 "$DP_LOG"
  dp_install_packages
  local status admin hostname
  status="$(tailscale status --json 2>/dev/null || echo '{}')"
  printf '\n1. Connect the replacement server to the intended Tailscale network\n'
  echo "Use an individual account. Restoring keeps the household's saved members and roles."
  if ! python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get("BackendState")=="Running" else 1)' <<< "$status"; then
    echo "Open the sign-in link below, verify the account/network, and approve this replacement server."
    tailscale up
  else
    echo "The existing Tailscale connection and settings are preserved."
  fi
  read -r -p "Exact Tailscale account email that owns this replacement node: " admin
  read -r -p "Replacement server hostname (for example john-pi): " hostname
  if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)8091$' && ! systemctl is-active -q david-pi-helper.service; then
    dp_die "Local port 8091 is in use by another service; it was preserved"
  fi
  if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)8090$'; then
    dp_die "Local port 8090 is in use; its existing service was preserved"
  fi
  dp_install_host_files
  python3 /usr/local/lib/david-pi/installer/host.py --etc "$DP_ETC" prepare-recovery --admin "$admin" --hostname "$hostname"
}
