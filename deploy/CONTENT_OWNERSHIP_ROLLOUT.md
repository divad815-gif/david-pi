# Saved-content ownership rollout

Notes and Files now enforce the reviewed route-policy ownership rules in the
domain write transaction. This source change is not authorization to run a
live migration or to assign historical content.

## Invariants

- A human actor comes only from the verified Tailscale login and the exact
  explicit admitted-household identity list. Request bodies never select an owner.
- `admin` controls the service; it is not a content-superuser role.
- Shared records remain readable by both household members. Updates, state
  changes, restore, and deletion are owner-only.
- Historical rows with no `owner_id` are `legacy_unclaimed`: readable when
  shared, hidden when private, and immutable. No display-name or legacy label
  is treated as ownership evidence.
- Every successful domain mutation and its content-neutral audit/outbox entry
  commit in one `BEGIN IMMEDIATE` transaction. Updates also bind both
  `owner_id` and `version` in the SQL predicate.
- Permanent purge remains unavailable. The API returns `503` with
  `retained=true` until the 30-day retention rule and a protected backup newer
  than the trash event can be proved by an independently accepted recovery
  implementation.

## Additive schema behavior

Startup adds nullable ownership columns to existing folder rows and a positive
version column to existing file rows. SQLite supplies version `1`; ownership
stays null. It never rewrites an existing note, file, folder, object, name, or
visibility value.

## Required live gate

Before deployment, operate only on a storage-consistent database clone and:

1. Run `david_pi_ownership_inventory.py` with the exact two independently admitted test users
   principals. Retain the content-neutral counts with the release evidence.
2. Verify a fresh protected backup and a no-network restore drill.

3. Exercise two independently admitted test users canaries: each can create and mutate their own
   private/shared note, root folder, and file; each can read but cannot mutate
   the other's shared records; neither can see the other's private records.
4. Verify legacy shared rows render as `Legacy (unclaimed)` and every attempted
   mutation leaves the row, object bytes, version, and audit count unchanged.
5. Verify stale-version and failed-audit tests roll back the domain mutation.
6. Keep the previous application image available for rollback. Schema rollback
   is forward-only: older code may ignore the additive columns, but no column
   or audit history is removed.

## Access-control operational evidence

Each denied decision emits one fixed, content-neutral counter record with only
its mode and disposition. It has no principal, role, path, method, route, query,
address, reason, or content dimension. The host status collector parses only
those exact records from a bounded 24-hour Docker log window, aggregates across
all portal workers, and discards every other log line. It reads the configured
mode from the dedicated nonsecret Compose label rather than inspecting the
container environment.

Server Status publishes observed, blocked, shadow, and unenforced domain totals
with `counter_scope=container_aggregate_rolling_window`. An unavailable Docker
probe, capped log window, `shadow`, `off`, or invalid mode cannot appear green.
An invalid value is shown as configured mode `unknown` with effective
safe-shadow behavior. The log window is intentionally bounded and not durable
lifetime accounting; adding a new writable operational-state path is outside
this rollout.

Any failed check rejects the live rollout. It does not authorize repairing,
claiming, deleting, or moving saved content.
