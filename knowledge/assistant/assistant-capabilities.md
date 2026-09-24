# Assistant capabilities and limits

David-Pi answers allowlisted household and server-health questions with
deterministic handlers. It cannot execute commands, change files, control Docker,
restart services, administer Tailscale or Pi-hole, or reveal browsing history.

Questions outside that deterministic pool can use the authenticated Windows helper
when it is explicitly enabled and online. The production default is disabled because
the Pi bridge labels requests as `coding_readonly` but cannot itself enforce the
external broker's tool permissions. Enable it only after a live broker canary proves
command, write, and tool capabilities are rejected in that mode. A future Ubuntu
broker can replace or supplement Windows without changing the portal contract. No
generative model runs on the Raspberry Pi.
