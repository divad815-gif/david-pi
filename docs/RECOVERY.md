# Recovery and rollback

## Application rollback

Release updates preserve `/srv/data` and record changed configuration beneath
`/etc/david-pi/rollback`. Keep the previous release archive and Docker image
until verification and smoke tests pass.

```bash
sudo systemctl stop david-pi-portal.service
cd /srv/compose/photo-portal
sudo docker compose down
# Restore the prior source/Compose release, then:
sudo docker compose up -d --no-build
sudo systemctl start david-pi-portal.service
sudo david-pi verify
```

## SSH recovery

SSH hardening arms a five-minute systemd rollback before reload. Keep two
sessions open until it is confirmed. If both remote sessions are lost, use a
local keyboard/display or mount the OS card on a trusted Linux computer and
restore the saved fragment. David-Pi deliberately does not include a web route
that generates administrator keys or runs arbitrary host commands.

## Storage failure

The host can boot without its external disk, but the portal fails closed. Do
not create a substitute `/srv/data/family-photos` tree on the OS card. Inspect
the disk, cable, enclosure, power, UUID, and filesystem before restarting the
portal. Never run filesystem repair against a mounted filesystem.

## Restore testing

`sudo david-pi restore-test` reads the latest source/database backup in an
isolated temporary directory, verifies the archive, and runs SQLite
`PRAGMA quick_check` against backup copies only. It does not start an isolated
application or modify production. A complete recovery test also needs a
physically independent media/data snapshot and a planned maintenance window.

## Support bundle

```bash
sudo david-pi support-bundle
```

The bundle contains bounded, sanitized system/service metadata. Review it
before sharing. It excludes environment files, credentials, private records,
filenames, DNS history, and command access.

