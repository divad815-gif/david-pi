#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"
source "$DP_ROOT/installer/lib/platform.sh"
source "$DP_ROOT/installer/lib/preflight.sh"

# Prerequisites come only from the distribution and vendors' signed apt feeds.
# No storage/SSH/DNS changes are made here.
dp_install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg python3 openssl rsync tzdata util-linux
  local docker_os codename arch tailscale_os
  docker_os="$(dp_docker_repo_os)"
  codename="$(dp_os_codename)"
  arch="$(dpkg --print-architecture)"
  if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://download.docker.com/linux/${docker_os}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod 0644 /etc/apt/keyrings/docker.asc
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' "$arch" "$docker_os" "$codename" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  docker compose version >/dev/null || dp_die "Docker Compose v2 is required; install the vendor compose plugin before resuming"
  systemctl enable --now docker
  if ! command -v tailscale >/dev/null 2>&1; then
    tailscale_os="$docker_os"
    curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://pkgs.tailscale.com/stable/${tailscale_os}/${codename}.noarmor.gpg" -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl --fail --silent --show-error --proto '=https' --tlsv1.2 "https://pkgs.tailscale.com/stable/${tailscale_os}/${codename}.tailscale-keyring.list" -o /etc/apt/sources.list.d/tailscale.list
    apt-get update
    apt-get install -y tailscale
  fi
  systemctl enable --now tailscaled
}

dp_setup() {
  dp_require_root
  umask 077
  install -d -m 0700 "$DP_ETC"
  touch "$DP_LOG"; chmod 0600 "$DP_LOG"
  dp_preflight || dp_die "Preflight failed; no installation changes were made"
  local admin='' hostname='' repository="${DAVID_PI_REPOSITORY:-divad815-gif/david-pi}" image="${DAVID_PI_IMAGE_OVERRIDE:-}" owner_name='' status
  while (($#)); do
    case "$1" in
      --admin) admin="${2:-}"; shift 2 ;;
      --hostname) hostname="${2:-}"; shift 2 ;;
      --image) image="${2:-}"; shift 2 ;;
      --repository) repository="${2:-}"; shift 2 ;;
      *) dp_die "Unknown setup option: $1" ;;
    esac
  done
  if [[ -f "$DP_ETC/installation.json" ]]; then
    if [[ ! -f "$DP_ETC/host-state/installed.json" ]]; then
      python3 "$DP_ROOT/installer/host.py" operation repair
    else
      echo "Already configured. Open your private website's administrator settings or run sudo david-pi status."
    fi
    return
  fi
  [[ "$image" =~ ^ghcr\.io/[a-z0-9_.-]+/david-pi@sha256:[0-9a-f]{64}$ ]] || dp_die "Use a verified stable release installer; a pinned image digest is required"
  if [[ -z "$admin" ]]; then read -r -p "Your exact Tailscale account login (usually email): " admin; fi
  if [[ -z "$hostname" ]]; then
    read -r -p "Your first name (for the suggested server hostname): " owner_name
    hostname="$(printf '%s' "$owner_name" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-50)-pi"
    local chosen
    read -r -p "Server hostname [$hostname]: " chosen
    hostname="${chosen:-$hostname}"
  fi
  if systemctl is-active -q david-pi-portal.service; then
    dp_die "An existing legacy David-Pi service is running. Use the separately rehearsed migration; fresh setup did not change it"
  fi
  dp_install_packages
  status="$(tailscale status --json 2>/dev/null || echo '{}')"
  if ! python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get("BackendState")=="Running" else 1)' <<< "$status"; then
    echo "Sign in using the Tailscale URL below. Keep the same individual account for the setup wizard."
    tailscale up
  fi
  # Refuse an unrelated listener before enabling our temporary setup service.
  if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)8091$' && ! systemctl is-active -q david-pi-helper.service; then
    dp_die "Local port 8091 is already in use; existing service was preserved"
  fi
  if ss -ltnH | awk '{print $4}' | grep -Eq '(^|:)8090$' && ! systemctl is-active -q david-pi-portal.service; then
    dp_die "Local port 8090 is already in use; existing service was preserved"
  fi
  local release_root="/usr/local/lib/david-pi-releases/$DP_VERSION" pointer="/usr/local/lib/.david-pi-pointer-$$" release_stage="/usr/local/lib/david-pi-releases/.new-$$"
  if [[ -e /usr/local/lib/david-pi && ! -L /usr/local/lib/david-pi ]]; then
    dp_die "A legacy host-code directory exists. Use the separately rehearsed migration; it was preserved"
  fi
  install -d -m 0755 /usr/local/lib/david-pi-releases
  if [[ ! -e "$release_root" ]]; then
    install -d -m 0755 "$release_stage"
    rsync -a --exclude .git --exclude '__pycache__' --exclude '.venv' --exclude '.env' --exclude 'clients/android/.gradle' --exclude 'clients/android/**/build' "$DP_ROOT/" "$release_stage/"
    mv -T -- "$release_stage" "$release_root"
  fi
  [[ -x "$release_root/david-pi" ]] || dp_die "Installed release is incomplete; inspect $release_root before retrying"
  ln -s -- "$release_root" "$pointer"
  mv -Tf -- "$pointer" /usr/local/lib/david-pi
  ln -sfn /usr/local/lib/david-pi/david-pi /usr/local/bin/david-pi
  install -d -m 0755 /run/david-pi
  printf '%s\n' '{"schema_version":1,"overall_state":"unavailable","subsystems":{}}' > /run/david-pi/server-status.json
  for name in david-pi-helper.service david-pi-portal.service david-pi-status.service david-pi-status.timer; do
    install -m 0644 "$DP_ROOT/installer/systemd/$name" "/etc/systemd/system/$name"
  done
  systemctl daemon-reload
  python3 /usr/local/lib/david-pi/installer/host.py initialize --admin "$admin" --hostname "$hostname" --image "$image" --repository "$repository"
}
