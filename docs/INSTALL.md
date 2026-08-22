# Guided installation

The installer supports 64-bit Raspberry Pi, Intel, and AMD hardware running a
supported Debian or Ubuntu Server release. It is intentionally interactive and resumable. It
does not format a disk, publish a public website, or disable password SSH
without a separate, explicit safety step.

## 1. Prepare a supported Linux host

Use Raspberry Pi OS Lite 64-bit, Debian 12/13, or Ubuntu Server 22.04/24.04.
Choose a minimal, 64-bit installation with systemd. Set a unique administrator
username, strong temporary password, hostname, locale, and network connection.
Enable SSH. There is no David-Pi default credential.

Boot the server, sign in once, and update its package metadata:

```bash
sudo apt update
sudo apt full-upgrade
sudo reboot
```

Reconnect after the reboot. Before starting the installer, enroll an SSH
public key and prove it works in a second terminal. Keep the first terminal
open.

## 2. Obtain and verify a release

The recommended bootstrap is one command after the supported OS exists:

```bash
sudo apt-get update &&
sudo apt-get install -y ca-certificates curl &&
curl -fsSL https://github.com/divad815-gif/david-pi/releases/latest/download/install.sh |
sudo bash
```

The bootstrap checks native Linux/systemd, distribution, architecture, memory,
and free space before installation. It downloads `release-manifest.txt` and the
matching archive into a mode-0700 temporary directory, validates the archive
SHA-256 before extraction, and passes the release's immutable GHCR image digest
to setup. Temporary downloads are removed whether setup succeeds or fails.

People who do not want to pipe a remote script into `sudo` should download
`install.sh` and `install.sh.sha256`, run `sha256sum -c`, inspect the script, and
then execute `sudo bash install.sh`.

`david-pi preflight` is read-only. `setup` records completed phases in
`/etc/david-pi/install-state.env`; rerunning it resumes after the last completed
phase.

## 3. Answer the wizard

The wizard asks for:

- the existing Linux administrator, hostname, and user-visible instance name;
- confirmation of detected Pi, laptop, or generic-server capabilities;
- permission to apply dedicated-server lid/suspend policy when a laptop is detected;
- a storage profile: OS-disk trial, dedicated primary, dedicated primary plus a
  physically separate backup, or restore;
- optional Pi-hole, exit-node advertisement, TMDB, GIF search, and Windows
  Assistant bridge features;
- a private website safe-shutdown password, which is stored only as a hash.

Container memory and CPU limits are generated from detected host resources,
while reserving capacity for Linux, SSH, Tailscale, Pi-hole, and filesystem
caching. Disk discovery prints device, capacity, filesystem, model, transport, and
mount information. Confirm model and capacity physically before selecting a
device. Existing data disks must already be ext4. A disk without a filesystem
is rejected, with instructions to run the separate destructive command:

```bash
sudo david-pi prepare-storage
```

That command refuses the OS disk and mounted disks, and requires typing the
exact device confirmation. It is never called automatically by `setup`.

The production Compose file pulls the exact image digest recorded in the
release manifest. It does not build on the server. Developers can build from a
checked-out source tree with the separate `compose.dev.yaml` override.

## 4. Tailscale and SSH safety gates

Tailscale displays its own private login URL. Approve the Pi in the intended
tailnet. The installer configures private Serve to `127.0.0.1:8090`, verifies
Funnel is off, and never edits the tailnet ACL/grants document.

Before password SSH is disabled, the installer requires at least one enrolled
public key and asks you to prove a second key-authenticated session is open. A
five-minute automatic rollback is armed before reloading SSH. Failed syntax or
failed confirmation restores the prior configuration.

## 5. Verification

At completion run:

```bash
sudo david-pi status
sudo david-pi verify
sudo david-pi backup
sudo david-pi restore-test
```

Verification checks the storage mount and sentinel, container health and UID,
zero effective capabilities, read-only root, loopback-only publication,
Tailscale Serve/Funnel state, Pi-hole binding when selected, physical backup
independence, absence of `.env` in the image, and absence of the Docker socket.

The full installation report is root-readable at
`/etc/david-pi/INSTALLATION_REPORT.md`.

## What remains manual

- Tailnet ACL/grants policy and device/user membership
- Router DNS, port-forwarding, UPnP, DMZ, and inbound IPv6 review
- A LAN-only test with Tailscale disconnected
- API account creation and token rotation
- A real restore drill
- UPS shutdown integration specific to the UPS model

## Supported-host boundaries

- Native Linux with systemd is required.
- `amd64/x86_64` and `arm64/aarch64` are supported.
- Windows, WSL, Docker Desktop, macOS, 32-bit systems, and non-systemd Linux
  distributions are not production targets in this release.
- The installer accepts internal SATA/NVMe disks and external USB disks. The
  same UUID, sentinel, and physical-backup-independence checks apply to both.
- The active data filesystem remains ext4 for predictable Linux permissions,
  atomic replacement, and recovery behavior.
