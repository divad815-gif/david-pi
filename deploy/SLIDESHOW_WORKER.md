# David-Pi slideshow worker

Slideshow creation has one executor mode. The portal runs with
`DAVID_PI_SLIDESHOW_EXECUTOR_MODE=queue`: it authorizes the caller, validates
the collection and exact source metadata, chooses the result privacy, and
durably queues a job. It does not start a thread or FFmpeg process. The separate
`slideshow-worker` service runs with executor mode `worker` and is the only
component allowed to claim and render those jobs.

Every accepted job binds its owner, result visibility, fixed result media ID,
collection name/owner/privacy/version/deletion state, ordered source IDs and
their owner/privacy/version/deletion/hash/size/path/type metadata, render
settings, and a canonical snapshot digest. Claims use one `BEGIN IMMEDIATE`
oldest-job compare-and-swap. Generation, attempt, worker, token, and bounded
expiry fence every later update. An expired lease cannot be renewed or used for
publication. Retry exhaustion is terminal and user-visible errors never expose
raw FFmpeg diagnostics.

While a job is owned, a dedicated keepalive commits a fenced lease renewal
before atomically refreshing the runtime health receipt. It remains active
through source preprocessing, the long FFmpeg render, derivative creation, and
publication recovery. The keepalive stops at the bounded job deadline, and any
lease loss or health-write failure blocks completion and every later live
publication check. Health receipts are accepted for at most 300 seconds, while
the configured 30-second interval keeps legitimate jobs healthy throughout the
14,400-second runtime limit.

The renderer opens source media read-only through descriptor-pinned storage.
It renders into anonymous `O_TMPFILE` inodes, enforces job, FFprobe, FFmpeg,
output-size, free-space, process, CPU, and memory limits, and terminates the
FFmpeg process group on timeout or shutdown. Once output verifies, the worker
stores its future name plus exact device, inode, size, and digest before linking
that descriptor into `incoming`. Thus a crash before the link leaves no named
orphan, while a crash after the link leaves enough durable identity evidence
for exact cleanup. A symlink or same-name replacement is retained as operator
evidence and is never removed automatically. Source originals are never
renamed, unlinked, rewritten, or used as cleanup targets.

Publication uses the existing durable media-intent transaction with the fixed
result ID. Prepared and committed intents must exactly match the job owner,
privacy, target inode/size/digest, slideshow ingestion source, and required live
validator contract. Immediately before the database publication commits, the
worker revalidates its live lease and the collection plus every ordered source's
current version, privacy, owner, deletion state, and stored metadata. A
committed matching intent is completed without rendering again, which makes
the publication exactly once across crashes.

The Compose worker has no network, ports, or secrets; runs as explicit UID/GID
10001; drops all capabilities; enables `no-new-privileges`; uses a read-only
root filesystem and private bounded tmpfs; and has explicit CPU, memory, PID,
queue, lease, runtime, FFprobe, FFmpeg, output, and free-space limits. The
family media bind must remain writable because canonical publication creates
the generated original and derivatives in that tree. Read-only source behavior
is therefore enforced by descriptor modes, exact database-selected paths, and
inode-fenced cleanup rather than an unsafe split view of one object store.

First activation is fail-closed. The portal must migrate the additive schema
before the worker becomes healthy. Any legacy `working` job without a complete,
valid, bounded lease and snapshot contract blocks worker activation; deployment
must not guess an owner or adopt it. The worker also refuses to bootstrap
missing media directories or schema. `david-pi-portal-lifecycle`, writer
readiness, server status, independent backup quiescence/restart, maintenance
preparation, release promotion, and rollback metadata all include this service.

The first rollout's previous image has neither candidate-only worker module, so
it must never be assigned to `slideshow-worker` or `device-backup-worker`.
While holding the writer-transition lock, follow the checked schema-v2 rollback
sequence: stop both candidate-only workers, move only the four legacy writers
to the exact previous digest, and require the hash-bound legacy readiness gate
to prove those writers ready and both newer workers absent or stopped. Retain
the slideshow queue, device-upload and secondary-copy state, staging evidence,
publication intents, schema, and all media unchanged; queued work stays dormant
until compatible workers return. Only a later rollback digest with separate,
hash-bound worker-compatibility evidence may move all six services together.
Never delete or rewrite a job, intent, staged inode, source item, or generated
item merely to make rollback pass. This repository change is source-only and
does not activate or deploy the service. See `RELEASE_PROMOTION.md` for the
reviewed argument-vector sequence.
