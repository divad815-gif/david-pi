# Installation configuration

The installer owns `/etc/david-pi/installation.json`. It is a versioned, validated
document, readable by the application service but writable only by the local
host helper. Ordinary portal requests cannot write it or run shell commands.
Settings uses a restricted local socket with an explicit operation allowlist.

| Field | Meaning |
| --- | --- |
| `schema_version` | Configuration format; currently integer `1`. |
| `instance_id` | Immutable random UUID created during setup; identifies this household installation across upgrades and restores. |
| `display_name` | Website name, independent of the address and storage. |
| `hostname` | Hostname actually assigned after Tailscale enrollment, including any collision suffix. |
| `public_url` | Exact approved Tailscale HTTPS origin; address changes require local recovery and Android reconnection. |
| `timezone`, `country` | IANA timezone and two-letter movie-availability country. |
| `members` | Explicitly admitted individual Tailscale logins, display names, and `admin` or `household` roles. At least one administrator is required. |
| `storage` | Dedicated local ext4 primary location, folder/drive mode, and optional independent backup location. |
| `modules` | Selected modes from the shared registry in `modules/installation.py`. Omitted modules are disabled. |
| `integrations.web_push` | Optional browser notification delivery. Requires Chat. |

Movie Night and Recipes each support `manual`, `connected`, and `disabled`.
Other modules support `enabled` and `disabled`. Phone Backup requires Media;
the Android pairing registry remains available for an audiobook-only server.
The shared media storage substrate supports MyTube without enabling the Media
website or slideshow worker.

Configuration updates use atomic replacement. Changing the display name keeps
the UUID, actual HTTPS origin, and data location. Removing a person removes
website admission and invalidates their paired device access without deleting
their content. Administrator status grants server-management rights; it does
not grant access to another person's private library.

Provider credentials, the Chat encryption key, and browser push private keys
are stored separately in protected files under `/etc/david-pi/secrets`. They are
excluded from ordinary configuration, browser status responses, source
archives, images, and the Git repository. Backups and update recovery snapshots
must retain these keys so encrypted content can be recovered.

Disabling a module blocks its routes, hides navigation, removes its selected
background services, and prevents its ordinary storage migration/import on the
next portal start. Data and credentials remain available for re-enabling it.
Host configuration jobs recreate containers so replaced configuration files
cannot leave an existing bind mount pointing at an old version.

Without a claimed configuration the production portal admits no real user.
Developer tests use isolated synthetic fixtures; those are never household
admission defaults. Invalid or unsupported configuration fails closed and needs
[local administrator recovery](RECOVERY.md).
