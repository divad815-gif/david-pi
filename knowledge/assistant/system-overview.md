# David-Pi system overview

David-Pi is a private household server running on a Raspberry Pi 4 with 4 GB
of memory and 64-bit Raspberry Pi OS. The operating system boots from microSD.
Persistent portal data lives on an external 4 TB ext4 drive mounted at
`/srv/data`. The portal runs in a hardened Docker container and uses SQLite.

The portal contains Media, Files, Notes, Movie Night, Recipes, Games, phone
backup, Server Status, and Assistant modules. Tailscale supplies private HTTPS.
The site is not intended to be publicly accessible.

