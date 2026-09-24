# David-Pi backup and recovery runbook

This directory contains a source-only recovery design. Do not install or enable
it until the corrected implementation has passed its council re-review, the
intended 4 TB disk is physically present and positively identified, and a
disposable full restore has passed. None of these files authorizes formatting,
pruning, repurposing, or deleting existing storage.

## Non-negotiable safety rules

- Preserve the current independent backup and all saved application content.
- Keep local and remote pruning disabled. The Pi has no retention/delete
  credential and the upload script contains no `forget` or `prune` operation.
- Identify any new disk by serial, device path, filesystem UUID, and capacity.
  If partitioning or formatting is needed, stop for explicit approval naming
  that exact device.
- Never place manifest, B2, or restic secrets in Compose, Git, a release image,
  a snapshot, a command line, or a report.
- Never restore over `/`, `/home`, `/srv`, `/srv/data`, `/srv/backup-data`, the
  source snapshot, or any ancestor/descendant of the source snapshot.
- A copied or hash-verified restore is not automatically a boot-verified or
  disaster-recovery-complete restore.

## Local snapshot lifecycle

The data-backup service requires the source and backup mounts, validates the
source and backup sentinels byte-for-byte, validates the configured backup UUID,
and proves that source and destination differ by both filesystem UUID and
`st_dev`. It retains open source, backup-root, and working-snapshot directory
descriptors for the whole run. The inode, mount ID, owner, mode, UUID, device,
`st_dev`, and exact sentinels are rechecked before every mutating stage, so a
same-filesystem directory replacement with a copied sentinel fails. The service
also takes its non-blocking whole-run lock in a private `0700` runtime and the
shared writer-transition lock. Systemd creates the shared, root-owned `0700`
`/run/david-pi-writer-transition` directory for both the backup and portal
units. Backup holds that pinned-directory lock across every copy, writer stop,
restart, and bounded-readiness step. Portal start/stop uses the same lock, so
Compose cannot start or stop inside a quiesced backup pass.

1. While applications remain online, make an optimization pass over the data
   tree and recovery configuration into a timestamped `.incomplete-*`
   directory. The internal copier performs every destination traversal through
   retained directory descriptors and
   `openat2(RESOLVE_BENEATH|RESOLVE_NO_MAGICLINKS|RESOLVE_NO_SYMLINKS|RESOLVE_NO_XDEV)`.
   It never invokes rsync or a pathname-based `--delete`. A changing online
   source invalidates this pass; the quiesced pass rebuilds it from scratch.
2. Stop every known writer that was running and confirm it is stopped.
   The reviewed set is the portal, chat notifier, audiobook preparer,
   device-backup worker, slideshow worker, and maintenance worker. The two
   queue-owning workers are included even when idle because they own durable
   upload/copy or slideshow leases and publication intents.
3. Perform the final data/configuration pass and use SQLite's backup API for
   each database. Every copied database must pass `quick_check` and
   `foreign_key_check`.
4. Strictly restart every writer that was stopped and confirm each is running.
   A failed restart fails the backup and publishes a failed attempt; it can
   never publish success.
5. With applications back online, hash the immutable working snapshot. The
   signed schema-v4 manifest authenticates database bytes/mode/ownership, the
   exact database-tree inventory and aggregate tree metadata, and
   content/configuration regular files, directories, safe relative symlinks,
   modes, ownership, extended-attribute names and values, counts, bytes, and
   tree digests. Its fixed private `RESTORE-EVIDENCE.json` companion is hashed
   into the signed manifest and binds every restorable path, object hash,
   mode, owner, hard-link relationship, symlink target, and xattr name/value.
   The manifest remains content-neutral while restore has immutable per-entry
   evidence. Mount boundaries, escaping or absolute symlinks, and special
   entries fail closed. Schema-v3 snapshots cannot satisfy this production
   recovery gate; take and verify a fresh schema-v4 snapshot before rollout.
6. Reverify the completed signed snapshot, then atomically publish its strict
   timestamped direct-child `latest` link and local success state. Verification
   reads through the retained snapshot descriptor, and both the final name and
   candidate/published `latest` links are re-pinned and compared to that retained
   inode and mount before success is written. The state includes the signed
   payload digest and measured writer quiescence; pruning remains false.

Previous snapshots are never used as hard-link source trees. This deliberately
spends more backup space so a resumed copy cannot change mode, ownership,
xattrs, or ACLs on an inode in already-signed history. Each delete, create,
copy, chmod, chown, xattr, database, sentinel, summary, status, final rename,
and latest-link operation is performed relative to a pinned directory
descriptor. Every descent rejects a newly inserted mount before it can receive
data or metadata; if a mount appears after a directory is already open, the
descriptor continues to address the original private inode and removal of the
covered mount point fails closed. Status and manifest-summary files
are written through private, random, `O_EXCL|O_NOFOLLOW` temporary files, then
atomically replaced and directory-fsynced; fixed `.tmp` paths are not used. A
failed operation may deliberately leave its random private temporary file rather
than pathname-unlink an entry that could have been concurrently substituted.

Any later, separately approved installation must place
`david_pi_fd_tree.py` beside `david_pi_recovery_paths.py`,
`david_pi_snapshot_manifest.py`, `david_pi_restore_receipt.py`, and
`david_pi_restore_drill.py` in the same root-owned non-writable executable
directory. The descriptor and receipt modules are required runtime dependencies,
not optional test helpers; an installation missing either must fail before a
backup or restore starts.

An interrupted run remains under its `.incomplete-*` name and retains its
original start timestamp. More than one incomplete snapshot requires manual
review. Never delete one merely to make a later run proceed.

## Local-to-B2 coupling

There is deliberately no independent B2 timer. Install
`david-pi-data-backup-b2.conf` only as the drop-in
`david-pi-data-backup.service.d/20-b2.conf`; its `OnSuccess=` starts upload only
after the local service exits successfully. The uploader independently requires:

- `latest` to resolve to one timestamped direct child of the protected snapshot
  directory;
- the exact snapshot id and signed payload digest in `last-success.json`;
- a bounded completion age (default six hours);
- successful full verification of the signed local snapshot before upload;
- a single active uploader lock.

The uploader uses its own ephemeral 0700 `/run/david-pi-b2-backup` namespace.
Attempt status is persistent and private under `/var/lib/david-pi-b2-backup`;
the uploader neither depends on nor writes the shared `/run/david-pi` namespace.

After upload it runs a 1% repository data check and reports
`uploaded_pending_restore`. It explicitly reports Object Lock and off-site
restore verification as false. Any failure writes a current failed-attempt
status rather than leaving an old upload result looking current.

## Secret placement

Provision separate root-owned, non-symlink, nonempty files with mode 0400 or
0600:

- `/etc/david-pi/backup-manifest.key`
- `/etc/david-pi/b2/account-id`
- `/etc/david-pi/b2/account-key`
- `/etc/david-pi/b2/restic-repository`
- `/etc/david-pi/b2/restic-password`

The B2 systemd unit loads them with `LoadCredential=` and exposes only the
temporary credential paths to the process. `/etc/david-pi/b2` is outside the
Compose tree copied into local snapshots. Keep an independently encrypted,
offline recovery copy of the manifest and restic keys. Confirm no older copy of
these secrets remains in the Compose tree before the first snapshot.

## Restore target gate

Use a dedicated, otherwise-empty filesystem mounted at a narrow path such as
`/mnt/david-pi-restore`. The mount root must be owned by the invoking approved
user (root in production) with mode 0700. Its only initial entry must be a
root-owned regular file named `.david-pi-restore-target`, mode 0600 or stricter,
whose exact content is:

```text
david-pi-isolated-restore-v1
```

The restore tool rejects a symlink target, broad/protected paths, a target on
the snapshot filesystem, an unsafe sentinel, an occupied target, and a target
without the required bytes plus at least 5% or 64 MiB headroom (whichever is
larger).

## Restore sequence

Run restores in a new loopback-only network namespace. The CLI refuses to run
when it shares PID 1's network namespace or sees any interface other than `lo`.
Use a freshly prepared target for each failed attempt; never erase a partial
target without a separate, exact cleanup review.

1. `core`: copy and compare all configuration metadata/tree hashes and every
   database hash, mode, owner, SQLite integrity result, and foreign-key result.
   Keep that exact database tree under `databases/` as signed evidence, then
   create separate UID/GID 10001, mode-0640 database copies at their real
   application paths beneath `data/`.
2. `sample`: repeat the core checks and derive the exact deterministic object
   set solely from the signed private evidence—not from a fresh source walk.
   Before and after copying through pinned source and target descriptors,
   compare every selected object's SHA-256 digest, size, mode, owner, and xattr
   values to that evidence. A same-size, mode, xattr, or source-set change after
   manifest verification fails the restore.
3. `full`: repeat the core checks and compare the entire restored data tree to
   the authenticated signed summary. Same-size corruption, hard-link changes,
   ownership/mode drift, missing directories, and symlink-target changes fail.
   Only after this comparison does assembly normalize the writable `/data`
   root. It recreates the intentionally excluded empty upload/runtime paths and
   validates the exact application storage sentinel. Signed database evidence
   remains unchanged while application copies receive the approved ownership.
4. With the source unavailable and external networking still disabled, boot a
   disposable copy of the application against the restored data and execute
   module smoke tests. This is a separate council-observed gate.

The tool reports `isolated_data_verified` only when its namespace guard passed.
It always leaves `application_boot_verified=false` and `drill_complete=false`;
no copied-data test is allowed to call the whole drill healthy.

## Signed restore evidence

The source-only `david-pi-restore-data-drill.service` resolves the hardened
`latest` snapshot link, performs a full restore with external networking removed,
and asks the restore tool to publish a signed receipt only after all restore
checks return successfully. The receipt uses the exact canonical HMAC signer and
verifier used by the snapshot manifest. It contains no paths, filenames, user
identities, device identities, database names, counts, or content. It binds a
random receipt id and canonical timestamps to the signed snapshot id and manifest
digest.

Receipt publication requires an already approved root-owned `0700`
`/var/lib/david-pi-recovery` directory and a unique root-owned `0600` regular
sentinel named `.david-pi-restore-evidence` with exact content:

```text
david-pi-restore-evidence-v1
```

The fixed `latest-restore-evidence.json` file is written and read only through a
pinned parent descriptor. Symlinks, hard links, unsafe ownership/mode, parent
replacement, oversize JSON, a stale signature, future time, or a receipt for a
different current snapshot all fail closed. The status collector accepts a
receipt for at most 90 days and only when it matches the current independent
snapshot's signed digest. Install `david-pi-server-status-restore-evidence.conf`
as a status-service drop-in only after both the sentinel directory and manifest
key exist.

Schema v1 proves only an isolated data restore. It is structurally unable to set
`application_boot_verified` or `disaster_recovery_complete` true. Consequently,
Server Status remains in a warning state with
`RESTORE_APPLICATION_BOOT_PENDING` even after valid data evidence is present.
That warning is intentional until a separately reviewed, source-unavailable
application boot exercise can issue stronger evidence.

## Explicit residual risks

- The B2 job currently uses restic `--no-lock`. Its private local `flock`
  prevents concurrent uploads from this service instance, but it cannot rule
  out a second host using the repository at the same time. Do not add another
  writer until a repository-lock-compatible least-privilege design is tested.
- The descriptor-confined copier is Linux-specific and intentionally fails when
  `openat2` or mount IDs are unavailable. A mount event or retained-root
  pathname replacement during backup or restore fails the run; reverify source
  and destination before retrying. Do not substitute a pathname copier.
- The manifest HMAC key can authenticate only while it remains secret; a fully
  compromised Pi holding that symmetric key could forge later manifests. Loss
  of either the restic password or manifest key can also make recovery or
  authenticity verification impossible. Maintain separately encrypted offline
  recovery copies and test them without placing key material in reports.
- Governance Object Lock and a source-independent off-site restore remain
  unverified until the B2 pilot completes. The status API must continue to show
  those facts as false rather than treating upload success as disaster recovery.

## B2 pilot gate

1. Create a disposable private B2 bucket and encrypted restic repository.
2. Independently confirm 30-day Governance Object Lock for repository objects.
3. Give the routine Pi credential only the minimum list/read/write capability;
   confirm it cannot delete. Keep any retention credential off the Pi.
4. Upload a fixture snapshot, interrupt and resume it, then prove deletion with
   the routine credential is denied.
5. Revoke or remove the source credential and restore with the original source
   unavailable. Compare every database and media hash.
6. Run the retention-preview command. It must remain a dry run and must never
   mutate the repository.
7. If restic and Object Lock conflict, stop. Do not weaken Object Lock.

## 4 TB transition gate

The intended new disk was absent during source preparation. When it is attached,
inspect it read-only and record serial, model, size, device path, UUID, current
mounts, and existing signatures. Council must unanimously confirm the identity
and rollback plan before any destructive disk command. Create a new
sentinel-protected destination and a fresh coherent snapshot without altering
the current backup. Production data migration and every retention change remain
blocked until a full isolated restore and disposable application boot pass.

## Capacity forecast

`david_pi_backup_capacity.py` reads only filesystem metadata. It counts
hard-linked inodes once across snapshots, estimates recent daily allocation
growth, and reports days to the 90% usage threshold without emitting filenames.
Treat fewer than two observations as insufficient data. A warning below 90
forecast days starts storage-expansion review; it never authorizes deletion.

## Required validation before installation

- Shell syntax and Python compile checks pass.
- Failure-path tests cover same-size corruption, configuration corruption, safe
  and escaping symlinks, signed-sample mode/xattr/set drift, same-filesystem
  bind mounts inserted at actual mutation timing, retained-root replacement,
  target replacement, target ownership/mode/mount/filesystem/capacity gates,
  stale or mismatched local-to-B2 status, locks, writer restart failure, and
  failed-state publication.
- `systemd-analyze verify` passes for the units and the distinct runtime/cache/state directories
  are proven present from a clean boot.
- A measured fixture run proves writer quiescence remains inside the approved
  30-minute window.
- The council unanimously re-reviews the exact commit proposed for installation
  and its rollback evidence.

## Rollback principle

Before live installation, capture unit-file hashes, enabled states, timer states,
container running states, mount identities, and the current release image. A
rollback disables only the newly introduced units/drop-in and restores those
captured definitions/states. It must not roll back, prune, rewrite, or move any
application data or existing backup snapshot.
