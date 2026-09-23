#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_pihole_setup() {
  dp_require_root
  cat <<'HELP'
Pi-hole is optional. This release supports connecting an existing local Pi-hole
query database for aggregate statistics. No individual DNS queries are shown.

If you have not installed Pi-hole, follow its official Docker installation:
  https://docs.pi-hole.net/docker/
Do not change your router or Tailscale DNS until Pi-hole answers local tests.
Changing DNS is a separate, deliberate action in those products.

To connect an existing local instance, enter the full host path of its
pihole-FTL.db file. For Docker, use the host path of its persistent /etc/pihole
volume. Press Enter to skip. This connection does not change DNS or Pi-hole.
HELP
  local database
  read -r -p 'Existing pihole-FTL.db path: ' database
  [[ -n "$database" ]] || return 0
  python3 "$DP_ROOT/installer/host.py" pihole-connect "$database"
  install -m 0644 "$DP_ROOT/installer/systemd/david-pi-pihole-summary.service" /etc/systemd/system/
  install -m 0644 "$DP_ROOT/installer/systemd/david-pi-pihole-summary.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now david-pi-pihole-summary.timer
  echo 'Pi-hole aggregate statistics connected. Your DNS settings were preserved.'
}
