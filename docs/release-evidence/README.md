# Stable publication receipts

No stable receipt exists yet. Missing, skipped, stale, simulated, or failed checks block publication. Workflow artifacts are not a public preview channel.

Run `python3 scripts/stable_release_gate.py --source-digest` after the implementation is complete. Perform every check listed in `REQUIRED` in that script against this exact source and APK. Retain original logs/screenshots in private storage without publishing household data or credentials. Record their SHA-256 hashes in `stable.json` alongside version, source digest, APK digest, reviewer, reviewed_at, and a checks array. Each check records id, expected environment, status, source_sha256, performed_by, completed_at, notes, evidence_sha256, and simulated=false. Do not create receipts from unit-test success or from this example.

A named release reviewer must inspect those records and the corresponding private evidence. Technical receipt validation checks completeness and source identity; it cannot prove that a human carried out a test. Any code or guide change invalidates the source digest, so repeat affected acceptance and explicitly review the complete evidence set before publication.
