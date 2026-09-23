# Isolated installation acceptance

Install QEMU, `qemu-utils` and `cloud-image-utils` from the host distribution. Download Ubuntu 24.04 AMD64 from [Ubuntu cloud images](https://cloud-images.ubuntu.com/noble/current/) and Debian 13 AMD64 from [Debian cloud images](https://cloud.debian.org/images/cloud/trixie/latest/). Check each downloaded image against its publisher's checksum document, then calculate its SHA-256 for the harness. Retain the downloaded checksum document.

```sh
python3 tools/vm/vm.py prepare --directory work/vm/ubuntu --image work/vm/images/noble-server-cloudimg-amd64.img --sha256 VERIFIED_SHA256 --port 22221
python3 tools/vm/vm.py start --directory work/vm/ubuntu
python3 tools/vm/vm.py ssh --directory work/vm/ubuntu -- 'cloud-init status --wait && cat /etc/os-release'
```

A separate Debian guest uses port 22222. Each guest gets a disposable SSH key, 32 GiB OS overlay, two separate 12 GiB virtual disks, 4 GiB memory, and NAT with SSH bound to host loopback. No host folders, real disks, SSH agent, or production credentials are exposed. KVM is used when available; otherwise full software emulation is selected. The guests can download prerequisites through NAT. Use only a dedicated test Tailscale network for end-to-end enrollment.

Transfer the candidate source archive with `scp` using the generated key and port (see `vm.json`); do not copy the live server's database, configuration or keys. Run the documented setup inside the guest. Prepare only `/dev/vdb` and `/dev/vdc` as ext4 test disks. Confirm `lsblk` first. This destructive preparation is safe only inside these disposable guests, never on the host or live server.

For each supported OS, record: OS version, source digest, image identity, installer transcript, selected options, fresh install, interrupted/repeated setup, reboot, removal of the data virtual disk while powered off, full-filesystem rejection, update interruption, and clean restoration into another fresh VM. An ext4 backup disk in a VM is independent from the virtual primary disk for application tests; it does not prove physical disk-failure protection.

For the v9.22.2 upgrade, obtain its public source/image and create synthetic notes, recipes, watchlist, two household users, file and media fixtures. Back them up, upgrade the fixture installation, and verify those exact records and byte hashes. Never use household data as upgrade fixtures. Test incompatible schema rollback rejection and newer-content protection.

`stop` requests a guest shutdown. Inspect the guest PID before manual cleanup; the harness will not kill arbitrary PIDs or delete VM folders. A successful boot is **not** an installation acceptance pass. Real Tailscale/browser setup, device behavior, recovery verification and newcomer execution have separate release receipts.
