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

1. Use a clean disposable machine/VM with separate empty test storage.
2. Keep the original server and backup unchanged. Use only synthetic fixtures
   for public test evidence; never publish real household records.
3. On the original server, `sudo david-pi restore-test` verifies snapshot
   hashes and database integrity. This does **not** prove a clean restore.
   On the clean target use `sudo david-pi restore --snapshot SNAPSHOT_DIRECTORY
   --data-root EMPTY_DIRECTORY`, with explicit absolute paths, then
   `sudo david-pi repair` and `sudo david-pi verify`.
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

`sudo david-pi uninstall-app` removes application services and preserves content
by default. Keep backups and keys until you have verified a replacement. Erasing
data is outside uninstall’s default behavior.
