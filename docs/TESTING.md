# Try the David-Pi beta

**Testing version: 10.0.0-beta.1.** Files become available only after technical
acceptance and explicit publication. A source branch or successful CI run is
not a downloadable release. Use the command below only once this exact version
appears as a **Pre-release** on the
[GitHub releases page](https://github.com/divad815-gif/david-pi/releases).

This beta lets willing testers try installation and the Android companion before
stable release. Use a separate test machine and content you can replace. Keep an
independent copy of anything you want to retain. Do not upgrade an existing
household server to this beta.

## Before you begin

Use a supported 64-bit Linux system: Debian 13 on an AMD64 PC, Ubuntu Server
24.04 on an AMD64 PC, or Raspberry Pi OS based on Debian 13 on a Pi 4/5.
Physical Pi 4/5 testing is still pending; an ARM64 container test does not prove
the experience on a Pi. The [installation guide](INSTALL.md) lists the machine
and storage checks. Windows, WSL, and network shares are not supported hosts or
storage destinations for this release.

You need access to a Tailscale account. Your browser device and the test server
must connect to the same test network. Plan to admit each household person
explicitly; network access does not make someone an administrator. Follow the
[illustrated Tailscale guide](TAILSCALE.md) for sign-in, certificates, and the
separate identity used to claim the server. No external movie or recipe account
is required: you can skip every optional integration.

## Install the explicitly selected beta

Open a terminal **on the test server**, or connect to it using an interactive
SSH session. Run:

```bash
curl --fail --location --proto '=https' --tlsv1.2 https://github.com/divad815-gif/david-pi/releases/download/v10.0.0-beta.1/install.sh | sudo bash
```

This version-specific installer selects **10.0.0-beta.1**, verifies its source
archive, and uses its immutable container image. It does not use GitHub's latest
stable download. If the exact release is absent, stop and check the releases
page; do not substitute an older installer from the source branch.

The terminal guides you through Tailscale sign-in and prints the actual HTTPS
setup address and a separate one-use claim code. Keep it open so you can follow
progress and recovery instructions. Open the printed link on your connected
browser device. Enter the claim code in the wizard; it is deliberately not
included in the URL.
Choose the display name, timezone, detected storage, modules, and optional
services. The hostname assigned by Tailscale may include a collision suffix;
use the address the installer prints. See [installation](INSTALL.md),
[storage](STORAGE.md), and [household access](HOUSEHOLD.md) for the screens and
choices.

If setup stops or your claim expires, follow [troubleshooting](TROUBLESHOOTING.md).
Recovery differs depending on whether verified setup has been saved. Do not
delete configuration files, change identity records by hand, or restart with
different release files to make an error disappear.

## Android and optional services

Download **david-pi-backup.apk from the same beta release**. Follow the
[Android guide](ANDROID.md) for Tailscale access, pairing, permissions, backups,
offline content, and updates. The APK uses the retained signing identity;
its source attestation and checksum are adjacent release assets. Installing
this file should preserve an existing installation with that signing identity,
but physical-device update acceptance remains pending and is part of this beta.

An obsolete Android WebView shows a native explanation instead of opening a
broken portal. Update Android System WebView/Chrome using your device's supported
update mechanism. The API 28 emulator smoke test does not establish modern
WebView playback/offline behavior, every manufacturer's background scheduling,
or physical-device reliability. Native Firebase chat alerts are unavailable.

Movie Night can use a manual watchlist, and Recipes can keep local recipes
without credentials. [Optional services](INTEGRATIONS.md) explains accounts,
data requests, costs, country-specific availability, and skipping integrations.
Synthetic provider-failure tests do not prove that a live provider account works
in every country. Report the selected country and provider when sharing results;
never share API keys or subscription passwords.

## Updates, backups, and leaving the beta

Normal website update checks select **stable releases only**. Beta users are not
automatically moved to another beta. To deliberately install a later published
beta on an existing beta installation, read that release's notes and use the
server terminal:

```bash
sudo david-pi update --version 10.0.0-beta.2
```

The version above is an example, not a claim that beta.2 exists. Select only a
published compatible version. This command uses the verified update process and
its mandatory recovery snapshot. Rerunning the first-install command is not an
update procedure. A stable installation cannot enroll in beta through this
command. A newer compatible stable release can be selected through ordinary
updates when available; older releases are not a supported downgrade.

Read [updates](UPDATES.md), [backup and restore](RECOVERY.md), and
[uninstall](RECOVERY.md#update-snapshots-and-uninstall) before testing recovery. Routine backups and the local
snapshot used by an update serve different purposes. Preserve the original
backup when testing restore on a separate machine. Do not copy a snapshot over
newer activity automatically.

## Feedback checklist

Try what is practical for you and record **pass, failed, or not tried**. You do
not need to finish every item to provide useful feedback.

- Install using only these published instructions. Record where you paused,
  guessed, needed help, or could not find a link. Describe the expected next step.
- Use your chosen name throughout the portal, browser title, installed web app,
  and Android pairing. Try timezone and detected storage dropdowns.
- Skip external services. Use a manual watchlist, local recipes, text chat,
  notes, and files. Disable and re-enable a module and check retained content.
- Admit a separate household member. Check shared content, private content, and
  administrator-only settings using the appropriate separate identities.
- Restart the server and reconnect. Note how clear progress, errors, expired
  claims, and the documented recovery instructions are.
- On Android, record device model, Android version, WebView version, pairing,
  permissions, interrupted upload/resumption, scheduled backups, offline books,
  listening progress, reconnecting, and installing a later signed update.
- On a Pi, record Pi model, OS version, storage connection, installation,
  restart, playback, and background work. Report observed behavior rather than
  assuming the AMD64 or emulated ARM64 result applies.
- Try a narrow phone screen, enlarged text, and keyboard navigation. Describe
  text you cannot read, controls you cannot reach, and unclear recovery messages.

Use the repository's [issue tracker](https://github.com/divad815-gif/david-pi/issues)
for sanitized feedback. Include beta version, machine/OS, the step, expected and
actual behavior, and whether you followed the guide without help. Remove claim
links/tokens, real tailnet addresses, email addresses, API keys, private content,
and household screenshots before posting. Prefer the displayed error and a
short redacted excerpt to a complete log or database.

## Maintainer: prepare and publish the same tested candidate

This section is not needed to install the beta. The manual **Testing release
source preflight** workflow is read-only, uses a separate `testing-release`
environment, and does not upload artifacts or signing keys. Normal CI keeps its
source tests, debug Android build, and unpublished multiarchitecture builds.
The [stable workflow and its acceptance gates](RELEASE.md) remain separate.

The local maintainer machine needs Python 3.13 with the repository dependencies
and pytest, Git, Docker/Buildx with an OCI-capable image store, and native or
emulated execution for both architectures. Use the original verified signed APK
and schema-2 attestation in `artifacts/android/`. Set the existing pinned Android
SDK/JDK verifier locations: `ANDROID_SDK_ROOT` identifies the SDK and
`ANDROID_JAVA` identifies the JDK's `bin/java` executable. They must match the
[pinned release policy](../config/android-release.json).
The tool verifies these inputs; it never signs or rebuilds Android. Keep signing
credentials and the optional private-marker file outside tracked source.

After committing the reviewed source, choose a new directory under ignored
`work/` and the verified official Python multiarchitecture base digest:

```bash
.venv/bin/python scripts/testing_release.py build \
  --python-base python@sha256:REPLACE_WITH_VERIFIED_BASE_DIGEST \
  --output work/beta-1-candidate \
  --private-markers work/private-release-markers.txt
```

The placeholder deliberately is not runnable. Do not replace an immutable base
with a floating tag. `--private-markers` is optional for a clean distribution
checkout; use the maintained private denylist when reconciling household source.
The tool builds sequentially, verifies source/APK bytes, runs media/ABI checks
on both architectures, checks the separate maintenance user, exports and checks
every OCI blob and runtime layer, and produces SBOM/base provenance. Full logs
and original source paths remain in the private output directory.

Perform the required real technical checks against these exact candidates and
record reviewed [testing acceptance](release-evidence/README.md). No tool can
turn unit fixtures or a missing human test into actual acceptance. Retain the
private evidence files named by their hashes. Physical/newcomer tests are
explicitly pending; disclose the practical limits in `known_limitations`.

For the local delivery tool, keep the completed evidence under ignored `work/`
or outside the checkout. Its clean-commit check also rejects uncommitted receipt
files. Do not make another commit between candidate preparation and publication.
Review sanitized evidence as a release asset rather than changing already-built source.
Run the publication dry run:

```bash
.venv/bin/python scripts/testing_release.py publish \
  --output work/beta-1-candidate --repository divad815-gif/david-pi \
  --evidence work/reviewed-testing-acceptance.json \
  --private-markers work/private-release-markers.txt
```

Publication requires this exact source commit already on GitHub, an authenticated
GitHub CLI with release-write access, and Docker authentication with GHCR package
write access. The package must permit anonymous pulls. No token belongs in a
command example, source file, public receipt, or uploaded asset. Add `--execute`
only after reviewing the dry run and technical gates. The tool pushes the tested
images without rebuilding, verifies their registry manifests, combines only
AMD64/ARM64, tests anonymous manifest access, creates a draft prerelease, downloads
and compares its assets, then exposes it with `--prerelease --latest=false`.
It downloads and verifies the public assets again without GitHub credentials.
The private publication receipt distinguishes a still-private draft from an
already-public release whose final download check failed.
No `latest` image tag is created.

A failed publication may leave versioned images or a draft release. It refuses
to overwrite an existing version. Inspect the failure and remote state before
deciding how to proceed; do not repeatedly rerun a failed write or publish the
draft by hand to bypass verification. Preparing candidates is not publication,
and successful publication is not stable acceptance.

### When the local login cannot upload container packages

Use the gated Actions handoff instead of creating a new personal package token.
It uses the repository's `GITHUB_TOKEN` for package writes and needs no signing
keys. Prepare the candidate and reviewed evidence as above, then run:

```bash
.venv/bin/python scripts/testing_transfer.py stage \
  --output work/beta-1-candidate --repository divad815-gif/david-pi \
  --evidence work/reviewed-testing-acceptance.json \
  --private-markers work/private-release-markers.txt
```

This is a dry run. It creates a deterministic, allowlisted transfer containing
the tested OCI bytes, signed APK, screened source and sanitized receipts. It
excludes private build logs and tool paths. After review, repeat with `--execute`
to upload that same transfer into a private draft prerelease. Review the generated
`testing-tag-message.json`, then deliberately create and push the annotated tag:

```bash
git tag -a v10.0.0-beta.1 -F work/beta-1-candidate/testing-tag-message.json
git push origin refs/tags/v10.0.0-beta.1
```

Run those commands only from the exact clean candidate commit. The tag binds its
version, source revision and transfer checksum. Configure the `testing-release`
environment to permit this beta tag. The **Promote tested beta** tag workflow
works from that commit without requiring an earlier merge to `main`. It checks
the staged checksum, technical gate, source/APK provenance and all OCI blobs,
imports the existing images, and pushes each selected platform without rebuilding.
It replaces only its own transfer asset with the final verified downloads while
the release is still a draft, then performs the same prerelease and anonymous
download checks. A plain/lightweight tag or a tag without this matching private
draft cannot publish. Do not push the beta tag before staging is complete.
