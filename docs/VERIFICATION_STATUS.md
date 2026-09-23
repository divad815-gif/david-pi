# Verification status for the portable development candidate

This is a working implementation record, not a stable-release receipt. Source
continues to change. Only `docs/release-evidence/stable.json`, validated against
the final exact source and APK, can satisfy publication gates.

The implementation checks below were completed at earlier source revisions.
They establish progress, not acceptance of every subsequent commit. The final
release must repeat its required checks against the exact artifacts it publishes.

## Implementation checks

- The complete Python suite passed 1,238 tests and 409 subtests. Its single
  generic-host ABI skip is covered by explicit architecture image checks below.
  The browser JavaScript suite passed 232 tests.
- Installed official Ubuntu QEMU x86/ARM, cloud-image tools, buildx, Compose and
  user-emulation packages on the development host.
- Downloaded official Debian 13 and Ubuntu 24.04 AMD64 cloud images and checked
  them against their publishers' checksum documents.
- Booted independent full Debian 13.7 and Ubuntu 24.04.5 guests using QEMU software
  emulation. Verified cloud-init completion, SSH access, OS identity and isolated
  32 GiB OS / 12 GiB data / 12 GiB backup virtual disks.
- Built AMD64 and ARM64 application/runtime-test images locally. Both runtimes passed
  four real media-rendering tests and six kernel-ABI/cleanup checks. ARM64 ran
  under emulation; AMD64 ran natively with the corrected architecture assertion.
- Generated architecture-specific CycloneDX SBOMs from both runtime images,
  inventorying 38 installed Python distributions each without reinstating pip.
- Ran real Debian VM filesystem checks for prepared ext4/UUID storage, separate
  backup snapshots, missing-drive refusal, restoration, keys and ownership.
  Docker operations were stubbed in this subset, so it is not full installation
  or application recovery acceptance.
- Started the actual local application image under Docker inside both Debian
  13.7 (Docker 26.1.5) and Ubuntu 24.04.5 (Docker 29.1.3).
  All six selected services became healthy; health/readiness, named home, manual
  Movie Night, Recipes, Notes and status endpoints returned success. An unadmitted
  synthetic identity was denied, disabled Pi-hole remained neutral, and skipped
  backup configuration remained visible. Because QEMU has no hardware acceleration,
  this run used healthcheck timing overrides of 120-second timeout, 60-second
  interval and 300-second startup period; production startup timing is unverified.
  Identity headers were synthetic and loopback-only. This was not real Tailscale
  enrollment, ownership claim or full installation acceptance.
- Ran an upgrade against the exact public v9.22.2 source in a disposable fixture:
  notes, files, photos, recipes, movie lists and encrypted chat retained content
  and household privacy. A second startup made no additional schema changes;
  the original recovery fixture remained intact. This is an application migration
  check, not a complete interrupted host-update acceptance.
- Built and verified the signed version 27 Android APK using the recovered
  original certificate and pinned official toolchain. Passed 59 JVM unit tests,
  12 API 28 emulator instrumentation checks and 100 focused Python tests. An
  actual signed version 24 → 27 emulator update retained seeded app data and
  launched successfully. API 35 emulator initialization failed before app tests;
  real Tailscale pairing and physical-device workflows remain separate gates.
- Fixed a real VM startup failure where source archive permissions prevented the
  separate maintenance user from importing application code. Both architecture
  images now pass an import check as that restricted user.
- Checked a 375 px browser layout for settings and created/read a manual Movie
  Night fixture with all providers skipped and the chosen John-Pi display name.
  This is a focused browser check, not complete accessibility acceptance.
- Exercised release provenance, publication-gate refusal, portable Compose,
  maintenance isolation and collector contracts with targeted automated tests.

## Assisted fresh-install walkthrough

A later walkthrough completed a fresh Debian 13 installation in a disposable
full Linux VM. The user approved real Tailscale enrollment and completed the
private HTTPS browser wizard. This exercised the following behavior:

- Official prerequisite packages installed with signature verification enabled.
- Setup used the hostname actually assigned by Tailscale, including a collision
  suffix, and kept it independent from the chosen website display name.
- `sudo david-pi setup` renewed the one-use claim without repeating installation
  questions or changing the saved installation identity.
- The user selected the timezone and detected prepared ext4 data/backup drives
  showing available capacity. Storage discovery needed a correction during the
  walkthrough to handle the setup service's isolated mount view.
- The five-stage progress display reached ready, and the user opened the portal
  with the chosen display name. The browser title and web-app manifest agreed.
- All six selected services ran; all four declared Docker health checks passed.
  The two preparers do not declare separate container health checks. The native
  `sudo david-pi verify` check and authenticated HTTPS readiness checks passed.
- Local modules worked with optional provider connections skipped. A separate
  backup destination was configured and correctly reported **restore unverified**.

This was assisted testing, not a clean acceptance pass for the final candidate.
The installer and image began at `d83cec0`; helper correction `539c135` was applied
during setup while preserving the existing claim session. Downloads used a
private local HTTPS source and a verified local image export, so public GitHub
and registry delivery were not tested. Software emulation required longer
startup and healthcheck timeouts without disabling readiness checks.

The subsequent source change `7323335` removes duplicate service startup during
fresh installation. Its installer suite passed 170 tests with one root-only
skip; that change still needs a new image and an unmodified fresh-install run.
The working test server was left on its successfully verified configuration.

## Source-publication checks

Before opening the development pull request, the current full Python suite
passed 1,319 tests and 409 subtests. Two checks were skipped because they require
an architecture-specific release image or an isolated root test environment.
The JavaScript suite passed 248 tests. Eight additional privacy-scanner tests
passed after removing inherited household labels from the scanner's source.
Personal denylist values now stay in an excluded local file.

ShellCheck and shell syntax checks passed after correcting argument quoting
and a conditional in the legacy chat verification script. Both Compose
configurations validated. Public-source and credential checks screened the
current tree, and the unpublished commit history was reviewed for new private
content. These are source-publication checks, not stable-release receipts.

## Still required for stable publication

- Unmodified final-candidate installation through real Tailscale enrollment and
  browser ownership claim on both supported VM operating systems, including
  normal download delivery, reboot and failure scenarios.
- Final exact-source AMD64 and ARM64 image/runtime verification and SBOMs.
- Failed-update recovery and clean independent backup restoration with actual
  application content checks, including the final v9.22.2 release fixture.
- Final signed Android artifact validation and physical-device backup, offline,
  reconnect and in-place update acceptance.
- Physical Raspberry Pi 4 and Pi 5 installation/runtime checks.
- Mobile/accessibility checks and a newcomer completing the published guide
  without undocumented assistance.

Publishing development source and a pull request does not satisfy these gates.
No live deployment, published container image, GitHub package release or
downloadable preview is claimed by this record. Local VM logs, account details,
private addresses and signing material are excluded from public source.
