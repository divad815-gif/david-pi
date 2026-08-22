# Private Tailscale access

David-Pi is designed to be private. Docker publishes the portal only on
`127.0.0.1:8090`; Tailscale Serve supplies HTTPS. Funnel must remain disabled.
Router port forwarding is neither required nor desired, and carrier-grade NAT
does not prevent private tailnet access.

The installer configures local Serve state but cannot safely invent your
tailnet users, groups, tags, or administrative devices. Copy
`deploy/tailscale-policy-example.hujson`, replace every placeholder, and review
it in the Tailscale policy editor. The example separates:

- household HTTPS access;
- administrator-only SSH and Pi-hole administration;
- household DNS use.

Do not apply the example unchanged. Confirm the policy using Tailscale's policy
tests and keep an existing administrative session open while changing access.

Useful local verification:

```bash
tailscale status
tailscale serve status
tailscale funnel status
sudo david-pi verify
```

From a LAN device with Tailscale fully disconnected, the Pi's LAN address on
port 80 must fail. From an approved Tailscale device, the private HTTPS MagicDNS
name must work. Website membership is not the same as SSH or OS administration.

