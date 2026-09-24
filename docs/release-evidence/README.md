# Publication receipts

## Stable release

No stable receipt exists yet. Missing, skipped, stale, simulated, or failed checks block stable publication. Workflow artifacts are not a public preview channel. The separately gated testing release below exists to collect physical-device and newcomer feedback; it does not satisfy stable acceptance.

Run `python3 scripts/stable_release_gate.py --source-digest` after the implementation is complete. Perform every check listed in `REQUIRED` in that script against this exact source and APK. Retain original logs/screenshots in private storage without publishing household data or credentials. Record their SHA-256 hashes in `stable.json` alongside version, source digest, APK digest, reviewer, reviewed_at, and a checks array. Each check records id, expected environment, status, source_sha256, performed_by, completed_at, notes, evidence_sha256, and simulated=false. Do not create receipts from unit-test success or from this example.

A named release reviewer must inspect those records and the corresponding private evidence. Technical receipt validation checks completeness and source identity; it cannot prove that a human carried out a test. Any code or guide change invalidates the source digest, so repeat affected acceptance and explicitly review the complete evidence set before publication.

## Testing prerelease

Use `scripts/testing_release_gate.py --source-digest` after the source is frozen.
The source inventory excludes receipt files in this directory except this
README, avoiding a circular digest. Do not fabricate success receipts from
unit-test fixtures. A current passing gate is necessary for the local
[testing publication procedure](../TESTING.md); building a candidate does not
publish it.

The reviewed JSON document has these fields:

- `schema_version`: integer `1`; `kind`: `testing-release-acceptance`.
- `version`: the exact canonical beta in VERSION, such as `10.0.0-beta.1`.
- `source_sha256`, `android_apk_sha256`: exact screened source and signed APK.
- `reviewer`, `reviewed_at`: reviewer identity suitable for a public receipt and
  an ISO-format timestamp. Keep personal household identities out of the record.
- `known_limitations`: a nonempty list of clear, practical limitations included
  in release notes. Distinguish emulator coverage from physical devices and
  explicitly describe incomplete modern-WebView/offline checks.
- `pending_acceptance`: exactly `hardware-pi4`, `hardware-pi5`,
  `android-pair-backup-offline-reconnect`, `android-signed-update`, and
  `newcomer-unaided-install`. These remain pending in this beta path.
- `checks`: exactly one actual passing result for each technical check below.

| Check ID | Required environment |
| --- | --- |
| `vm-debian13-amd64` | `full-vm` |
| `vm-ubuntu2404-amd64` | `full-vm` |
| `install-resume-reboot` | `full-vm` |
| `household-admission-isolation` | `integration` |
| `local-modules-enable-disable` | `integration` |
| `storage-update-recovery` | `full-vm` |
| `independent-backup-clean-restore` | `full-vm` |
| `browser-setup-accessibility` | `browser` |
| `android-signed-emulator-smoke` | `emulator` |

Each check records `id`, `environment`, `status: "pass"`, exact `source_sha256`,
`implementation: "actual"`, `performed_by`, `completed_at`, `notes`,
`fixture_scope`, `limitations` (a list), and the SHA-256 of retained original
evidence as `evidence_sha256`. Full-VM checks additionally require
`actual_services: true`, `actual_filesystems: true`, and `default_timings: true`.
Fresh OS checks also require `real_tailscale: true`. The signed emulator check
records the same `android_apk_sha256` as the release. The household check requires
`real_admission_smoke: true` in addition to its described isolation fixture.

The real VM tests must use shipped services and actual filesystems, including
fresh installation at default timings, interruption/resume/reboot, storage
pressure and update recovery, and independent restore onto a clean target with
actual content checked. Do not substitute mocked install/update/restore code or
extended test-only startup deadlines. Architecture runtime/media checks and
signed APK source/signature verification are also performed by the local
candidate tool and retained in its separate runtime receipt.

Synthetic household content is expected. For household negative/isolation tests,
clearly identify any private test-only identity transport in `fixture_scope`
and its limits; it does not prove real Tailscale admission. Installation must
include real Tailscale enrollment/access, with a separate real admission smoke
recorded in the household check. Provider faults can be simulated only when
their scope is disclosed; do not relabel them as successful live provider tests.
An API 28 signed emulator check can cover native pairing/backup/continuity while
recording modern offline and physical-device limits. A reviewer must inspect
the underlying evidence; schema validation alone cannot establish that a test
was performed honestly or thoroughly.
