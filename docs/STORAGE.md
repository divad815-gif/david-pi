# Choose storage without risking existing files

![Private household access and separate backup storage](images/private-home.svg)

Use a folder on a local ext4 filesystem or an already prepared ext4 drive.
Network shares, NTFS, exFAT, encrypted drives requiring an unattended unlock
workflow, and automatic blank-drive formatting are not supported in the first
release. A USB drive must have reliable power and remain attached.

Before setup, identify drives by capacity, model and UUID using your OS disk
utility or `lsblk -f`. Back up any existing content before preparing a blank
or repurposed disk. Creating an ext4 filesystem erases its selected partition;
use your operating system’s disk utility and verify the selected device there.
David-Pi does not format or repartition disks for you.

Select an existing writable directory in the wizard. David-Pi creates a dedicated
child directory and marks it with the installation’s storage identity. When
mount configuration is required it uses the existing filesystem UUID, so USB
port order does not change which disk is used. It grants access only to managed
application paths; it does not recursively change unrelated files’ ownership.

Do not move, replace, copy or edit the storage identity file to bypass a warning.
If the intended disk is absent or replaced, startup must stop rather than create
an empty library on the system disk. Reconnect the original disk and verify its
mount before retrying. A full drive must reject uploads cleanly; free space
without deleting unknown database files, then retry the operation.

A backup destination is optional during setup. It counts as independent only
when it is a different physical device from primary storage. A second folder
on the same drive cannot protect against that drive failing. The interface must
say **not configured** when skipped and **restore unverified** until a complete
restore drill succeeds. See [backup and restore](RECOVERY.md).
