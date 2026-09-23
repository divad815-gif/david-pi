#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_support_bundle() {
  dp_require_root
  local output="${1:-/tmp/david-pi-support-$(date -u +%Y%m%dT%H%M%SZ).json}"
  # No logs, identities, hostnames, paths, environment, or credentials leave the host.
  python3 - "$output" <<'PY'
import json,os,platform,subprocess,sys
value={"architecture":platform.machine(),"system":platform.system()}
for name in ("david-pi-helper.service","david-pi-portal.service"):
    result=subprocess.run(["systemctl","is-active",name],capture_output=True,text=True)
    value[name]=result.stdout.strip()
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
with os.fdopen(fd,'w') as stream:json.dump(value,stream,indent=2)
print(sys.argv[1])
PY
}

dp_uninstall_app() {
  dp_require_root
  local confirm
  read -r -p "Type REMOVE APP to stop application services and preserve all content, keys and backups: " confirm
  [[ "$confirm" == 'REMOVE APP' ]] || dp_die "Confirmation did not match; nothing changed"
  systemctl disable --now david-pi-portal.service david-pi-helper.service david-pi-status.timer
  systemctl disable --now david-pi-pihole-summary.timer 2>/dev/null || true
  docker compose --project-name david-pi -f "$DP_ETC/compose.json" down
  echo "Application stopped. Content, keys, configuration, backups, Tailscale and Pi-hole were preserved."
}
