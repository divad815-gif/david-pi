# David-Pi release promotion and rollback contract

This contract prepares evidence. It does not connect to the Pi, change a
service, edit SSH, deploy a container, or prune an image. Promotion remains a
separate, explicitly approved operation after backup/restore and household
access gates pass.

## Required identities

A production candidate is eligible only when all three inputs are immutable
OCI references:

- the candidate application image (`repository@sha256:<64 lowercase hex>`);
- the last known-good rollback image in the same repository; and
- the Python base image used to build the candidate.

All three inspected images must use the same supported Linux architecture,
either AMD64 or ARM64. Set `DOCKER_DEFAULT_PLATFORM` to `linux/amd64` or
`linux/arm64` for the specific evidence run. ARM64 execution on an AMD64 host
requires QEMU. Architecture mismatches fail closed in the base, candidate, and
rollback evidence checks. Public stable publication also requires both target
architectures and the acceptance receipts in `docs/RELEASE.md`.

The release image gate also exercises the Alpine/musl `renameat2` syscall
fallback used by race-safe audiobook cleanup. It asserts the selected architecture’s kernel ABI and syscall number
(`aarch64`: 276; `x86_64`: 316), demonstrates no-replace success and collision
preservation, and proves cleanup fails closed when neither interface exists.
Host-side release helpers still use their runner's Python and POSIX shell;
that does not add Bash or host tooling to the minimal application image.

A tag such as `latest`, `stable`, or `9.22.0` is useful for discovery but is
never sufficient evidence for promotion or rollback. Do not guess or copy a
base digest from documentation. Resolve it in the release environment, pull
that exact reference, and retain the generated provenance evidence.

The Dockerfile keeps `python:3.13-alpine3.22` as the compatible development default.
It accepts `PYTHON_BASE_IMAGE` for a release build and records the exact base
reference and digest in OCI labels. This deliberately leaves digest resolution
to release CI rather than pinning an unverified value in source.

To build a candidate manually from an already resolved base image for local
testing:

```sh
make image-pinned-base \
  IMAGE=registry.example/david-pi:candidate \
  BASE_IMAGE=python:3.13-alpine3.22@sha256:<verified-digest>
```

Manual candidate images are never accepted by the hosted promotion job. They
remain useful for testing the same release contract locally.

## Vulnerability evidence

`make vulnerability-scan` runs a fixed-policy Trivy 0.74.0 dependency scan of
`requirements.txt` and a separate scan of the built image. Trivy runs on the
CI host, not in the application image. The application containers are not
given scanner databases, network access, or credentials.

The scanner writes normalized JSON plus the two raw Trivy JSON documents. The
gate:

- requires a parseable scanner version and valid schema-v2 output;
- records the scanner executable hash and vulnerability-database timestamps;
- requires non-empty language-package and image-package coverage;
- does not ignore unfixed findings;
- blocks every `HIGH` or `CRITICAL` finding; and
- exits `2` with machine-readable `status: error` evidence when the scanner is
  absent, crashes, or produces malformed/empty output.

Exit `1` means a real policy finding; exit `2` means the scan itself cannot be
trusted. Neither is a pass. The promotion checker reconstructs normalized
results from the hash-bound raw scan files, so changing only a top-level status
or count cannot manufacture a successful result.

## Promotion bundle

Enable `prepare_promotion` in the manual `Verify release candidate` workflow on
the exact `main` revision and supply two immutable inputs: the known-good
rollback image and the verified Python base image. The rollback image must be
in this repository's fixed lowercase `ghcr.io/<owner>/<repository>` package.
The base reference must use the Docker Official Image repository `python` by
immutable digest. Before any candidate is published, a non-executing rootfs
snapshot check proves Linux ARM64, Python 3.13, Alpine 3.22, and the ARM64 musl
loader; another repository or runtime family fails closed.
A manual verification run with the toggle disabled retains the existing
source-only behavior.

Promotion mode grants package-write access only to its job, authenticates with
the workflow token, checks that its checkout is the exact clean revision,
builds the Linux ARM64 candidate without accepting an external candidate,
publishes it under a run-unique discovery tag, and derives `CANDIDATE_IMAGE`
only from the immutable digest and config digest returned by that same Buildx
push operation. It never pulls the mutable tag to select a digest. It then
pulls the derived immutable reference, binds the pulled config and rootfs to
the builder metadata, and exercises the media/FFmpeg and ARM64 `renameat2`
gates in a disposable test layer whose parent is that exact candidate. Only
then does it run the equivalent of:

```sh
make promotion \
  CANDIDATE_IMAGE=ghcr.io/<owner>/<repository>@sha256:<observed-candidate> \
  PREVIOUS_IMAGE=ghcr.io/<owner>/<repository>@sha256:<known-good> \
  BASE_IMAGE=python@sha256:<verified-base>
```

The bundle contains:

- `promotion-release-manifest.json`, preserving the existing source and image
  manifest fields while adding structured image labels and repo digests;
- `promotion.cdx.json`, the CycloneDX SBOM generated from the exact candidate;
- `promotion-vulnerability.json` and its raw dependency/image scan evidence;
- `base-image-provenance.json`, proving the locally pulled base digest and the
  Dockerfile build contract; the checker also requires the candidate's
  recorded root-filesystem layers to begin with the verified base layers;
- `candidate-images.compose.json` and `rollback-images.compose.json`, generated
  Compose overrides containing only exact image references;
- `rollback-metadata.json`, including exact candidate, prior image, services,
  source/Compose hashes, local image IDs, and argument-vector commands; and
- the hash-bound `compose.yaml` and legacy rollback readiness gate needed to
  execute and verify the first-rollout fallback without source reconstruction;
- `promotion-manifest.json`, which hash-binds all preceding evidence and is
  emitted as `approved` only after the offline consistency checks pass. The
  checker requires the release manifest, scanner, and rollback evidence to
  identify the same local candidate image ID.
- `candidate-build-metadata.json`, the Buildx push result used to select the
  candidate digest before any registry pull by digest. The promotion manifest
  hash-binds this file and requires its pushed digest and configuration digest
  to match the independently inspected candidate.

Run the checker again after transferring or before using an evidence bundle:

```sh
python scripts/promotion_manifest.py check \
  --manifest build/promotion-manifest.json
```

The checked rollback metadata is intentionally non-executing. An operator can
review its `commands.rollback` argument vector during the change council, keep
the known-good digest locally, and invoke the matching Compose override only if
the separately approved canary fails. Never replace that digest with a tag.
The candidate service set is exactly `photo-portal`, `chat-notifier`,
`audiobook-preparer`, `slideshow-worker`, `david-pi-maintenance`, and
`device-backup-worker`.

The first-rollout previous image predates `modules.slideshow_worker` and
`modules.device_backup_worker`, so the rollback contract must never assign that
image to either candidate-only worker. Schema-v2 rollback evidence instead
contains a hash-bound legacy profile. Its candidate override starts all six
services. Its rollback override pins the four legacy services to the exact
previous digest, leaves both candidate-only workers pinned to the exact
candidate digest behind the inactive `rollback-disabled` profile, and names
only the four legacy services in the rollback `up` command. Hold the existing
writer-transition lock for the complete rollback and readiness window. The
reviewed argument-vector sequence is fail-closed and must be followed in order:

1. stop `device-backup-worker` and `slideshow-worker` with the generated override;
2. update only the four legacy services to the previous digest; and
3. run the hash-bound `deploy/david-pi-legacy-writer-readiness` gate.

The legacy gate requires stable healthy/running identities for all four old
writers and requires both candidate-only workers to be absent or stopped. Do
not use the six-writer readiness gate for this legacy rollback and do not
replace the exact service-list command with a bare `docker compose up`. The
stop and update commands do not delete or rewrite `slideshow_jobs`, publication
intents, device uploads, secondary-copy state, media, volumes, or schema;
queued work remains dormant until compatible workers are promoted again. Any
failure in one sequence step blocks the next step and needs change-council
review. A future six-service rollback is allowed only after the previous digest
has separate, hash-bound compatibility evidence for both workers; never infer
compatibility from an image tag or from the candidate source tree.

Before first activation, inspect `slideshow_jobs`: any `working` row without the
new generation, token, owner, and bounded lease contract blocks rollout. Do not
adopt or rewrite that row during deployment; roll back and resolve it under a
separate data-change approval.

## Residual boundaries

- Registry authentication, digest resolution, image publication, Pi access,
  backups, canary identity checks for two independently admitted test users, and deployment are not
  performed by the local source tools. The hosted promotion job performs the
  first three only for its fixed GHCR package; it never connects to the Pi.
- Vulnerability results are a point-in-time statement tied to the scanner
  database available during CI. Re-run the workflow for a later promotion.
- The evidence is integrity-linked but not signed. Keep GitHub artifact access
  controlled; add an external signing/attestation system before treating a
  bundle as independently non-repudiable.
- The hosted runner, GitHub Actions definitions, GitHub token, package ACLs,
  resolved base image, and package registry are trusted-builder boundaries.
  The workflow proves which clean source and observed digest it handled; it
  does not claim byte-for-byte reproducible builds.
- Development Compose tags remain compatible. The generated digest-only
  override is mandatory for production promotion and rollback.
