# Choose storage without risking existing files

![Private household access and separate backup storage](images/private-home.svg)

Use a folder on a local ext4 filesystem or an already prepared ext4 drive.
Network shares, NTFS, exFAT, encrypted drives requiring an unattended unlock
workflow, and automatic blank-drive formatting are not supported in the first
release. A USB drive must have reliable power and remain attached.

## Choose from the detected locations

Connect and mount the drive on the **server**, then open the wizard's storage
section. A drive attached only to your browser computer is not server storage.
The dropdown lists suitable mounted local ext4 locations and shows their label,
total capacity and available space. It identifies the system disk separately.
Compare the label and capacity with the drive you intend to use before selecting
it. The listed path is where Linux has mounted it; you do not need to memorize
or type that path for a detected choice.

Choose the location for your library first. Setup creates a dedicated
`david-pi-data` child directory. It preserves files outside the managed
application directory. Space and the selected drive are checked again before
installation, because a drive may have been disconnected since the list loaded.

For backups, choose a suitable location on a **different physical drive**, or
choose **Skip backups for now — configure later**. The backup destination gets its own
`david-pi-backups` directory. Another folder or partition on the primary drive
does not protect against that drive failing. If there is no separate drive yet,
skip backup and configure one later. Normal use can continue.
The wizard does not configure a backup drive to mount automatically after a
reboot. Prepare that persistent mount using the operating system's tools, and
after reboot check that the same backup filesystem is mounted before relying
on it. A backup destination that is missing must be reconnected before backup
can succeed.

**Checkpoint:** the selected data location has enough free space for the
libraries you plan to keep. Backup is either explicitly skipped or points to
independent storage. The system disk is a valid choice only when it uses ext4
and has sufficient room for both the operating system and your content.

Leave room for processing as well as original files. Audiobook uploads and
playback preparation keep at least **1 GiB free**, plus the estimated space for
playback copies and temporary processing. If an import reports insufficient
space, free space on the library drive before retrying; adding a backup drive
does not increase the library's available space.

## If your drive is missing from the list

1. Check it is attached to the server and powered on.
2. Open the server's disk utility, or use `lsblk -f` in its terminal, to check
   the filesystem and mount location. It must be local, ext4 and mounted.
3. If it is prepared but not mounted, follow
   [the existing-ext4 mounting example below](#mount-an-existing-ext4-filesystem-from-a-terminal),
   then choose **Refresh storage choices** in the wizard.
4. If you want a particular existing folder within a supported filesystem,
   open **Advanced: enter an existing folder path**. Enter its existing absolute parent
   path, such as `/srv/home-library`; setup adds its dedicated child directory.

A blank drive will not become an eligible choice automatically. Before preparing
one, identify the physical device by model and capacity; an unformatted drive
may not have a filesystem UUID yet. Back up existing content first. Creating an ext4 filesystem erases
the selected partition, so use the OS's disk preparation instructions and verify
the selected device. David-Pi does not format or repartition drives.
An illustrated blank-drive preparation guide remains to be completed before
the newcomer installation checks can pass.

## Mount an existing ext4 filesystem from a terminal

This example is for an already prepared local ext4 filesystem on the server.
It does not format or partition anything. First identify the intended drive
and its filesystem using this read-only command:

```sh
lsblk -o NAME,SIZE,MODEL,FSTYPE,UUID,MOUNTPOINTS
```

Match the physical drive's model and capacity, then find its existing **ext4**
filesystem and UUID. If its **MOUNTPOINTS** column already lists a location,
refresh the wizard instead of mounting it again. If the filesystem is not ext4,
the UUID is missing, or you cannot confidently identify the drive, stop here.

For an unmounted ext4 filesystem, choose a new, unused mount directory. The
following example uses `/mnt/home-data`. Run the first command separately; if
the directory already exists, stop and choose another unused name. Do not mount
over a directory containing existing files.

```sh
sudo mkdir /mnt/home-data
```

Replace `YOUR_EXISTING_EXT4_UUID` with the UUID you just checked, then mount
that filesystem and confirm its identity:

```sh
sudo mount -t ext4 UUID=YOUR_EXISTING_EXT4_UUID /mnt/home-data
findmnt --mountpoint /mnt/home-data --output TARGET,SOURCE,FSTYPE,UUID
```

Check that the reported filesystem is ext4 and the UUID matches the intended
drive. If mounting fails, read the error rather than formatting the drive.
Choose **Refresh storage choices** in the wizard and select that location.
For a selected primary data drive, setup manages the persistent UUID mount when
required. Backup-drive mount persistence must be prepared separately using the
operating system's tools and checked after reboot. This mounting example alone
does not edit the system's persistent mount configuration.

## What setup manages

When required for a primary data drive, setup configures the existing filesystem to mount by UUID so
changing USB ports does not select another disk. It grants access only to its
managed paths; it does not recursively change unrelated files' ownership.
It does not configure automatic mounting of the backup drive.

Each application directory has an installation storage identity. Do not move,
replace, copy or edit that identity file to bypass a warning. If the intended
disk is missing or replaced, startup stops instead of creating an empty library
on the system disk. Reconnect the correct drive and verify its mount before
retrying. If a drive fills up, free space by removing content you recognize;
do not delete unknown database files or active upload parts.

Skipped backup must show **not configured**. A configured backup remains
**restore unverified** until an actual restore has been tested. See
[backup and restore](RECOVERY.md). Local recovery snapshots created before
updates do not replace an independent backup.

## Space for updating the server

Update recovery defaults to a private folder alongside the library on the same
filesystem. The first snapshot needs room for a full copy plus database working
space and a 1 GiB reserve. Later snapshots share unchanged recovery copies, while
changed content gets new copies. Leave space for that first snapshot when planning
your library drive, or choose a separate prepared local update recovery folder in
Settings. The system disk still holds container images and release downloads, but
it no longer receives a full external-library copy by default. See
[update storage and retention](UPDATES.md#where-update-snapshots-live).
