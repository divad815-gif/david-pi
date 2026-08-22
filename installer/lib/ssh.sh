#!/usr/bin/env bash
set -Eeuo pipefail
source "${DAVID_PI_CLI_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)}/installer/lib/common.sh"

dp_ssh_prepare() {
  local admin_user="$1" home ssh_dir auth_file key_count confirmed
  home="$(getent passwd "$admin_user" | cut -d: -f6)"
  [[ -n "$home" ]] || dp_die "Cannot determine home directory for $admin_user"
  ssh_dir="$home/.ssh"
  auth_file="$ssh_dir/authorized_keys"
  install -d -m 0700 -o "$admin_user" -g "$admin_user" "$ssh_dir"
  touch "$auth_file"
  chown "$admin_user:$admin_user" "$auth_file"
  chmod 0600 "$auth_file"
  key_count="$(awk '!/^[[:space:]]*(#|$)/ {n++} END {print n+0}' "$auth_file")"
  echo "SSH key count for $admin_user: $key_count"
  if (( key_count == 0 )); then
    echo "Add a public key before hardening SSH. From the client computer use ssh-copy-id or append only the public .pub key."
    dp_die "No administrator public key is enrolled; password SSH will not be disabled"
  fi

  dp_log "Leaving current SSH authentication unchanged until a second key session is verified"
  dp_save_state SSH_HARDENING pending_second_session
  cat <<EOF
Open a SECOND terminal now and connect with the enrolled key:
  ssh $admin_user@$(hostname -I | awk '{print $1}')
Then resume setup and confirm the key session when prompted.
EOF
  dp_yes_no confirmed "Did the second key-authenticated SSH session succeed and remain open?" no
  [[ "$confirmed" == yes ]] || dp_die "SSH hardening was deferred safely; rerun setup after opening the second key session"
  dp_apply_ssh_hardening
}

dp_apply_ssh_hardening() {
  local fragment=/etc/ssh/sshd_config.d/90-david-pi.conf
  local rollback=/run/david-pi-ssh-rollback.sh before=/run/david-pi-ssh-fragment.before existed=/run/david-pi-ssh-fragment.existed
  dp_backup_file "$fragment"
  rm -f "$before" "$existed"
  if [[ -f "$fragment" ]]; then
    cp -a "$fragment" "$before"
    touch "$existed"
  fi
  cat > "$rollback" <<'EOF'
#!/bin/sh
set -eu
if [ -f /run/david-pi-ssh-fragment.existed ]; then
  cp -f /run/david-pi-ssh-fragment.before /etc/ssh/sshd_config.d/90-david-pi.conf
else
  rm -f /etc/ssh/sshd_config.d/90-david-pi.conf
fi
sshd -t && systemctl reload ssh
EOF
  chmod 0700 "$rollback"
  systemd-run --unit=david-pi-ssh-rollback --on-active=5m "$rollback" >/dev/null
  dp_atomic_write "$fragment" 0644 root root <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
X11Forwarding no
EOF
  sshd -t || { systemctl stop david-pi-ssh-rollback.timer 2>/dev/null || true; "$rollback"; dp_die "New SSH configuration failed validation and was restored"; }
  systemctl reload ssh
  echo "SSH reloaded. Confirm the existing second session still works."
  local confirmed
  dp_yes_no confirmed "Is the verified second SSH session still connected?" no
  if [[ "$confirmed" == yes ]]; then
    systemctl stop david-pi-ssh-rollback.timer 2>/dev/null || true
    systemctl reset-failed david-pi-ssh-rollback.service 2>/dev/null || true
    rm -f "$rollback" "$before" "$existed"
    dp_save_state SSH_HARDENING verified_key_only
    dp_log "SSH key-only configuration confirmed"
  else
    "$rollback"
    systemctl stop david-pi-ssh-rollback.timer 2>/dev/null || true
    rm -f "$rollback" "$before" "$existed"
    dp_die "SSH confirmation failed; previous settings restored"
  fi
}
