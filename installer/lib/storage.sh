#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_root_disk() {
  local root_source parent
  root_source="$(findmnt -n -o SOURCE /)"
  parent="$(lsblk -ndo PKNAME "$root_source" 2>/dev/null | head -n1)"
  [[ -n "$parent" ]] && echo "/dev/$parent" || echo "$root_source"
}

dp_list_storage() {
  local root_disk
  root_disk="$(dp_root_disk)"
  echo "Detected physical disks (the OS disk is unavailable for selection):"
  lsblk -dpno NAME,SIZE,FSTYPE,MOUNTPOINTS,MODEL,TRAN | while read -r line; do
    if [[ "$line" == "$root_disk "* || "$line" == "$root_disk" ]]; then
      echo "  OS:        $line"
    else
      echo "  candidate: $line"
    fi
  done
}

dp_validate_candidate() {
  local device="$1" root_disk
  root_disk="$(dp_root_disk)"
  [[ -b "$device" ]] || dp_die "$device is not a block device"
  [[ "$device" != "$root_disk" ]] || dp_die "Refusing to select the OS disk"
  case "$device" in /dev/*) ;; *) dp_die "Use an explicit /dev/... device path" ;; esac
}

dp_partition_for_device() {
  local device="$1" child_count child
  child_count="$(lsblk -lnpo NAME,TYPE "$device" | awk '$2=="part" {n++} END {print n+0}')"
  if (( child_count == 1 )); then
    child="$(lsblk -lnpo NAME,TYPE "$device" | awk '$2=="part" {print $1}')"
    echo "$child"
  elif (( child_count == 0 )); then
    echo "$device"
  else
    dp_die "$device has multiple partitions; prepare it manually or select a single-partition disk"
  fi
}

dp_mount_by_uuid() {
  local source="$1" target="$2" timeout_seconds="$3" uuid fstype existing
  uuid="$(blkid -s UUID -o value "$source")"
  fstype="$(blkid -s TYPE -o value "$source")"
  [[ -n "$uuid" ]] || dp_die "No filesystem UUID found on $source"
  [[ "$fstype" == ext4 ]] || dp_die "$source uses $fstype; David-Pi requires ext4"
  install -d -m 0750 "$target"
  existing="$(findmnt -n -o SOURCE "$target" 2>/dev/null || true)"
  if [[ -n "$existing" && "$existing" != "$source" ]]; then
    dp_die "$target is already mounted from $existing"
  fi
  if ! grep -qE "^[^#]*UUID=${uuid}[[:space:]]+${target}[[:space:]]" /etc/fstab; then
    dp_backup_file /etc/fstab
    printf 'UUID=%s %s ext4 defaults,noatime,nofail,x-systemd.device-timeout=%ss 0 2\n' "$uuid" "$target" "$timeout_seconds" >> /etc/fstab
  fi
  mountpoint -q "$target" || mount "$target"
  [[ "$(findmnt -n -o UUID "$target")" == "$uuid" ]] || dp_die "UUID verification failed for $target"
  echo "$uuid"
}

dp_select_existing_disk() {
  local purpose="$1" target="$2" answer device source uuid
  dp_list_storage >&2
  read -r -p "Enter the whole-disk device for $purpose (example /dev/sda), or 'none': " answer
  [[ "$answer" != none ]] || { echo none; return 0; }
  device="$(readlink -f "$answer")"
  dp_validate_candidate "$device"
  source="$(dp_partition_for_device "$device")"
  if [[ -z "$(blkid -s TYPE -o value "$source")" ]]; then
    dp_die "$source has no filesystem. Run 'sudo david-pi prepare-storage' separately, then resume setup."
  fi
  uuid="$(dp_mount_by_uuid "$source" "$target" 30)"
  printf '%s|%s|%s\n' "$device" "$source" "$uuid"
}

dp_prepare_storage() {
  dp_require_root
  local device confirm model size
  dp_list_storage
  read -r -p "Whole disk to ERASE and prepare as ext4: " device
  device="$(readlink -f "$device")"
  dp_validate_candidate "$device"
  model="$(lsblk -dno MODEL "$device" | xargs)"
  size="$(lsblk -dno SIZE "$device" | xargs)"
  echo "WARNING: this permanently erases $device ($model, $size)."
  read -r -p "Type ERASE $(basename "$device") to continue: " confirm
  [[ "$confirm" == "ERASE $(basename "$device")" ]] || dp_die "Confirmation did not match; nothing changed"
  if lsblk -nrpo MOUNTPOINTS "$device" | grep -q '/'; then
    dp_die "A filesystem on $device is mounted; unmount it deliberately before preparing"
  fi
  wipefs --all "$device"
  parted --script "$device" mklabel gpt mkpart david-pi ext4 1MiB 100%
  partprobe "$device"
  udevadm settle
  local partition
  partition="$(lsblk -lnpo NAME,TYPE "$device" | awk '$2=="part" {print $1; exit}')"
  [[ -b "$partition" ]] || dp_die "The new partition did not appear"
  mkfs.ext4 -L DAVID_PI "$partition"
  dp_log "Prepared $partition as ext4; no mount configuration was changed"
}
