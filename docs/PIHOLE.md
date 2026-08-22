# Pi-hole setup

Pi-hole is optional. The installer exposes DNS on port 53 and binds its web
administration page only to host loopback port 8081. The portal receives only a
sanitized aggregate snapshot; it does not mount the complete Pi-hole data
directory.

The default list is HaGeZi Light, selected for broad household ad/tracker
coverage with a relatively low risk of breaking ordinary sites. The installer
backs up `gravity.db` before changing the enabled adlist and then rebuilds
gravity. It does not print query history, client addresses, or domains.

Before changing router or Tailscale DNS, verify directly:

```bash
dig example.com @PI_LAN_ADDRESS
dig doubleclick.net @PI_LAN_ADDRESS
docker exec pihole pihole status
```

Then change one test device first. A public "secondary DNS" is not a true
failover on many clients: clients may use it at any time and bypass blocking.
For deterministic filtering plus DNS redundancy, operate a second independent
Pi-hole. If availability is more important than complete blocking, a public
secondary is an explicit tradeoff.

Router and Tailscale DNS changes are manual because their interfaces and
failure behavior are outside the Pi. Record the old values before changing
them. Never configure Pi-hole as its own upstream resolver.

