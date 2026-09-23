# David-Pi maintenance worker

The portal no longer owns production housekeeping timers. `compose.yaml`
defines one `david-pi-maintenance` service from the exact portal release image.
It has no network, ports, or secrets, drops every capability, uses a read-only
root filesystem, and runs as dedicated UID 10002 in the portal's shared group.

The protected family tree is mounted at `/data` read-only. The only persistent
write surface is the pre-created
`/data/.david-pi-operations/maintenance` directory, mounted a second time at
`/maintenance-state` read-write and owned by UID 10002. The worker verifies
that the read-only anchor and writable mount are the same directory inode before
locking or writing. The portal receives only group-readable, read-only access
to the resulting aggregate metrics database.

Before Compose starts, `david-pi-portal-lifecycle` takes the exclusive lock on
the pinned, root-owned `0700` `/run/david-pi-writer-transition` directory and
holds it across stop, preparation, start, and bounded readiness. The independent
backup takes the same lock for its entire copy/quiesce/restart/readiness run, so
startup, shutdown, and backup cannot interleave writer state transitions.
Once Compose startup is attempted, any command error or HUP/INT/TERM before the
readiness sequence completes triggers a synchronous `docker compose down` while
that same lock is still held. A partially started or unready stack is therefore
not left running when the lifecycle wrapper reports failure.
Systemd creates and preserves that private runtime directory for both units.
The lifecycle wrapper stops all Compose writers and the root-run
`david-pi-prepare-maintenance-state` helper independently verifies that all six
known writers are stopped. Despite its retained deployment filename, that
helper now prepares both maintenance state and the three isolated audiobook
worker directories. It pins every directory component without following
symlinks and validates the sentinel, inode, device, and `statx` mount identity
before every creation or metadata transition. Install the lifecycle,
preparation, and `david-pi-writer-readiness` helpers in `/usr/local/sbin` with
mode `0755` alongside the unit. The latter is the shared bounded startup/recovery
gate: the portal, maintenance, slideshow, and device-backup containers must
report exactly `healthy`, while the notifier and audiobook preparer must remain
running with stable start
identities.

Preparation is deliberately create-only. `/`, `/srv`, `/srv/data`, and the
family storage root must have the reviewed root ownership and exact modes in
the helper; the only reviewed mount transition is `/srv/data`. The sentinel
must be a singly linked root-owned `0644` regular file. Existing operations and
maintenance directories must already be exactly `root:root 0755` and
`10002:10001 0750`. Drift fails startup without chmod/chown repair. A missing
operations directory is created at its final metadata. A missing maintenance
directory is created as an empty payload beneath a fresh, cryptographically
unpredictable root-owned `0700` transaction directory at the pinned family-tree
root—not beneath the independently replaceable operations name. Ownership and
mode are set and fsynced while the payload is private. It is then published at
the final name with `renameat2(RENAME_NOREPLACE)`; no metadata-changing syscall
ever runs on an inode found at that final name. Path and mount identity are
revalidated around every step. If the final name appears concurrently,
publication fails without replacing or repairing it.

A process or host crash before publication can leave only an unreferenced
`.maintenance-prepare-v2-*` transaction. Ordinary preparation failures retain
the same transaction evidence, and a successful first publication retains its
empty transaction envelope. The helper never path-deletes these directories:
even after an identity check, a same-name replacement could win the interval
before `rmdir(2)`. Retries neither trust, open, clean, nor adopt old transaction
names; they use a new unpredictable name and can complete.
A crash during atomic publication leaves either no final name or the completely
configured final directory. A crash after publication is therefore idempotent.
Old root-owned `0700` objects at the public `maintenance` name (including any
artifact from an earlier helper version) remain fail-closed and require an
explicit operator recovery review; they are never silently claimed as
provenance. Stale private transaction cleanup is likewise a separate reviewed
operator action, not an automatic deletion path.

The helper itself must run as effective `root:root`. The host must provide
`statx` mount IDs and `renameat2(RENAME_NOREPLACE)`; an `openat2`-less
kernel is accepted only because the independent mount-ID check preserves the
no-cross-mount invariant.

Audiobook preparation is limited to exactly three rebuildable write surfaces:
`.david-pi-operations/audiobook`, `audiobooks/streaming`, and
`audiobooks/incoming/streaming`. Each must be `10001:10001 0750`. The existing
operations root must remain `root:root 0755`; the existing audiobook library,
`originals`, and `incoming` directories must remain `10001:10001 0755`. Those
parents are deployment preconditions, not repair targets. The helper pins and
metadata-validates `originals` but never lists it, opens a child, changes it, or
creates it. A missing parent or any drift fails startup.

Each missing audiobook target is created at final metadata under its own fresh,
unpredictable root-owned `0700` transaction at the pinned family-tree root and
is published with `renameat2(RENAME_NOREPLACE)`. Existing targets are only
validated; their contents are not inspected or changed. A concurrent final
name is never replaced. The three publications cannot be one filesystem
transaction, so a crash may leave a strict prefix of fully configured targets
plus private transaction evidence. That state is safe and retryable: Compose
has not started, already complete targets validate idempotently, and remaining
targets use new transactions. Transactions are retained rather than path-
deleted for the same replacement-race reason as maintenance transactions.

Activation requires a maintenance window with the writer-transition lock held,
all six known containers stopped, the trusted storage and exact parent
metadata verified, and enough free inodes for up to three payloads and their
retained transaction envelopes. If existing target metadata differs, stop for
operator review; do not chmod, chown, delete, or move it automatically.

The singleton is an advisory lock on the pinned operations-directory inode, so
replacing `worker.lock` cannot start a second worker. The lock, metrics, status,
heartbeat, sentinel, database, and traversed-directory objects are opened
without following symlinks and have their device/inode identity revalidated.
Regular worker files must be singly linked and owned by the maintenance UID.

Only aggregate system-metric collection is enabled. Collector timestamps must
be timezone-aware, no more than 15 minutes old, and no more than five seconds in
the future; the recorded sample retains that validated collector timestamp.
Each task is independently scheduled and failure-isolated. A failed task marks
the heartbeat degraded, which deliberately fails the container health check.

The 15-second heartbeat is written only to private tmpfs, and its configured
maximum interval is 30 seconds to fit the 90-second health window. Persistent
metadata-only status is written only when task state/results change or every 15
minutes. Neither document contains paths, filenames, identities, content,
secrets, or raw exception messages.

Photo, note, and generic upload-part retention accept only `off` or read-only
`preview`; all three are `off` in Compose and there is no apply/delete code.
The Photos and Notes UI therefore promises no automatic 30-day deletion.
Secondary-copy mutation belongs only to the dedicated device-backup worker;
the maintenance worker never performs it. Backups quiesce all six known writers
and use the same bounded readiness helper after restart.

This source change does not deploy or touch the live Pi. Activation still
requires the protected deployment, verification, and rollback gate.
