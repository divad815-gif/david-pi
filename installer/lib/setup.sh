#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"
source "$DP_ROOT/installer/lib/platform.sh"
source "$DP_ROOT/installer/lib/preflight.sh"
source "$DP_ROOT/installer/lib/storage.sh"
source "$DP_ROOT/installer/lib/ssh.sh"
source "$DP_ROOT/installer/lib/pihole.sh"

dp_setup() {
  dp_require_root
  umask 077
  install -d -m 0700 "$DP_ETC"
  touch "$DP_LOG"; chmod 0600 "$DP_LOG"
  dp_load_state
  dp_select_image
  dp_save_state DAVID_PI_IMAGE "$DAVID_PI_IMAGE"
  # Defaults keep upgrades from an older installer state resumable. Older
  # state files predate the generic-host questions and omit these keys.
  INSTANCE_NAME="${INSTANCE_NAME:-David-Pi}"
  CONFIGURE_ALWAYS_ON="${CONFIGURE_ALWAYS_ON:-no}"
  PLATFORM_PROFILE="${PLATFORM_PROFILE:-$(dp_platform_profile)}"
  if [[ -z "${PORTAL_MEMORY_LIMIT:-}" || -z "${PORTAL_CPU_LIMIT:-}" || -z "${PORTAL_PID_LIMIT:-}" ]]; then
    IFS='|' read -r PORTAL_MEMORY_LIMIT PORTAL_CPU_LIMIT PORTAL_PID_LIMIT <<< "$(dp_resource_defaults)"
  fi
  dp_log "Starting or resuming David-Pi $DP_VERSION setup"

  if ! dp_phase_complete PREFLIGHT; then
    dp_preflight || dp_die "Preflight failed; no installation changes were made"
    dp_mark_phase PREFLIGHT; dp_load_state
  fi
  if ! dp_phase_complete CHOICES; then
    dp_collect_choices
    dp_mark_phase CHOICES; dp_load_state
  fi
  if ! dp_phase_complete HOST_POLICY; then
    dp_configure_host_policy
    dp_mark_phase HOST_POLICY; dp_load_state
  fi
  if ! dp_phase_complete STORAGE; then
    dp_configure_storage
    dp_mark_phase STORAGE; dp_load_state
  fi
  if ! dp_phase_complete PACKAGES; then
    dp_install_packages
    dp_mark_phase PACKAGES; dp_load_state
  fi
  if ! dp_phase_complete APPLICATION; then
    dp_install_application
    dp_mark_phase APPLICATION; dp_load_state
  fi
  if ! dp_phase_complete TAILSCALE; then
    dp_configure_tailscale
    dp_mark_phase TAILSCALE; dp_load_state
  fi
  if [[ "${INSTALL_PIHOLE:-no}" == yes ]] && ! dp_phase_complete PIHOLE; then
    dp_install_pihole
    dp_mark_phase PIHOLE; dp_load_state
  fi
  if ! dp_phase_complete SERVICES; then
    dp_install_services
    dp_mark_phase SERVICES; dp_load_state
  fi
  if ! dp_phase_complete SSH; then
    # ADMIN_USER is populated by dp_collect_choices or the root-only state file.
    # shellcheck disable=SC2153
    dp_ssh_prepare "$ADMIN_USER"
    dp_mark_phase SSH; dp_load_state
  fi
  source "$DP_ROOT/installer/lib/verify.sh"
  dp_verify
  dp_mark_phase VERIFIED
  dp_log "Installation complete: https://$TAIL_DNS/"
  cat <<EOF

David-Pi installation complete.
Private website: https://$TAIL_DNS/
Pi-hole selected: $INSTALL_PIHOLE
Independent backup configured: ${BACKUP_CONFIGURED:-no}

Manual steps are listed in:
  $DP_ETC/INSTALLATION_REPORT.md
EOF
}

dp_collect_choices() {
  local detected_user profile_choice resource_defaults
  detected_user="${SUDO_USER:-}"
  [[ -n "$detected_user" && "$detected_user" != root ]] || detected_user="$(logname 2>/dev/null || true)"
  dp_prompt ADMIN_USER "Linux administrator username" "$detected_user"
  id "$ADMIN_USER" >/dev/null || dp_die "User $ADMIN_USER does not exist"
  dp_prompt HOSTNAME_VALUE "Hostname" "david-pi"
  [[ "$HOSTNAME_VALUE" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]{0,62}$ ]] || dp_die "Invalid hostname"
  dp_prompt INSTANCE_NAME "Household/server display name" "David-Pi"
  [[ "$INSTANCE_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9._[:space:]-]{0,79}$ ]] || dp_die "Display name must contain 1-80 safe characters"
  PLATFORM_PROFILE="$(dp_platform_profile)"
  resource_defaults="$(dp_resource_defaults)"
  IFS='|' read -r PORTAL_MEMORY_LIMIT PORTAL_CPU_LIMIT PORTAL_PID_LIMIT <<< "$resource_defaults"
  if [[ "$PLATFORM_PROFILE" == linux-laptop ]]; then
    dp_yes_no CONFIGURE_ALWAYS_ON "Configure this dedicated laptop to ignore lid-close and automatic suspend" yes
  else
    CONFIGURE_ALWAYS_ON=no
  fi
  cat <<'EOF'
Storage profile:
  1) Trial on the OS disk
  2) Dedicated primary storage only (internal or external)
  3) Dedicated primary plus physically independent backup
  4) Restore existing David-Pi data (storage is selected first)
EOF
  dp_prompt profile_choice "Choose 1, 2, 3, or 4" 2
  case "$profile_choice" in
    1) INSTALL_PROFILE=trial ;;
    2) INSTALL_PROFILE=primary ;;
    3) INSTALL_PROFILE=protected ;;
    4) INSTALL_PROFILE=restore ;;
    *) dp_die "Invalid storage profile" ;;
  esac
  dp_yes_no INSTALL_PIHOLE "Install Pi-hole" no
  dp_yes_no ENABLE_EXIT_NODE "Advertise this server as a Tailscale exit node" no
  dp_yes_no ENABLE_TMDB "Enable TMDB search/posters for Movie Night" no
  dp_prompt MOVIE_REGION "Movie region (two-letter country code)" US
  MOVIE_REGION="${MOVIE_REGION^^}"
  [[ "$MOVIE_REGION" =~ ^[A-Z]{2}$ ]] || dp_die "Movie region must contain two letters"
  dp_yes_no ENABLE_WINDOWS_BRIDGE "Enable the optional Windows Assistant broker service" no
  for key in ADMIN_USER HOSTNAME_VALUE INSTANCE_NAME PLATFORM_PROFILE CONFIGURE_ALWAYS_ON PORTAL_MEMORY_LIMIT PORTAL_CPU_LIMIT PORTAL_PID_LIMIT INSTALL_PROFILE INSTALL_PIHOLE ENABLE_EXIT_NODE ENABLE_TMDB MOVIE_REGION ENABLE_WINDOWS_BRIDGE; do
    dp_save_state "$key" "${!key}"
  done
}

dp_configure_host_policy() {
  if [[ "${PLATFORM_PROFILE:-$(dp_platform_profile)}" != linux-laptop || "${CONFIGURE_ALWAYS_ON:-no}" != yes ]]; then
    return 0
  fi
  install -d -m 0755 /etc/systemd/logind.conf.d
  dp_backup_file /etc/systemd/logind.conf.d/60-david-pi-server.conf
  dp_atomic_write /etc/systemd/logind.conf.d/60-david-pi-server.conf 0644 root root <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
  systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
  dp_log "Configured always-on laptop policy; logind settings take full effect after reboot"
}

dp_configure_storage() {
  local selection primary_device primary_source primary_uuid backup_device backup_source backup_uuid
  if [[ "$INSTALL_PROFILE" == trial ]]; then
    install -d -m 0750 /srv/david-pi-trial-data /srv/data
    if ! mountpoint -q /srv/data; then
      grep -qE '^[^#]*/srv/david-pi-trial-data[[:space:]]+/srv/data[[:space:]]+none[[:space:]]+bind' /etc/fstab || {
        dp_backup_file /etc/fstab
        echo '/srv/david-pi-trial-data /srv/data none bind 0 0' >> /etc/fstab
      }
      mount --bind /srv/david-pi-trial-data /srv/data
    fi
    dp_save_state PRIMARY_DEVICE "$(dp_root_disk)"
    dp_save_state PRIMARY_SOURCE "$(findmnt -n -o SOURCE /)"
    dp_save_state PRIMARY_UUID "$(findmnt -n -o UUID /)"
    dp_save_state BACKUP_CONFIGURED no
    return
  fi

  if mountpoint -q /srv/data; then
    primary_source="$(findmnt -n -o SOURCE /srv/data)"
    primary_device="/dev/$(lsblk -ndo PKNAME "$primary_source" | head -n1)"
    primary_uuid="$(findmnt -n -o UUID /srv/data)"
    echo "Using existing /srv/data mount: $primary_source UUID=$primary_uuid"
  else
    selection="$(dp_select_existing_disk primary /srv/data)"
    [[ "$selection" != none ]] || dp_die "A dedicated primary disk is required for the selected profile"
    IFS='|' read -r primary_device primary_source primary_uuid <<< "$selection"
  fi
  [[ "$primary_device" != "$(dp_root_disk)" ]] || dp_die "Primary data must not be the OS disk in this profile"
  dp_save_state PRIMARY_DEVICE "$primary_device"
  dp_save_state PRIMARY_SOURCE "$primary_source"
  dp_save_state PRIMARY_UUID "$primary_uuid"

  if [[ "$INSTALL_PROFILE" == protected || "$INSTALL_PROFILE" == restore ]]; then
    if mountpoint -q "$DP_INDEPENDENT"; then
      backup_source="$(findmnt -n -o SOURCE "$DP_INDEPENDENT")"
      backup_device="/dev/$(lsblk -ndo PKNAME "$backup_source" | head -n1)"
      backup_uuid="$(findmnt -n -o UUID "$DP_INDEPENDENT")"
    else
      selection="$(dp_select_existing_disk backup "$DP_INDEPENDENT")"
      if [[ "$selection" != none ]]; then IFS='|' read -r backup_device backup_source backup_uuid <<< "$selection"; fi
    fi
    if [[ -n "${backup_device:-}" ]]; then
      [[ "$backup_device" != "$primary_device" ]] || dp_die "Backup and primary resolve to the same physical device"
      dp_save_state BACKUP_DEVICE "$backup_device"
      dp_save_state BACKUP_SOURCE "$backup_source"
      dp_save_state BACKUP_UUID "$backup_uuid"
      dp_save_state BACKUP_CONFIGURED yes
      printf '%s\n' 'david-pi-independent-backup-v1' > "$DP_INDEPENDENT/.david-pi-backup-storage"
      chmod 0600 "$DP_INDEPENDENT/.david-pi-backup-storage"
    else
      [[ "$INSTALL_PROFILE" != protected ]] || dp_die "Protected profile requires an independent backup device"
      dp_save_state BACKUP_CONFIGURED no
    fi
  else
    dp_save_state BACKUP_CONFIGURED no
  fi
}

dp_install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg python3 python3-venv sqlite3 rsync openssl parted smartmontools usbutils pciutils lm-sensors upower
  if apt-cache show nvme-cli >/dev/null 2>&1; then apt-get install -y nvme-cli; fi
  if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    local docker_os codename arch
    docker_os="$(dp_docker_repo_os)" || dp_die "No Docker repository mapping exists for this OS"
    curl -fsSL "https://download.docker.com/linux/${docker_os}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    codename="$(dp_os_codename)"
    arch="$(dpkg --print-architecture)"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' "$arch" "$docker_os" "$codename" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  systemctl enable --now docker
  if ! command -v tailscale >/dev/null 2>&1; then
    local tailscale_os
    codename="$(dp_os_codename)"
    tailscale_os="$(dp_os_id)"
    curl -fsSL "https://pkgs.tailscale.com/stable/${tailscale_os}/${codename}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL "https://pkgs.tailscale.com/stable/${tailscale_os}/${codename}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
    apt-get update
    apt-get install -y tailscale
  fi
  systemctl enable --now tailscaled
  if apt-cache show unattended-upgrades >/dev/null 2>&1; then
    apt-get install -y unattended-upgrades
    dp_atomic_write /etc/apt/apt.conf.d/52david-pi-unattended 0644 root root <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
  fi
}

dp_install_application() {
  local tmdb_token='' giphy_key='' shutdown_password=''
  install -d -m 0755 /usr/local/lib/david-pi
  rsync -a --delete --exclude .git --exclude 'clients/android/.gradle' --exclude 'clients/android/**/build' \
    "$DP_ROOT/" /usr/local/lib/david-pi/
  chmod 0755 /usr/local/lib/david-pi/david-pi
  ln -sfn /usr/local/lib/david-pi/david-pi /usr/local/bin/david-pi
  hostnamectl set-hostname "$HOSTNAME_VALUE"
  install -d -m 0750 -o 10001 -g 10001 \
    "$DP_DATA" "$DP_DATA/incoming" "$DP_DATA/tmp/uploads" "$DP_DATA/tmp/runtime" \
    "$DP_DATA/quarantine" "$DP_DATA/platform" "$DP_DATA/files" "$DP_DATA/audiobooks" "$DP_DATA/chat"
  printf '%s\n' "$DP_SENTINEL_VALUE" > "$DP_DATA/.david-pi-storage"
  chown 10001:10001 "$DP_DATA/.david-pi-storage"
  chmod 0640 "$DP_DATA/.david-pi-storage"
  install -d -m 0700 "$DP_BACKUPS"
  install -d -m 0755 /run/david-pi /run/david-pi-windows
  install -d -m 0750 -o "$ADMIN_USER" -g "$ADMIN_USER" "$DP_COMPOSE"
  rsync -a --delete \
    --exclude .git --exclude installer --exclude clients --exclude tests \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' --exclude 'static/apk/*.apk' \
    "$DP_ROOT/" "$DP_COMPOSE/"
  chown -R "$ADMIN_USER:$ADMIN_USER" "$DP_COMPOSE"
  find "$DP_COMPOSE" -type d -exec chmod 0750 {} +
  find "$DP_COMPOSE" -type f -exec chmod 0640 {} +
  chmod 0750 "$DP_COMPOSE/docker-entrypoint.sh"

  if [[ "$ENABLE_TMDB" == yes ]]; then
    echo "Create a TMDB API Read Access Token at https://www.themoviedb.org/settings/api"
    dp_secret_prompt tmdb_token "TMDB API Read Access Token"
    [[ -n "$tmdb_token" ]] || dp_die "TMDB was selected but no token was entered"
  fi
  dp_yes_no ENABLE_GIPHY "Enable optional GIF search in Chat" no
  if [[ "$ENABLE_GIPHY" == yes ]]; then
    dp_secret_prompt giphy_key "GIPHY API key"
  fi
  dp_secret_prompt shutdown_password "Choose a website safe-shutdown password (leave empty to disable)"
  dp_atomic_write "$DP_COMPOSE/.env" 0600 "$ADMIN_USER" "$ADMIN_USER" <<EOF
DAVID_PI_ALLOWED_HOSTS=127.0.0.1,localhost
DAVID_PI_IMAGE=$DAVID_PI_IMAGE
DAVID_PI_PUBLIC_URL=https://localhost
DAVID_PI_INSTANCE_NAME=$INSTANCE_NAME
DAVID_PI_PLATFORM_PROFILE=$PLATFORM_PROFILE
DAVID_PI_PORTAL_MEMORY=$PORTAL_MEMORY_LIMIT
DAVID_PI_PORTAL_CPUS=$PORTAL_CPU_LIMIT
DAVID_PI_PORTAL_PIDS=$PORTAL_PID_LIMIT
MOVIE_REGION=$MOVIE_REGION
TMDB_API_READ_TOKEN=$tmdb_token
GIPHY_API_KEY=$giphy_key
DAVID_PI_VAPID_PUBLIC_KEY=
DAVID_PI_FCM_CREDENTIAL_FILE=
IOS_SHORTCUT_ICLOUD_URL=
DAVID_PI_SHUTDOWN_PASSWORD_HASH_B64=
EOF

  if [[ "$DAVID_PI_IMAGE" == ghcr.io/*@sha256:* ]]; then
    docker pull "$DAVID_PI_IMAGE"
  else
    (cd "$DP_ROOT" && docker compose -f compose.yaml -f compose.dev.yaml build)
  fi
  dp_provision_runtime_secrets
  if [[ -n "$shutdown_password" ]]; then dp_set_shutdown_password "$shutdown_password"; fi
  dp_save_state ENABLE_GIPHY "$ENABLE_GIPHY"
}

dp_provision_runtime_secrets() {
  local secret_root="$DP_COMPOSE/secrets" chat_key="$DP_COMPOSE/secrets/chat-master.key" vapid_key="$DP_COMPOSE/secrets/chat-vapid-private.pem" vapid_public env_tmp
  install -d -m 0750 -o root -g 10001 "$secret_root"
  [[ -s "$chat_key" ]] || openssl rand -base64 32 > "$chat_key"
  [[ -s "$vapid_key" ]] || openssl ecparam -name prime256v1 -genkey -noout -out "$vapid_key"
  chown root:10001 "$chat_key" "$vapid_key"; chmod 0440 "$chat_key" "$vapid_key"
  vapid_public="$(docker run --rm --entrypoint python -v "$vapid_key:/run/key.pem:ro" "$DAVID_PI_IMAGE" -c 'import base64; from cryptography.hazmat.primitives import serialization; k=serialization.load_pem_private_key(open("/run/key.pem","rb").read(),password=None); r=k.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint); print(base64.urlsafe_b64encode(r).rstrip(b"=").decode())')"
  env_tmp="$(mktemp "$DP_COMPOSE/.env.XXXXXX")"
  awk -v value="$vapid_public" '/^DAVID_PI_VAPID_PUBLIC_KEY=/{print "DAVID_PI_VAPID_PUBLIC_KEY=" value; next} {print}' "$DP_COMPOSE/.env" > "$env_tmp"
  chown "$ADMIN_USER:$ADMIN_USER" "$env_tmp"; chmod 0600 "$env_tmp"; mv -f "$env_tmp" "$DP_COMPOSE/.env"
  tar -C "$secret_root" -czf "$DP_BACKUPS/$(date -u +%Y%m%dT%H%M%SZ)-chat-key-recovery.tgz" chat-master.key chat-vapid-private.pem
  chmod 0600 "$DP_BACKUPS"/*-chat-key-recovery.tgz
}

dp_set_shutdown_password() {
  local password="$1" encoded env_tmp
  encoded="$(docker run --rm --entrypoint python "$DAVID_PI_IMAGE" -c 'import base64,sys; from werkzeug.security import generate_password_hash; print(base64.b64encode(generate_password_hash(sys.stdin.read().rstrip("\n")).encode()).decode())' <<< "$password")"
  env_tmp="$(mktemp "$DP_COMPOSE/.env.XXXXXX")"
  awk -v value="$encoded" '/^DAVID_PI_SHUTDOWN_PASSWORD_HASH_B64=/{print "DAVID_PI_SHUTDOWN_PASSWORD_HASH_B64=" value; next} {print}' "$DP_COMPOSE/.env" > "$env_tmp"
  chown "$ADMIN_USER:$ADMIN_USER" "$env_tmp"; chmod 0600 "$env_tmp"; mv -f "$env_tmp" "$DP_COMPOSE/.env"
}

dp_configure_tailscale() {
  if ! tailscale status >/dev/null 2>&1; then
    echo "Tailscale will print a private login URL. Open it and approve this server."
    tailscale up
  fi
  if [[ "$ENABLE_EXIT_NODE" == yes ]]; then tailscale set --advertise-exit-node; fi
  TAIL_DNS="$(tailscale status --json | python3 -c 'import json,sys; print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))')"
  [[ -n "$TAIL_DNS" ]] || dp_die "Tailscale is connected but no MagicDNS hostname is available"
  dp_save_state TAIL_DNS "$TAIL_DNS"
  local env_tmp
  env_tmp="$(mktemp "$DP_COMPOSE/.env.XXXXXX")"
  awk -v hosts="$TAIL_DNS,127.0.0.1,localhost" -v url="https://$TAIL_DNS" '
    /^DAVID_PI_ALLOWED_HOSTS=/{print "DAVID_PI_ALLOWED_HOSTS=" hosts; next}
    /^DAVID_PI_PUBLIC_URL=/{print "DAVID_PI_PUBLIC_URL=" url; next}
    {print}
  ' "$DP_COMPOSE/.env" > "$env_tmp"
  chown "$ADMIN_USER:$ADMIN_USER" "$env_tmp"; chmod 0600 "$env_tmp"; mv -f "$env_tmp" "$DP_COMPOSE/.env"
  tailscale serve reset || true
  tailscale serve --bg http://127.0.0.1:8090
  tailscale funnel status 2>&1 | grep -qi 'tailnet only' || dp_die "Funnel/private status could not be confirmed"
}

dp_install_services() {
  local name source target
  for source in \
    david-pi-backup.py:david-pi-backup \
    david-pi-data-backup:david-pi-data-backup \
    david-pi-pihole-summary.py:david-pi-pihole-summary \
    david-pi-server-status.py:david-pi-server-status \
    david-pi-assistant-diagnostics.py:david-pi-assistant-diagnostics \
    david-pi-windows-bridge.py:david-pi-windows-bridge \
    david_pi_safe_shutdown.py:david_pi_safe_shutdown.py; do
    target="${source#*:}"; source="${source%%:*}"
    [[ -f "$DP_COMPOSE/deploy/$source" ]] && install -m 0750 "$DP_COMPOSE/deploy/$source" "/usr/local/sbin/$target"
  done
  for name in "$DP_COMPOSE"/deploy/*.service "$DP_COMPOSE"/deploy/*.timer "$DP_COMPOSE"/deploy/*.path; do
    [[ -f "$name" ]] && install -m 0644 "$name" /etc/systemd/system/
  done
  systemctl daemon-reload
  install -d -m 0755 /run/david-pi
  [[ -f /run/david-pi/server-status.json ]] || printf '%s\n' '{"schema_version":1,"generated_at":null,"overall_state":"unavailable","subsystems":{}}' > /run/david-pi/server-status.json
  [[ -f /run/david-pi/backup-status.json ]] || printf '%s\n' '{"ok":false,"last_attempt":null,"error":"not_run_yet"}' > /run/david-pi/backup-status.json
  [[ -f /run/david-pi/pihole-summary.json ]] || printf '%s\n' '{"stale":true,"updated_at":null,"total":0,"blocked":0}' > /run/david-pi/pihole-summary.json
  chmod 0644 /run/david-pi/*.json
  systemctl enable --now david-pi-backup.timer david-pi-server-status.timer david-pi-assistant-diagnostics.timer david-pi-safe-shutdown.path
  if [[ "$INSTALL_PIHOLE" == yes ]]; then systemctl enable --now david-pi-pihole-summary.timer; else systemctl disable david-pi-pihole-summary.timer 2>/dev/null || true; fi
  if [[ "${BACKUP_CONFIGURED:-no}" == yes ]]; then systemctl enable --now david-pi-data-backup.timer; else systemctl disable david-pi-data-backup.timer 2>/dev/null || true; fi
  if [[ "$ENABLE_WINDOWS_BRIDGE" == yes ]]; then systemctl enable --now david-pi-windows-bridge.service; else systemctl disable david-pi-windows-bridge.service 2>/dev/null || true; fi
  systemctl enable --now david-pi-portal.service
  if [[ "$INSTALL_PIHOLE" == yes ]]; then systemctl start david-pi-pihole-summary.service; fi
  systemctl start david-pi-backup.service david-pi-server-status.service
  dp_write_install_report
}

dp_write_install_report() {
  dp_atomic_write "$DP_ETC/INSTALLATION_REPORT.md" 0600 root root <<EOF
# David-Pi installation report

- Version: $DP_VERSION
- Instance name: ${INSTANCE_NAME:-David-Pi}
- Hostname: $HOSTNAME_VALUE
- Platform profile: ${PLATFORM_PROFILE:-unknown}
- Architecture: $(dp_normalized_arch)
- Portal limits: ${PORTAL_MEMORY_LIMIT:-unknown} memory, ${PORTAL_CPU_LIMIT:-unknown} CPUs, ${PORTAL_PID_LIMIT:-unknown} PIDs
- Private URL: https://${TAIL_DNS:-pending}/
- Profile: $INSTALL_PROFILE
- Primary source: ${PRIMARY_SOURCE:-unknown}
- Primary UUID: ${PRIMARY_UUID:-unknown}
- Independent backup: ${BACKUP_CONFIGURED:-no}
- Pi-hole: $INSTALL_PIHOLE
- Exit-node advertisement: $ENABLE_EXIT_NODE
- TMDB configured: $ENABLE_TMDB

## Manual actions

1. Apply the generated Tailscale ACL/grants example manually after replacing placeholders.
2. If Pi-hole was selected, verify DNS locally before changing router or Tailscale global DNS.
3. Test the website from a LAN device with Tailscale disconnected; it must fail.
4. Run sudo david-pi restore-test and record the result.
5. Keep the OS image and recovery material offline.
EOF
}
