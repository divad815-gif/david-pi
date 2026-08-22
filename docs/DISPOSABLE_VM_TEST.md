# Disposable Windows acceptance test

This procedure validates a public David-Pi release without repartitioning the
Windows host and without touching a real David-Pi installation.

Use the official [Debian installer](https://www.debian.org/distrib/) and the
official [VirtualBox installation guide](https://www.virtualbox.org/manual/ch02.html).
The repeatable VM helper is `tools/windows/Disposable-DavidPiVm.ps1`; run it
with `-Action Plan` before installing or creating anything.

## Test machine

- VirtualBox VM: Debian 13 minimal amd64, systemd, OpenSSH server
- 4 virtual CPUs, 6 GiB RAM
- 20 GiB dynamically allocated OS disk
- separate 4 GiB dynamic primary-data disk
- separate 4 GiB dynamic independent-backup disk
- NAT with Windows TCP `2222` forwarded to guest TCP `22`
- hostname `david-pi-test`, administrator `davidpi-test`
- keep at least 25 GiB free on Windows throughout the test

Use a test-only SSH key and verify two simultaneous sessions through
`ssh -p 2222 davidpi-test@127.0.0.1`. Never reuse household secrets or data.

## Installation choices

Run the exact public one-command installer and select protected storage,
`/dev/sdb` for primary, `/dev/sdc` for backup, Pi-hole enabled, exit node off,
TMDB/GIPHY/Windows Assistant off, and a temporary shutdown password. Blank
disks must be prepared individually with `david-pi prepare-storage`; setup never
formats them.

Temporarily enroll the VM in Tailscale interactively. Do not advertise routes,
change global DNS, or change the household router. Confirm the second SSH
session before accepting SSH hardening.

## Required evidence

Record sanitized command output for installer checksum validation, image
digest, service/container hardening, loopback publication, Serve/Funnel,
distinct storage UUIDs, reboot persistence, backup/restore, repair, fail-closed
missing-primary behavior, Pi-hole health, and all automated test totals. Do not
record login URLs, keys, tokens, personal records, or DNS queries.

## Cleanup

Remove the temporary Tailscale device, then power off and delete the VM, all
three virtual disks and snapshots, the Debian ISO, and the test SSH key.
Uninstall VirtualBox and confirm Windows free space is reclaimed. Preserve only
the sanitized acceptance evidence.
