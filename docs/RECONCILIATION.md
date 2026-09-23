# Source reconciliation for the portable release

This record describes the source used to build the unpublished 10.0.0 candidate.
It does not authorize or report a production upgrade.

## Provenance

The existing repository and AGPL-3.0 history are preserved. The public upgrade
baseline is `v9.22.2`, commit
`cc47ff7eb5210d3379fc1ab07e0ce7e11f166986`.

A read-only source capture from the running ARM64 server was taken on
2026-09-23 UTC. The captured `app.py` matched the running portal container:
`54dcec7c5054ae9c668063ab1ad836991e165b46b38cbe4ccde2ac5e523643e8`.
The private source-capture archive has SHA-256
`c638fb345aa9a95bb29166f1d8e40d0f5d72c0a67b4f0e8454f37754b4bed717`.
The archive and operational inventory stay outside public release artifacts.

The portal was running image
`sha256:8b017a329c0729039e1534be87c23d42700f7aa34a4cc2fa16e8f73087b6fd63`;
background workers used
`sha256:1f356db90afb604b08b88bf9dd3aa165d692c91bce0206c713bb9bc50c684510`.
Those different images are evidence of version drift, not a release baseline
to reproduce. The portable package builds the portal and selected workers from
one reconciled source image for each supported architecture.

## Reconciled behavior

The application, Android sources, UI and domain tests were brought forward from
the source capture. The public verified bootstrap, resumable installation,
storage checks and release tooling were retained and extended. Personal
identities and addresses were replaced with synthetic examples. Databases,
keys, logs, signing material and private operational reports are excluded.

The maintenance failure included an observation for `access_control` that its
validator did not admit. The observation registry now accepts that subsystem.
Actual VM execution also exposed restrictive source-file permissions that
prevented the separate maintenance user from importing Python modules. Image
construction now makes public application code readable while keeping runtime
state and secrets separately protected. Recovery checks are recorded separately;
a healthy maintenance worker is not evidence of a successful restoration.

Runtime selection comes from the versioned installation configuration and shared
module registry. Unselected modules do not initialize their domain databases or
run their workers. Their existing content and keys are retained. Shared media
storage can support MyTube without exposing the Media module; Phone Backup
explicitly requires Media. Companion pairing is a core capability, independent
of whether automatic phone backup is selected.

## Persistence contract

| State | Required preservation |
|---|---|
| Installation configuration | Installation UUID, actual HTTPS origin, household logins and roles, storage identity, selected modules, locale and provider modes |
| Protected secrets | Chat encryption key, optional browser-push keys, provider credentials and companion authentication state |
| Media and files | Original content, metadata databases, shared/private ownership, collections, upload receipts and in-progress job state |
| Audiobooks | Original content, prepared playback assets, book metadata, person-scoped progress and offline synchronization revisions |
| Local applications | Notes, recipes/planning, movie watchlists and subscription choices, places, games and local assistant state |
| Chat | Encrypted messages and attachment content, memberships and delivery state, with the matching encryption key |
| Host operations | Installed release metadata, persistent operation journal, storage UUID, prior image/configuration and update recovery snapshots |

Backups and update snapshots include managed content, consistent databases,
installation configuration and matching secrets. Restoring databases without
their content or encryption keys is not sufficient. Backup integrity checking
and a clean-machine content restoration remain distinct results.

## Upgrade and release evidence

The migration harness archives the exact public tag, creates synthetic content
through its historical APIs, upgrades a copy with the current application, and
starts that copy twice. It checks actual text and file hashes, chat decryption,
private/shared access boundaries and migration idempotency. It also verifies
that the original fixture remains unchanged. No household database is used.

See [verification status](VERIFICATION_STATUS.md) for completed checks and
remaining acceptance gates. Only evidence bound to the final source and APK can
satisfy the [stable publication gate](RELEASE.md). The live server's eventual
migration is a separate rehearsal and deployment.
