#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

DP_HAGEZI_LIGHT='https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/light.txt'

dp_install_pihole() {
  local password tz
  dp_secret_prompt password "Choose a Pi-hole admin password"
  [[ ${#password} -ge 10 ]] || dp_die "Pi-hole password must be at least 10 characters"
  [[ "$password" =~ ^[A-Za-z0-9._~-]+$ ]] || dp_die "Use 10 or more letters, numbers, and . _ ~ - so Docker can parse the secret safely"
  tz="$(cat /etc/timezone 2>/dev/null || echo UTC)"
  install -d -m 0750 /srv/compose/pihole /srv/data/pihole
  install -m 0640 "$DP_ROOT/installer/templates/pihole-compose.yaml" /srv/compose/pihole/compose.yaml
  dp_atomic_write /srv/compose/pihole/.env 0600 root root <<EOF
TZ=$tz
PIHOLE_PASSWORD=$password
EOF
  install -m 0644 "$DP_ROOT/installer/systemd/david-pi-pihole.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now david-pi-pihole.service
  dp_run_timeout 180 sh -c 'until docker inspect pihole --format "{{.State.Health.Status}}" 2>/dev/null | grep -qx healthy; do sleep 3; done'
  dp_configure_pihole_light
  dp_log "Pi-hole installed with loopback-only administration and HaGeZi Light"
}

dp_configure_pihole_light() {
  local db=/srv/data/pihole/gravity.db
  [[ -f "$db" ]] || dp_die "Pi-hole gravity database is unavailable"
  install -d -m 0700 /srv/backups/pihole
  cp -a "$db" "/srv/backups/pihole/gravity-$(date -u +%Y%m%dT%H%M%SZ).db"
  docker exec pihole pihole-FTL sqlite3 /etc/pihole/gravity.db \
    "UPDATE adlist SET enabled=0; INSERT INTO adlist(address,enabled,comment) SELECT '$DP_HAGEZI_LIGHT',1,'HaGeZi Light - relaxed low-breakage household blocking' WHERE NOT EXISTS (SELECT 1 FROM adlist WHERE address='$DP_HAGEZI_LIGHT'); UPDATE adlist SET enabled=1,comment='HaGeZi Light - relaxed low-breakage household blocking' WHERE address='$DP_HAGEZI_LIGHT';"
  dp_run_timeout 600 docker exec pihole pihole -g
  docker exec pihole pihole status | grep -qi enabled || dp_die "Pi-hole blocking is not enabled"
}
