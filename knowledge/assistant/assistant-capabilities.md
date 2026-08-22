# Assistant capabilities and limits

David-Pi answers allowlisted household and server-health questions with
deterministic handlers. It cannot execute commands, change files, control Docker,
restart services, administer Tailscale or Pi-hole, or reveal browsing history.

Questions outside that deterministic pool use the authenticated Windows helper
when it is online. A future Ubuntu broker can replace or supplement Windows
without changing the portal contract. No generative model runs on the Raspberry Pi.
