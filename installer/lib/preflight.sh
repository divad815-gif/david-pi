#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"
source "$DP_ROOT/installer/lib/platform.sh"

dp_preflight() {
  local failures=0 warnings=0 model arch normalized_arch os free_kb memory_mib cpu_count profile
  model="$(dp_hardware_model)"
  arch="$(uname -m)"
  normalized_arch="$(dp_normalized_arch)"
  os="$(. /etc/os-release; echo "${PRETTY_NAME:-unknown}")"
  free_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
  memory_mib="$(dp_memory_mib)"
  cpu_count="$(dp_cpu_count)"
  profile="$(dp_platform_profile)"

  echo "David-Pi preflight"
  echo "  Model:        $model"
  echo "  Profile:      $profile"
  echo "  OS:           $os"
  echo "  Architecture: $arch ($normalized_arch)"
  echo "  CPU cores:    $cpu_count"
  echo "  Memory:       $memory_mib MiB"
  echo "  Root free:    $((free_kb / 1024)) MiB"

  if [[ "$normalized_arch" == unsupported ]]; then
    echo "FAIL: a 64-bit amd64/x86_64 or arm64/aarch64 processor is required"
    failures=$((failures + 1))
  fi
  if dp_is_wsl; then
    echo "FAIL: WSL is for testing only; install native Debian or Ubuntu Server for unattended hosting"
    failures=$((failures + 1))
  fi
  if ! dp_supported_os; then
    echo "FAIL: supported hosts are Debian 13 AMD64, Ubuntu Server 24.04 AMD64, and Raspberry Pi OS Debian 13 ARM64 (Pi 4/5)"
    failures=$((failures + 1))
  fi
  if ! command -v systemctl >/dev/null 2>&1 || [[ "$(ps -p 1 -o comm= 2>/dev/null)" != systemd ]]; then
    echo "FAIL: systemd must be the host service manager"
    failures=$((failures + 1))
  fi
  if (( memory_mib < 3500 )); then
    echo "FAIL: at least 4 GB of installed RAM is required"
    failures=$((failures + 1))
  fi
  if (( cpu_count < 2 )); then
    echo "FAIL: at least two CPU cores are required"
    failures=$((failures + 1))
  fi
  if (( free_kb < 8 * 1024 * 1024 )); then
    echo "FAIL: at least 8 GiB free on the OS filesystem is required"
    failures=$((failures + 1))
  fi
  if ! getent hosts deb.debian.org >/dev/null 2>&1; then
    echo "FAIL: DNS/Internet preflight could not resolve deb.debian.org"
    failures=$((failures + 1))
  fi
  for port in 53 443 8081 8090 8091; do
    if ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "(^|:)$port$"; then
      echo "WARN: TCP port $port is already in use; setup will identify the owner before continuing"
      warnings=$((warnings + 1))
    fi
  done
  if ss -lunH 2>/dev/null | awk '{print $5}' | grep -Eq '(^|:)53$'; then
    echo "WARN: UDP port 53 is already in use; Pi-hole cannot start until the owner is resolved"
    warnings=$((warnings + 1))
  fi
  if [[ -f /var/run/reboot-required ]]; then
    echo "WARN: the host currently requires a reboot"
    warnings=$((warnings + 1))
  fi
  if [[ "$profile" == linux-laptop ]]; then
    echo "WARN: laptop detected; configure lid/suspend behavior explicitly in your OS for unattended hosting"
    warnings=$((warnings + 1))
  fi
  echo "Result: $failures failure(s), $warnings warning(s)"
  (( failures == 0 ))
}
