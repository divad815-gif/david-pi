# Device-backup housekeeping worker

`device-backup-worker` is the single production owner for retiring abandoned
phone-upload staging sessions and, when explicitly configured, verifying
content-addressed copies on a second storage device. It has no network and runs
as UID/GID `10001:10001`. The web containers keep in-process housekeeping
disabled so two processes cannot race the same upload or copy state.

The primary `/data` bind, database, incoming directory, originals directory,
storage sentinel, runtime directory, singleton lock, and (when enabled) every
secondary-storage identity are pinned at activation and revalidated every
cycle. Health remains unready until one complete cycle succeeds. A missing or
replaced mount, sentinel mismatch, permission failure, lease loss, or stalled
large-file hash fails health rather than reporting an idle worker.

Secondary verification is off when both `DAVID_PI_SECONDARY_BACKUP_ROOT` and
`DAVID_PI_SECONDARY_DATA_ID` are empty. Enabling it requires all three of these
deployment values together:

- `DAVID_PI_SECONDARY_BACKUP_HOST_ROOT` names an existing host mount that is
  physically independent of `/srv/data/family-photos`;
- `DAVID_PI_SECONDARY_BACKUP_ROOT=/secondary` selects the fixed container bind;
- `DAVID_PI_SECONDARY_DATA_ID` exactly matches the text in
  `/secondary/.david-pi-secondary-storage`.

The host mount must already contain a writable `originals` directory and must
be visible on a different device from primary storage. Compose uses
`create_host_path: false`; neither Compose nor the worker creates, repairs,
formats, mounts, deletes, or relabels storage. The default secondary bind points
at primary only so an unconfigured stack remains structurally valid, and the
worker rejects that bind if secondary verification is accidentally enabled.

The worker is part of the exact six-writer lifecycle: startup readiness,
status, snapshot quiescence/restart, promotion, and maintenance preparation all
include it. The first-rollout legacy image lacks this module, so checked
rollback stops both this worker and `slideshow-worker`, leaves their state and
saved content intact, and starts only the four legacy writers.

This source change does not mount a drive or activate the worker on David-Pi.
Production enablement still requires change-council review of the resolved host
mount, sentinel, ownership, free space, writer-transition lock, backup canary,
and isolated restore evidence.
