# Portal restart verification

`verify_portal_recovery.py` is read-only by default. It verifies that the fixed
`family-photo-portal` container belongs to the `photo-portal` Compose service,
uses `unless-stopped`, has a healthcheck, and publishes only on
`127.0.0.1:8090`.

After installing the source change in an approved maintenance window, run the
inspection first:

```text
/usr/local/sbin/verify-portal-recovery
```

The failure injection is deliberately separate. It requires a healthy starting
state and exact acknowledgement:

```text
DAVID_PI_ALLOW_PORTAL_RECOVERY_EXERCISE=YES \
  /usr/local/sbin/verify-portal-recovery --exercise
```

The exercise sends `SIGKILL` to PID 1 inside the fixed portal container, then
requires the same image to return `healthy` with a higher restart count within
90 seconds. It does not use an operator `docker stop`/`docker kill`, because
those suppress Docker restart policy. On timeout it makes one best-effort
`docker start` rollback attempt and exits nonzero. Verify `/ready`, Tailscale
HTTPS, both approved identities, and database integrity before closing the
window. Never combine this exercise with a schema, dependency, firewall, SSH,
Tailscale, kernel, or backup-retention change.
