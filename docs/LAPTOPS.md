# Repurposed laptop hosting

David-Pi can run on a supported 64-bit laptop with at least 4 GB RAM and two
CPU cores. Install Debian or Ubuntu Server directly on the computer; Windows,
WSL, and Docker Desktop are not unattended-server targets.

## Before installation

1. Confirm the battery is not swollen or physically damaged.
2. Clean the cooling vents and confirm the fan works, if one is fitted.
3. Prefer Ethernet. If Wi-Fi is required, verify it reconnects after reboot.
4. In firmware, enable restore-after-AC-loss when the computer supports it.
5. Use an SSD for the active media library where possible.
6. Plan a physically separate backup destination.

## Laptop profile

Preflight detects a battery-backed system as `linux-laptop`. The setup wizard
offers a dedicated-server policy that:

- ignores lid-close events;
- ignores automatic idle suspension;
- masks suspend, hibernate, hybrid-sleep, and sleep targets;
- leaves shutdown and reboot available;
- records the choice in `/etc/david-pi/install-state.env`.

The installer does not restart logind during the SSH session. Reboot after
installation, then verify the laptop remains reachable with its lid closed.

To reverse the policy, use local administrative access to remove
`/etc/systemd/logind.conf.d/60-david-pi-server.conf`, unmask the four sleep
targets, and reboot. Do not reverse it remotely without a tested recovery path.

## Health reporting

Generic Linux temperature comes from kernel thermal and hardware-monitor
interfaces. Battery charge, charging state, and AC availability come from the
Linux power-supply interface. SATA/NVMe/USB transport and SMART health are
reported when the hardware and bridge expose them. Unsupported metrics are
shown as unavailable and do not cause a false Pi-specific warning.

## Operational cautions

- A laptop battery is useful short-outage protection, not an independent data backup.
- Old batteries can fail abruptly; test safe shutdown behavior periodically.
- Consumer laptops may throttle under sustained video or audiobook processing.
- Do not allow the OS to suspend while serving DNS through Pi-hole.
- Test reboot, power loss, Tailscale reconnection, storage mounting, and DNS
  recovery before making the server the household DNS authority.
