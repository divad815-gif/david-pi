# Network boundary

Tailscale is the household access boundary. Tailscale Serve proxies the private
HTTPS name to the portal's loopback listener at `127.0.0.1:8090`. Funnel must
remain disabled. The portal should not publish a LAN-facing web port.

Pi-hole supplies filtered DNS. SSH, Docker, Tailscale administration, and
Pi-hole administration are technical controls and are not granted by ordinary
website use.

