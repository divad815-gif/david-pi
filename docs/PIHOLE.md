# Optional Pi-hole integration

Skip Pi-hole to keep the portal independent of household DNS. Enabling the
module and changing router DNS are separate decisions.

For an existing local supported Pi-hole instance, run `sudo david-pi pihole-setup`
and enter the absolute host path of its `pihole-FTL.db` file. With Docker, use
the database inside its persistent host volume. The integration reads aggregate
statistics only; it does not change DNS, query records or the Pi-hole database.

For a new instance, install Pi-hole separately and review
[Pi-hole’s official requirements](https://docs.pi-hole.net/main/prerequisites/).
Do not overwrite an existing service occupying DNS port 53 or a management
port. Keep the management interface private. Read the default blocking-list
choice in the guided step before applying it; retain existing configuration
when connecting an already configured Pi-hole.

Before changing router or Tailscale DNS, test DNS directly against the new
instance on one device. Record that device's old DNS values and confirm normal
websites still resolve. Only then change additional clients deliberately.
If resolution fails, restore the previous DNS values first and diagnose Pi-hole.

A public secondary DNS can bypass blocking because many clients use either
resolver at any time. For filtering with redundancy, use a second independent
working Pi-hole. Never configure Pi-hole as its own upstream resolver.
