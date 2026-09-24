# Complete device reconciliation

Device reconciliation is an observation of whether media previously backed up
from one authenticated Android device is still visible to that same device. It
does not delete, move, rewrite, or change access to canonical photos or files.
The iOS Shortcut path never submits reconciliation receipts.

## Receipt contract

`POST /api/v1/device-backup/reconcile` accepts exactly these fields:

- `scan_id`: canonical UUIDv7 generated once for the observation;
- `complete`: the literal JSON value `true`;
- `item_count`: count after canonical deduplication;
- `ids_sha256`: lowercase SHA-256 of the compact ASCII JSON encoding of the
  canonically sorted, deduplicated ID array;
- `visible_client_item_ids`: the complete Android image-and-video observation.

IDs use `[A-Za-z0-9._:-]`, are at most 200 characters, and are sorted by their
ASCII representation. The practical maximum is 50,000 unique IDs. The client
and server reject larger scans explicitly; neither silently truncates. The
50,000-item envelope is expected to remain below ordinary private-LAN request
limits while leaving substantial headroom above 5,000-item libraries.
The route independently caps the raw body at 12 MiB and authenticates the
credential without updating timestamps before reading or decoding the body.

Selected-photo permission, a missing image or video permission, a null or
failed MediaStore query, and cancellation all make a scan incomplete. An
incomplete scan is never sent and the server independently rejects it.

## Replay and ownership behavior

The Android client durably retains a pending receipt. An ambiguous network
result retries the same UUIDv7 when the canonical count and digest are
unchanged. The server returns the stored result for an exact replay, rejects a
conflicting replay, and rejects a receipt older than the latest accepted UUIDv7.
Distinct accepted receipts are throttled per device.
The server rejects UUIDv7 timestamps more than five minutes in the future with
the distinct `422 scan_clock_skew` protocol response. Its error object binds the
rejected `scan_id` to `server_time_ms` and `max_future_skew_ms`. Android accepts
that recovery only when the response ID exactly matches its pending receipt and
the UUID timestamp actually exceeds server time by the documented bound. It
persists the server-clock anchor before retiring only that rejected pending ID.
If the app stops between those operations, the persisted binding still prevents
the bad UUID from flooring the replacement. Missing, malformed, or misbound
clock data is protocol ambiguity and retains the same pending ID.

The client persists both its last acknowledged receipt and any ambiguous
pending receipt, and floors a replacement UUIDv7 above both values. A local
wall-clock rollback therefore does not create an older receipt, even when scan
content changes after an ambiguous response. An interrupted request and a 2xx
response with missing or malformed successful JSON are both ambiguous: Android
retains and retries the same receipt ID. A valid clock-skew rejection is the
only response that can retire its exact rejected ID without acknowledgement.

One `BEGIN IMMEDIATE` transaction authenticates the active credential, stages
all supplied IDs in a temporary table, updates only that device's
`device_media_records.local_source_visible` observations, records the receipt,
and writes a content-neutral event. Rejected requests do not advance device
contact or reconciliation timestamps. Canonical `photos`, file tables, and
stored objects are outside the transaction's mutation set.

## Android queue migration and rollback

Room migration 1→2 only adds `scanGeneration` and `sourceSignature`. Existing
rows receive the `legacy-existing` generation and an empty signature so their
hash, server upload ID, accepted offset, state, retry count, and error remain
unchanged. The first complete scan adopts a signature without resetting those
fields. A different source signature or client ID is treated as MediaStore ID
reuse and creates a fresh queue identity with fresh transfer state.

Upload selection and physical visibility are separate decisions. Every row in
the complete, fully authorized MediaStore observation contributes exactly one
opaque deterministic exact-source ID to reconciliation, including screenshots,
downloads, and messaging folders that are excluded from backup. Exclusion does
not create a queue row or upload that content. When durable queue state proves
the source signature is unchanged, its existing upload ID is retained. After
app-data loss, unchanged MediaStore metadata reconstructs the same opaque ID.
If the source signature changed, the current physical object receives a new ID
and the previously backed-up exact version is truthfully absent; reconciliation
does not claim that the old bytes remain on the phone. Filenames, bucket names,
content URIs, and source metadata are never placed in the receipt.

MediaStore observations are applied in bounded batches of 400. Each batch uses
one indexed lookup and one Room transaction, preserving the legacy queue ID and
all transfer state for an unchanged source while applying metadata and scan
evidence together. A 50,000-item collection therefore requires at most 125
observation transactions rather than one transaction plus multiple lookups per
item. Images and videos are separate collections, so a 50,000-item combined
library needs at most 126 transactions when both collections are non-empty.
The batching change does not alter Room schema version 2, migration 1→2, or the
durable pending/acknowledged reconciliation state.

Rollback to the previous server is source-compatible only if the new Android
client is not pointed at its legacy permissive reconciliation endpoint. A live
rollout therefore requires a server-first canary and an exact image rollback
reference. Rolling the source back does not require deleting the additive
receipt table or Android columns; leave them inert to preserve recovery
evidence.
