# David-Pi

A private household server for photos, videos, files, notes, recipes, watchlists,
audiobooks, games, household chat and Android phone backup. Use your own website
name—such as **John’s home**—and a Tailscale hostname such as **john-pi**.

**Portable release 10.0.0 is in development. It is not published or ready for
household installation yet.** Publication is blocked until the VM, restore,
Android, Raspberry Pi and newcomer checks pass. Existing GitHub downloads may
belong to the older installer. See [release status](docs/RELEASE.md).

![Setup flow: machine, Tailscale, administrator, name and storage, modules](docs/images/setup-flow.svg)

## What you need

- Raspberry Pi 4/5 running 64-bit Raspberry Pi OS based on Debian 13, or an
  Intel/AMD PC running Debian 13 or Ubuntu Server 24.04 LTS.
- At least 4 GB memory, two CPU cores and 8 GiB free OS space; additional space
  for household content. Ethernet is recommended.
- A Tailscale account and Tailscale on the phone/computer used for setup.
- An existing folder on a local ext4 filesystem or a prepared local ext4 drive.
  A separate backup drive is recommended and can be added later.

Each member uses their own Tailscale identity and must be admitted to the portal.
Tailscale is required. Movie search, online recipes, browser notifications and
Pi-hole are optional. A manual movie watchlist and local recipes work without
provider accounts. No streaming-service passwords are needed.

## Install after the stable release is available

Open a terminal on the server, then run:

```sh
curl -fsSL https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh | sudo bash
```

The command verifies release files, checks the machine, connects Tailscale and
prints a private setup link. Open it on a device signed into your Tailscale
network. Follow the wizard to claim ownership, choose the name and storage,
select modules, skip or configure integrations, and verify the installation.
If `curl` is unavailable, install it and `ca-certificates` using your operating
system’s package manager first. [Read the complete installation guide](docs/INSTALL.md)
for inspection before execution and the first-boot checklist.

## Guides

- [Tailscale and private access](docs/TAILSCALE.md)
- [Storage and prepared drives](docs/STORAGE.md)
- [Household accounts and naming](docs/HOUSEHOLD.md)
- [Optional services and API keys](docs/INTEGRATIONS.md)
- [Android companion](docs/ANDROID.md)
- [Backup and restore](docs/RECOVERY.md)
- [Updates](docs/UPDATES.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Developer VM testing](tools/vm/README.md) · [Release gates](docs/RELEASE.md)

Production uses verified image digests, never a source build on the server. The
portal is unprivileged and cannot run arbitrary system commands. Uninstalling
the application preserves content by default. A local recovery snapshot before
an update is not a substitute for an independent backup.

Licensed under [AGPL-3.0](LICENSE). The original Git history is preserved.
