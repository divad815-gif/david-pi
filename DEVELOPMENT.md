# David-Pi development and release workflow

The tracked source is the production-matching baseline for David-Pi. Runtime data,
credentials, databases, generated Android output, and machine-local configuration
must never be committed.

## Bootstrap

```sh
make bootstrap
make test-fast
```

The Android download is a signed release artifact, not a debug build. Release
verification requires the policy-pinned Android SDK Build Tools 35.0.0 `aapt`
and `apksigner` (selected from `ANDROID_SDK_ROOT`/`ANDROID_HOME`, or configured
with `ANDROID_AAPT` and `ANDROID_APKSIGNER`). Its tracked attestation binds the APK hash, package
version, source version, and established signer fingerprint. If any differ,
the portal does not advertise or serve the download and release verification
fails closed.

The signing keystore and credentials remain outside the repository. A recovery host can
use an existing protected properties file without rewriting it:

```sh
export JAVA_HOME=/protected/temurin-17.0.20.1
export ANDROID_JAVA="$JAVA_HOME/bin/java"
export PATH="$JAVA_HOME/bin:$PATH"
python scripts/android_release.py verify-java
SOURCE_SHA256="$(python scripts/android_release.py source-hash)"
DAVID_PI_ANDROID_SOURCE_SHA256="$SOURCE_SHA256" \
DAVID_PI_ANDROID_KEYSTORE_PROPERTIES=/protected/keystore.properties \
DAVID_PI_ANDROID_KEYSTORE_FILE=/protected/david-pi-backup-release.jks \
  ./clients/android/gradlew -p clients/android clean assembleRelease
install -m 0644 \
  clients/android/app/build/outputs/apk/release/app-release.apk \
  artifacts/android/david-pi-backup.apk
python scripts/android_release.py create \
  --output artifacts/android/david-pi-backup.manifest.json
python scripts/android_release.py verify
```

Run that sequence without editing Android source between `source-hash` and
`verify`, and keep the same policy-pinned `ANDROID_SDK_ROOT` (or explicit tool
paths) for attestation creation and verification. Promotion remains blocked
until both the fixed artifact path and sidecar pass the final command.

The host test suite intentionally excludes the real FFmpeg integration test because
FFmpeg is not installed on every workstation. `make test-media` builds the same
Alpine 3.22/musl Python environment as production and runs the FFmpeg and
race-safe audiobook-cleanup ABI tests inside the image.

## Required release verification

```sh
make verify IMAGE=david-family-photos:<candidate-tag>
```

Verification performs a tracked-file secret scan, fast Python and JavaScript tests,
the containerized ffmpeg integration test, an OCI-labeled runtime image, a
CycloneDX software bill of materials, a deterministic release manifest, and
fixed-version Trivy dependency/image scans. The scan fails closed when Trivy or
its package coverage is unavailable and blocks all `HIGH`/`CRITICAL` findings.
A production candidate must additionally pass the identity, database-integrity,
backup/restore, ARM64, browser, Android, and canary gates appropriate to its change.
The stopped-container gate compares every application file under `/app`, the
entrypoint content, and the effective user, command, working directory, ports,
and environment contract with the source tree. Release evidence records the
remaining image relationship as a `trusted_builder_declaration` with
`reproducible_build: false`: it does not independently prove arbitrary rootfs
changes made by Docker build instructions. Candidates therefore must be built
by the clean hosted release workflow from the recorded commit and pinned base,
not by an untrusted local builder.

Secret screening rejects tracked credential patterns and Android build inputs
that are ignored, use credential-like filenames, or contain high-confidence
credential signatures (including packaged APK entries). This is a release gate,
not a mathematical proof that arbitrary bytes contain no secret; release
evidence records that limitation explicitly.

The `Verify release candidate` workflow runs the same `make verify` contract for
every pull request and main-branch update in a clean hosted runner. It preserves the
generated SBOM, release manifest, normalized scan, and raw scanner evidence for
30 days. A green workflow is necessary but does not replace the Pi-specific
recovery, identity, canary, and rollback gates.

Production promotion additionally requires digest-only candidate, known-good,
and verified base-image references. The complete non-deploying evidence contract,
manual workflow, and exact rollback metadata are documented in
[`deploy/RELEASE_PROMOTION.md`](deploy/RELEASE_PROMOTION.md).

## Release rules

- Build and deploy by immutable image digest, never by an unverified mutable tag.
- Archive the SBOM, release manifest, vulnerability evidence (including raw scan
  JSON), and promotion/rollback manifests with every production release decision.
- Keep the current and previous known-good image digests and exact rollback command.
- Use one risk domain per production window.
- Schema work must be additive, checksummed, rehearsed on copies, and council-approved.
- Never include `.env`, secret files, databases, uploads, or media in source archives.
- Production smoke tests are read-only unless an approved disposable fixture exists.

Android's Gradle wrapper distribution is checksum-pinned. Release policy pins
the `aapt` executable and its SDK `libc++.so` companion, plus both the
`apksigner` launcher and implementation JAR. Before any verifier code runs, the
release tool copies those inputs into a private closed layout and authenticates
them; it also authenticates a closed inventory of the pinned Java runtime's
`bin`, `conf`, and `lib` trees. It executes only those snapshots with loader and
Java injection variables removed, then verifies that the snapshots did not
change. Absolute source paths are informational, so identical authenticated
tools remain verifiable on a recovery or hosted runner. The operating-system
kernel and system libraries used beneath that closed verifier bundle remain a
trusted host boundary.
The current Gradle project does not yet carry dependency-verification metadata
or a dependency lockfile, so resolution of the exact declared Maven coordinates
remains a trusted clean-builder/network assumption. Do not describe the APK as
reproducible until a second isolated build is byte-identical and that dependency
gate is added.
