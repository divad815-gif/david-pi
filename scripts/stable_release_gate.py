#!/usr/bin/env python3
"""Fail closed unless the exact public source and signed APK passed acceptance.

Evidence is reviewed attestation, not a substitute for performing the checks.
Logs are local or private; the public evidence bundle must be content-neutral.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    'vm-debian13-amd64': 'full-vm',
    'vm-ubuntu2404-amd64': 'full-vm',
    'runtime-arm64': 'container-arm64',
    'hardware-pi4': 'physical-pi',
    'hardware-pi5': 'physical-pi',
    'fresh-install': 'full-vm',
    'naming-membership-isolation': 'full-vm',
    'optional-integrations-skipped': 'full-vm',
    'module-disable-reenable': 'full-vm',
    'provider-failure-recovery': 'integration',
    'interrupted-install-reboot': 'full-vm',
    'missing-full-storage': 'full-vm',
    'upgrade-v9.22.2': 'full-vm',
    'update-failure-rollback': 'full-vm',
    'independent-backup-clean-restore': 'full-vm',
    'android-pair-backup-offline-reconnect': 'physical-android',
    'android-signed-update': 'physical-android',
    'accessibility-mobile-keyboard': 'browser',
    'newcomer-unaided-install': 'human',
}
EXCLUDED_ROOTS = {'work', 'outputs'}
EXCLUDED_PARTS = {'.git', '.venv', 'venv', 'build', 'dist', '__pycache__', '.pytest_cache', '.gradle', '.kotlin'}
EXCLUDED_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.pem', '.key', '.jks', '.keystore', '.qcow2', '.img', '.iso', '.pyc'}


def source_files(root: Path) -> list[Path]:
    raw = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root)
    files = []
    for name in sorted(set(raw.decode().split('\0')) - {''}):
        path = Path(name)
        # Local scratch belongs only to these repository-root directories.
        # Nested names can be real source packages, such as Android's work/.
        if path.parts[0] in EXCLUDED_ROOTS or any(p in EXCLUDED_PARTS for p in path.parts) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        if path.name.startswith('.env') and path.name != '.env.example':
            continue
        if path.parts[:2] == ('artifacts', 'android'):
            continue
        if path.parts[:2] == ('docs', 'release-evidence') and path.name != 'README.md':
            continue
        full = root / path
        if full.is_symlink():
            raise ValueError(f'source symlink is not permitted: {path}')
        if full.is_file():
            files.append(path)
    return files


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for relative in source_files(root):
        digest.update(relative.as_posix().encode() + b'\0')
        digest.update(hashlib.sha256((root / relative).read_bytes()).digest())
    return digest.hexdigest()


def validate(document: dict, root: Path, *, require_android=True) -> list[str]:
    errors = []
    if document.get('schema_version') != 1:
        errors.append('unsupported evidence schema')
    version = (root / 'VERSION').read_text().strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        errors.append('VERSION must name a stable semantic version')
    if document.get('version') != version:
        errors.append('evidence version differs from VERSION')
    expected_source = source_digest(root)
    if document.get('source_sha256') != expected_source:
        errors.append('acceptance evidence does not match the current public source')
    if not document.get('reviewer') or not document.get('reviewed_at'):
        errors.append('evidence requires a named reviewer and review timestamp')
    checks = document.get('checks')
    checks = checks if isinstance(checks, list) else []
    for name, environment in REQUIRED.items():
        matches = [c for c in checks if isinstance(c, dict) and c.get('id') == name]
        if len(matches) != 1:
            errors.append(f'{name}: exactly one receipt is required')
            continue
        check = matches[0]
        if check.get('status') != 'pass' or check.get('environment') != environment:
            errors.append(f'{name}: real {environment} pass is required')
        if check.get('source_sha256') != expected_source:
            errors.append(f'{name}: receipt belongs to different source')
        if not check.get('performed_by') or not check.get('completed_at') or not check.get('notes'):
            errors.append(f'{name}: receipt must identify tester, date and result')
        if not re.fullmatch(r'[a-f0-9]{64}', str(check.get('evidence_sha256', ''))):
            errors.append(f'{name}: receipt must hash the retained evidence')
        if check.get('simulated') is not False:
            errors.append(f'{name}: simulated or unspecified execution is not acceptance')
    if require_android:
        apk = root / 'artifacts/android/david-pi-backup.apk'
        if not apk.is_file():
            errors.append('signed Android release artifact is absent')
        elif document.get('android_apk_sha256') != hashlib.sha256(apk.read_bytes()).hexdigest():
            errors.append('Android artifact differs from the tested APK')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, default=ROOT / 'docs/release-evidence/stable.json')
    parser.add_argument('--source-digest', action='store_true')
    parser.add_argument('--root', type=Path, default=ROOT)
    args = parser.parse_args()
    if args.source_digest:
        print(source_digest(args.root)); return 0
    try:
        errors = validate(json.loads(args.evidence.read_text()), args.root)
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print('Stable publication blocked:\n' + '\n'.join('- ' + error for error in errors))
        return 1
    print('Stable acceptance receipts match this source and APK. Verify Android signing before publication.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
