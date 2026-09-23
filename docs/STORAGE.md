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
3. If it is prepared but not mounted, mount it using the operating system's
   tools, then choose **Refresh storage choices** in the wizard.
4. If you want a particular existing folder within a supported filesystem,
   open **Advanced: enter an existing folder path**. Enter its existing absolute parent
   path, such as `/srv/home-library`; setup adds its dedicated child directory.

A blank drive will not become an eligible choice automatically. Before preparing
one, identify it by model, capacity and UUID with your OS disk utility or
`lsblk -f`. Back up existing content first. Creating an ext4 filesystem erases
the selected partition, so use the OS's disk preparation instructions and verify
the selected device. David-Pi does not format or repartition drives.

## What setup manages

When required, setup configures the existing filesystem to mount by UUID so
changing USB ports does not select another disk. It grants access only to its
managed paths; it does not recursively change unrelated files' ownership.

Each application directory has an installation storage identity. Do not move,
replace, copy or edit that identity file to bypass a warning. If the intended
disk is missing or replaced, startup stops instead of creating an empty library
on the system disk. Reconnect the correct drive and verify its mount before
retrying. If a drive fills up, free space by removing content you recognize;
do not delete unknown database files or active upload parts.

Skipped backup must show **not configured**. A configured backup remains
**restore unverified** until an actual restore has been tested. See
[backup and restore](RECOVERY.md). Recovery snapshots made on the primary disk
before updates do not replace an independent backup.
