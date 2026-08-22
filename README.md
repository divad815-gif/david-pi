# David-Pi

David-Pi is a private household server for Raspberry Pi and repurposed 64-bit
Linux computers. It combines
photos and videos, files, notes, recipes, movies and TV, games, Date Night,
audiobooks, household chat, phone backup, and server health behind private
Tailscale HTTPS.

The public project contains no household data, API keys, Tailscale identity,
SSH keys, passwords, databases, or signing keys.

## Supported hosts

- Raspberry Pi 4 or Raspberry Pi 5 (`arm64`)
- 64-bit Intel/AMD desktops, mini PCs, and laptops (`amd64`)
- Raspberry Pi OS Lite / Debian 12 or 13
- Ubuntu Server 22.04 or 24.04 LTS
- At least 4 GB RAM, two CPU cores, and 8 GiB free on the OS disk
- Ethernet recommended
- Optional dedicated ext4 primary data disk, internal or external
- Optional physically separate ext4 backup disk

## Before running the installer

For a Pi, use Raspberry Pi Imager. For a repurposed computer, install a minimal
supported Debian or Ubuntu Server image. Then:

1. Choose a hostname such as `david-pi`.
2. Create your own administrator username and a strong temporary password.
3. Configure Wi-Fi only if Ethernet will not be used.
4. Enable SSH.
5. Boot the server and connect once with SSH.

David-Pi intentionally has no shared default SSH username or password.

## Install from a release

After installing a supported native Linux OS, run the public, checksum-verifying
bootstrap:

```bash
sudo apt-get update &&
sudo apt-get install -y ca-certificates curl &&
curl -fsSL https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh |
sudo bash
```

To inspect the exact installer before giving it root access:

```bash
curl -fsSLO https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh
curl -fsSLO https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh.sha256
sha256sum -c install.sh.sha256
less install.sh
sudo bash install.sh
```

The bootstrap validates the host, downloads the release archive and manifest to
a private temporary directory, verifies SHA-256 before extraction, pins the
published multi-architecture container by digest, starts the resumable wizard,
and removes temporary downloads. It rejects Windows, WSL, unsupported Linux
releases, unsupported CPU architectures, and insufficient resources.

For source development, use `compose.dev.yaml`; production never compiles the
application on the target host.

The wizard detects Pi, laptop, or generic-server hardware, sizes container
limits from available resources, performs preflight checks, protects SSH access, discovers storage,
installs the portal, configures private Tailscale Serve, offers Pi-hole and
TMDB setup, configures backups, and runs final verification.

## Management commands

```text
sudo david-pi setup
sudo david-pi preflight
sudo david-pi status
sudo david-pi verify
sudo david-pi update --from RELEASE.tgz --sha256 HASH --image GHCR_IMAGE@sha256:DIGEST
sudo david-pi backup
sudo david-pi restore-test
sudo david-pi repair
sudo david-pi support-bundle
sudo david-pi uninstall-app
```

`uninstall-app` does not erase `/srv/data` or a configured backup disk.

## Safety principles

- The portal binds only to `127.0.0.1:8090`.
- Tailscale Serve supplies private HTTPS; Funnel remains disabled.
- Storage is mounted by UUID and verified with a sentinel.
- The portal fails closed when storage is missing.
- Disk formatting is never automatic and requires a separate typed confirmation.
- Password SSH is not disabled until a second key-authenticated session is confirmed.
- API keys and notification credentials never enter source or Docker image layers.
- A backup is called independent only when it is on another physical device.
- Laptop lid/suspend protection is explicit and is never applied to a non-laptop automatically.
- Pi-only readings become unavailable on generic Linux instead of breaking health collection.

See [docs/INSTALL.md](docs/INSTALL.md), [docs/TAILSCALE.md](docs/TAILSCALE.md),
[docs/PIHOLE.md](docs/PIHOLE.md), [docs/LAPTOPS.md](docs/LAPTOPS.md), and
[docs/RECOVERY.md](docs/RECOVERY.md).
