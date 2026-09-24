# Content-free Healthchecks.io templates

These templates send only an empty HTTP `POST`. They never send host status,
user identity, filenames, logs, or exception text. The ping URL is a credential:
keep one URL per line in `/etc/david-pi/healthchecks/<check>.url`, owned by root
with mode `0600`. Never place a ping URL in Compose, a unit `Environment=`, a
command line, source control, or logs.

Install the Python helper as `/usr/local/sbin/david-pi-healthcheck-ping` and the
three unit templates under `/etc/systemd/system/`. Each template uses systemd
`LoadCredential=` and passes only its protected credential-file path through
`DAVID_PI_HEALTHCHECK_URL_FILE`.

Use only the stable, non-sensitive check names already represented by the
source drop-ins:

| Job | Check name |
| --- | --- |
| source/SQLite backup | `backup` |
| independent signed snapshot | `data-backup` |
| append-only B2 upload | `b2-backup` |
| sanitized server-status publication | `server-status` |
| isolated full data restore | `restore-data` |

For example:

```text
systemctl start david-pi-healthcheck-start@backup.service
systemctl start david-pi-healthcheck-success@backup.service
```

The reviewed drop-ins are under `deploy/healthchecks/`. Each wires a start ping,
a success ping, and this failure dependency:

```ini
[Unit]
OnFailure=david-pi-healthcheck-failure@backup.service
```

The leading `-` on start/success commands makes monitoring best-effort, so a
Healthchecks network outage cannot change the underlying job result. Missing or
unsafe URL credentials still make the separate ping unit fail with configuration
status 78; they never generate a fake success. The monitored job's own failure
continues to be authoritative.

`david-pi-restore-data-drill.service` includes the same three lifecycle hooks.
Its restore process runs with `PrivateNetwork=true` and only loopback available;
the separate ping units retain network access, so no monitoring credential enters
the restore namespace.

These are source templates, not an instruction to install them immediately.
Provision each protected URL first, copy only that job's drop-in, reload systemd,
and trigger both a synthetic success and a synthetic failure in its own approved
maintenance window. A failed ping deliberately prints only its state and
exception type; the secret URL is omitted.
