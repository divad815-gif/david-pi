# Backups and recovery

![Private household access and separate backup storage](images/private-home.svg)

A backup should protect household content, databases, installation configuration
and encryption keys. Keep its destination on a different physical drive. Keep
backup media private: it contains household information and the keys needed to
recover encrypted chat. Protect offsite copies according to your own needs.

During setup, select an independent prepared ext4 destination or explicitly skip.
Skipping does not block everyday use. The interface must show **not configured**
when no destination exists. A completed copy stays **restore unverified** until
you have restored into a clean machine and verified content.

## Routine backup

Start a backup from administrator controls or run `sudo david-pi backup` on the
server. Inspect the completed job and any reported errors. A missing destination,
full disk or a copy to the same physical device must not be described as an
independent successful backup. Reconnect the correct destination and retry.

Application writes must be paused or coordinated during the final consistent
snapshot. Saved databases, installation configuration, protected keys and pending
job state belong to the same recovery point. Do not copy a live database alone
with a file manager and assume it is a complete recoverable installation.

## Restore rehearsal

### Prepare the clean target

The replacement machine needs a compatible verified David-Pi release and local
management command, Docker and Compose, Tailscale, the shipped service files,
and a running management helper. It must be connected to the intended Tailscale
network with [private HTTPS enabled](TAILSCALE.md), with the backup and a separate
empty application destination mounted on [supported local ext4 storage](STORAGE.md).
The target must have no existing household installation configuration. Restore
correctly refuses to overwrite one; do not remove configuration to bypass it.

**A complete newcomer procedure for preparing this clean target has not yet
been validated or documented.** Current clean-restore testing supplied those
prerequisites separately. The commands below verify restore mechanics once the
target is prepared; they are not a complete recovery bootstrap. Completing the
ordinary installation wizard first creates a household configuration and is
not a substitute for the missing target-preparation procedure. This remains an
open [release acceptance requirement](RELEASE.md).

### Restore and verify content on a prepared target

1. Use a clean disposable machine/VM meeting the prerequisites above, with
   separate empty test storage.
2. Keep the original server and backup unchanged. Use only synthetic fixtures
   for public test evidence; never publish real household records.
3. On the original server, `sudo david-pi restore-test` verifies snapshot
   hashes and database integrity. This does **not** prove a clean restore.
   On the clean target use `sudo david-pi restore --snapshot SNAPSHOT_DIRECTORY
   --data-root EMPTY_DIRECTORY`, with explicit absolute paths, then
   `sudo david-pi repair`. Follow the requested repair job with
   `sudo david-pi status` until it succeeds, then run `sudo david-pi verify`.
4. Confirm member permissions, several file hashes, media playback, saved notes,
   recipes, watchlist and encrypted chat content from that recovery point.
5. Check that disabled modules’ data and keys were preserved and that Android
   reconnects to the intended installation identity.
6. Record the date, release and results. Report actual content verification, not
   merely a successful database integrity check.

The installer’s recovery CLI must name a specific snapshot and target. Never
restore into your existing live data directory just to test a backup. Follow
its refusal messages if the target is nonempty, storage identity differs, or
newer writes make rollback unsafe.

## Update snapshots and uninstall

Before every update the server captures a local recovery snapshot, even when
routine backups are skipped. This protects against a failed update on that
machine; it does not protect against disk failure. An incompatible migration
cannot be undone by switching images alone. Automatic recovery must never
replace newer household activity with older content. See [updates](UPDATES.md).

Currently, these update snapshots copy the entire application data directory
into management state on the operating-system disk. The destination must have
free space for that full content copy plus a 1 GiB reserve. A separate primary
data drive or independent backup drive does not change this location. Large
libraries can therefore exhaust the space available for updates; this remains
an open storage-design limitation. Do not relocate management state manually
or delete unknown recovery files to get past the check.
Configuration and keys are copied too, and each retained earlier snapshot
continues to use space on the same disk. There is currently no supported
snapshot-location setting; this limitation must be resolved before stable
publication.

To remove the running application, use the server terminal:

```sh
sudo david-pi uninstall-app
```

Type `REMOVE APP` to confirm. The command stops and disables David-Pi services
and removes the application's containers. It preserves content, configuration,
encryption keys, backups, downloaded images, the local management command and
service files. Docker, Tailscale and Pi-hole are also retained; they may support
other services on the machine.

This is not a factory reset or a disk cleanup command. There is no automatic
content-purge option. Keep backups and keys until you have verified a replacement;
erasing household data requires a separate, deliberate decision. You do not need
to reinstall the operating system for ordinary application maintenance.
