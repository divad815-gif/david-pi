#!/bin/sh
set -eu
rm -f /etc/ssh/sshd_config.d/99-david-pi-hardening.conf
/usr/sbin/sshd -t
systemctl reload ssh
