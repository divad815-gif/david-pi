# Portable release status and maintainer procedure

**Next testing target: 10.0.0-beta.1. Stable target: 10.0.0.** Both require an
explicit publication step; a source branch or CI build is not a release. The
[testing prerelease procedure](TESTING.md) permits willing friends to provide
physical-device and newcomer feedback after the technical beta checks pass.
No live server deployment is part of packaging. Real VM installation/recovery,
signed Android/device checks, physical Pi 4/5 checks, accessibility and newcomer
execution remain required before stable publication. Unit tests and successful
builds do not satisfy those stable gates.

## Reconciliation

The public v9.22.2 installer/history and AGPL license are retained. The portable
application reconciles newer live portal source and worker code into one source
and release image, so workers cannot silently remain on an older portal build.
No live databases, API keys, Tailscale identities, private operational records or
signing secrets belong in source archives or image layers.

The observed maintenance failure was a schema mismatch: the newer collector
emitted `access_control`, while the worker's observation allowlist rejected that
subsystem and marked metrics `observation_invalid`. Preserve strict privacy and
freshness checks while accepting the supported aggregate subsystem. A running
heartbeat alone is not evidence of healthy metrics or successful recovery.

## Release evidence

Testing and stable releases have separate gates. The local
`scripts/testing_release.py` procedure verifies the existing signed APK and
promotes the exact tested AMD64/ARM64 images without rebuilding or resigning.
It requires `scripts/testing_release_gate.py` to pass, publishes only a canonical
`X.Y.Z-beta.N` version, and creates a GitHub prerelease with `latest=false` and no
`latest` image tag. Normal installation/update checks do not select a beta.
Its separate manual hosted preflight is read-only. When local package-write
credentials are unavailable, a gated draft transfer and checksum-bound annotated
beta tag trigger the separate testing promotion workflow. That workflow imports
and uploads the same OCI bytes with the repository token; it does not rebuild
images or sign Android. No Android signing secrets are needed in GitHub for
this testing delivery path. See [testing instructions
and maintainer procedure](TESTING.md).

Run `python3 scripts/stable_release_gate.py`. It fails closed until
`docs/release-evidence/stable.json` contains every required real result, matches
the exact screened source digest, and names the exact signed APK hash. See the
[evidence format](release-evidence/README.md) and [VM harness](../tools/vm/README.md).
Physical Android, physical Pi, and unaided newcomer tests must be carried out and
reviewed; no agent should create synthetic success receipts for them.

The stable workflow is manual-only on `main`. Configure a protected
`stable-release` GitHub environment and signing secrets before use. The APK
keystore and properties are supplied as `ANDROID_KEYSTORE_BASE64` and
`ANDROID_KEYSTORE_PROPERTIES_BASE64`; decode only into runner temporary storage.
Keep the original certificate identity. Pinned Android verifier tools/runtime
must validate before signing evidence is accepted. If the official toolchain
changes, review and update its provenance separately; do not bypass verification.

Public Android attestations use schema 2 and contain verified tool hashes and
versions, without build-machine paths. Regenerate any schema-1 attestation with
the pinned verifier tools before packaging; release validation rejects the old
format. Keep full local tool-inspection evidence private. Regenerating metadata
does not require resigning an unchanged APK.

Provide an immutable official Python 3.13 Alpine 3.22 multiarchitecture digest.
Build and test both AMD64 and ARM64, produce architecture-specific SBOMs using
Python distribution metadata (runtime pip is intentionally removed), and verify
base-image provenance and the platform's musl loader. Local promotion evidence
must match candidate/base architecture and compatible rollback architecture.

Only after source tests, signed APK verification, runtime checks and acceptance
gates pass may the workflow log into GHCR and publish stable image digests and a
GitHub release. Assets include source archive, verified bootstrap, checksums,
release manifest, signed APK plus attestation, SBOMs and sanitized acceptance.
The release manifest carries `DATA_SCHEMA_VERSION=1` and
`ROLLBACK_MIN_DATA_SCHEMA=1` so installers can reject incompatible operations.

Local source archives are reproducibly ordered and include the screened public
file set, not a copy of the entire working directory. VM disks, caches, personal
files and signing secrets are excluded. Run both public/privacy scanners and
inspect the archive listing before publishing. Tagging alone must not publish.
