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

Use a supported clean Linux machine with a terminal, two CPU cores, 4 GB RAM
and at least 8 GiB free OS space. Attach the existing backup drive and a
separate prepared local ext4 destination with room for the restored content
plus 1 GiB. Keep the original server and backup intact. Do not complete the
ordinary new-home wizard first: that creates a different household, which
restore correctly refuses to overwrite.

1. Open the GitHub release used to create the backup. Use its exact version,
   shown in the original server's status or the backup's `release.json`.
   Download that release's `install.sh` and `install.sh.sha256` into the same
   folder on the replacement server. The release must support this recovery
   flow; a legacy 9.x backup needs the separately rehearsed migration.
2. In that folder, check the bootstrap and start **recovery preparation**:

   ```sh
   sha256sum -c install.sh.sha256
   sudo bash install.sh --prepare-recovery
   ```

   Use a matching release asset. For a stable installer whose default is
   `latest`, explicitly select the backup's version, for example:

   ```sh
   sudo env DAVID_PI_VERSION=10.0.0 bash install.sh --prepare-recovery
   ```

   Replace `10.0.0` with the exact version on the backup. A beta-specific
   installer already pins its exact beta version; it never selects a newer
   testing release automatically. Do not invent or paste a container digest.
   The bootstrap checks the source archive and retains the verified release
   selection used by the recovery command.
3. Follow the terminal's prerequisite installation and Tailscale sign-in.
   Approve **this replacement server** in the intended household network,
   then enter the exact individual account that owns the node. The account
   must match Tailscale's authenticated identity. Choose the replacement
   hostname. Existing unrelated Serve settings remain protected.
4. Follow [Enable HTTPS Certificates](TAILSCALE.md#3-enable-https-certificates)
   in the Tailscale admin console. Return to the terminal and press Enter
   after enabling certificates. The terminal displays the actual assigned
   private address, including any collision suffix. It also installs and
   starts the restricted local management helper and shipped service files.
   No new household ID, keys, administrator membership or claim token is created.
   The browser shows a recovery page until restored services are ready.

If preparation stops, fix the reported prerequisite or network problem and run
`sudo david-pi prepare-recovery` in the same terminal. The verified release
selection is retained. Do not delete configuration or keys to bypass a refusal;
use the machine's saved recovery state or a genuinely clean replacement.

### Select the backup and empty destination

Run this on the prepared replacement machine:

```sh
sudo david-pi restore
```

The terminal lists detected ext4 drives with their names, locations and capacity;
mounted drives also show free space. Select the backup drive, then a completed
snapshot by date, version and ID. Select a different physical drive for restored
data, and review the exact source and destination before typing `RESTORE`.
The new dedicated destination is named `david-pi-data` and must be empty.
Unsupported filesystems, ambiguous mounts and duplicate filesystem identities
are not offered. No drive is formatted or repartitioned.

An unmounted existing backup is mounted **read-only** at a new location under
`/mnt`; an unmounted replacement data drive is mounted read/write. Device
identity is checked again when selected. Already mounted drives keep their
mount settings. The restored primary drive's mount is configured by UUID when
needed. A backup attached by this recovery command is a temporary mount;
routine backups are **not configured** after restoration. Select and verify
an independent backup destination again once the household is working.

If your snapshots are in a custom folder, mount the existing ext4 drives with
your OS tools and use explicit absolute paths instead:

```sh
sudo david-pi restore --snapshot /mnt/backup/david-pi-backups/SNAPSHOT_ID --data-root /mnt/data/david-pi-data
```

Replace both paths with the intended mounted locations. An explicit restore
from a **local update snapshot** may use another empty folder on the same drive,
provided the source and destination trees do not overlap and sufficient space
remains. It still requires a clean installation and never overwrites the running
home's data. This is failure recovery on that drive, not protection against disk
failure. Independent backups require a different physical destination drive.

Restore verifies the
complete snapshot inventory, hashes, installation identity, keys and compatible
exact release before copying. A backup cannot supply an arbitrary image digest.
The saved website name, household members, permissions and installation ID
remain the same. The website uses the replacement node's verified HTTPS address;
update bookmarks and explicitly reconnect Android to the same installation.
If the old administrator account is unavailable, restoration does not silently
promote the replacement node's account. Use the documented local
[administrator recovery command](HOUSEHOLD.md) after restoring.

### Start services and verify actual content

```sh
sudo david-pi repair
sudo david-pi status
```

Follow the saved repair job until it completes, then run:

```sh
sudo david-pi verify
sudo david-pi address
```

Open the printed private address on a device connected to the household's
Tailscale network. Check member permissions, several original files, media
playback, notes, recipes, watchlist and encrypted chat from the chosen recovery
point. Verify disabled modules' content and keys, and Android reconnection.
Record the date, release and results before calling the restore successful.
`restore-test` checks hashes and database integrity on the original server; it
does not replace this clean-machine content check.

### If copying or startup stops

Before configuration is installed, a private recovery journal identifies the
exact snapshot and destination. Run `sudo david-pi restore` again and choose
`RESUME`. It verifies committed copies before continuing and refuses changed
content or a different snapshot. It restarts an incomplete temporary copy only
when the saved directory identity and snapshot paths still match, reclaiming
that copy before checking space. Retries need room for one complete copy plus
the reserve, rather than accumulating copies. Unknown files, links or changed
staging identities are preserved with a local-review error; do not remove
unrelated files or saved keys.
After configuration is installed, use `sudo david-pi repair`, then follow its
job with `status`. Restore refuses to copy older data over that installation.
A missing drive, full destination or altered backup remains a failure until
corrected; do not delete configuration to force it through.

This preparation workflow has automated protocol and file-copy checks. Full
newcomer recovery acceptance still requires following these instructions on a
clean real or virtual machine with real Tailscale access and checking content.
Earlier restore acceptance provisioned prerequisites separately; it is not
proof that the new preparation steps have already passed that gate.

## Update snapshots and uninstall

Before every update the server captures a local recovery snapshot, even when
routine backups are skipped. This protects against a failed update on that
machine; it does not protect against disk failure. An incompatible migration
cannot be undone by switching images alone. Automatic recovery must never
replace newer household activity with older content. See [updates](UPDATES.md).

Update snapshots now default to a private folder alongside the library on its
filesystem. The first snapshot needs room for a full copy plus database working
space and a 1 GiB reserve; later snapshots reuse verified unchanged recovery
copies. Configuration, keys and consistent databases are included. Live files
are never hardlinked into recovery snapshots.

Settings can select another dedicated prepared local ext4 recovery folder. After
a successful update, the newest recovery point remains and older successful
points are removed; failed or interrupted attempts stay for review. Use
`sudo david-pi update-snapshots` to inspect completed copies. See
[update storage and retention](UPDATES.md#retention-and-freeing-space) before
removing a known recovery point. Changing the storage setting leaves old copies
at their original location. These local snapshots remain separate from routine
independent backups.

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
