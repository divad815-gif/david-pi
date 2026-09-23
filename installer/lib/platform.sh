#!/usr/bin/env bash
set -Eeuo pipefail

# Hardware and operating-system discovery shared by preflight and setup.  The
# application is platform neutral; these helpers keep host-specific policy at
# the installer boundary.

dp_os_id() {
  (. /etc/os-release 2>/dev/null; printf '%s\n' "${ID:-unknown}")
}

dp_os_codename() {
  (. /etc/os-release 2>/dev/null; printf '%s\n' "${VERSION_CODENAME:-unknown}")
}

dp_os_version() {
  (. /etc/os-release 2>/dev/null; printf '%s\n' "${VERSION_ID:-unknown}")
}

dp_normalized_arch() {
  case "$(uname -m)" in
    aarch64|arm64) echo arm64 ;;
    x86_64|amd64) echo amd64 ;;
    *) echo unsupported ;;
  esac
}

dp_hardware_model() {
  local model=''
  if [[ -r /proc/device-tree/model ]]; then
    model="$(tr -d '\0' < /proc/device-tree/model)"
  elif [[ -r /sys/class/dmi/id/product_name ]]; then
    model="$(tr -d '\0' < /sys/class/dmi/id/product_name)"
  fi
  printf '%s\n' "${model:-Generic Linux host}"
}

dp_has_battery() {
  compgen -G '/sys/class/power_supply/BAT*' >/dev/null 2>&1
}

dp_is_wsl() {
  grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null
}

dp_platform_profile() {
  local model
  model="$(dp_hardware_model)"
  if [[ "$model" == *"Raspberry Pi"* ]]; then
    echo raspberry-pi
  elif dp_has_battery; then
    echo linux-laptop
  else
    echo linux-server
  fi
}

dp_memory_mib() {
  awk '/^MemTotal:/ {print int($2 / 1024); exit}' /proc/meminfo
}

dp_cpu_count() {
  getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo 1
}

dp_supported_os() {
  local id version arch
  id="$(dp_os_id)"
  version="$(dp_os_version)"
  arch="$(dp_normalized_arch)"
  case "$id:$version:$arch" in
    debian:13:amd64|ubuntu:24.04:amd64) return 0 ;;
    debian:13:arm64|raspbian:13:arm64) [[ "$(dp_hardware_model)" == *"Raspberry Pi 4"* || "$(dp_hardware_model)" == *"Raspberry Pi 5"* ]] ;;
    *) return 1 ;;
  esac
}

dp_resource_defaults() {
  local memory_mib cpu_count portal_memory portal_cpus
  memory_mib="$(dp_memory_mib)"
  cpu_count="$(dp_cpu_count)"

  if (( memory_mib < 6144 )); then
    portal_memory=1536m
  elif (( memory_mib < 12288 )); then
    portal_memory=2048m
  else
    portal_memory=3072m
  fi

  if (( cpu_count <= 2 )); then
    portal_cpus=1.0
  elif (( cpu_count == 3 )); then
    portal_cpus=2.0
  elif (( cpu_count == 4 )); then
    portal_cpus=3.0
  elif (( cpu_count <= 8 )); then
    portal_cpus="$((cpu_count - 1)).0"
  else
    portal_cpus=6.0
  fi

  printf '%s|%s|%s\n' "$portal_memory" "$portal_cpus" 256
}

dp_docker_repo_os() {
  case "$(dp_os_id)" in
    debian|raspbian) echo debian ;;
    ubuntu) echo ubuntu ;;
    *) return 1 ;;
  esac
}
