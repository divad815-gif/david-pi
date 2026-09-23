# Verification status for the unpublished portable candidate

This is a working implementation record, not a stable-release receipt. Source
continues to change. Only `docs/release-evidence/stable.json`, validated against
the final exact source and APK, can satisfy publication gates.

Completed during implementation:

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

Still required for stable publication:

- Complete installation through real Tailscale enrollment and browser ownership
  claim on both supported VM operating systems, including reboot and failures.
- Final exact-source AMD64 and ARM64 image/runtime verification and SBOMs.
- Failed-update recovery and clean independent backup restoration with actual
  application content checks, including the final v9.22.2 release fixture.
- Final signed Android artifact validation and physical-device backup, offline,
  reconnect and in-place update acceptance.
- Physical Raspberry Pi 4 and Pi 5 installation/runtime checks.
- Mobile/accessibility checks and a newcomer completing the published guide
  without undocumented assistance.

No live deployment, image push, GitHub release or downloadable preview is
claimed by this record. Local VM logs and private signing material are excluded
from public artifacts.
