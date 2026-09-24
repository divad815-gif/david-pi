"""Source-level checks of boundaries complement host protocol behavior tests."""
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

def read(name):
    return (ROOT / name).read_text()


def test_setup_uses_private_pre_storage_wizard_and_explicit_units():
    setup = read('installer/lib/setup.sh')
    helper = read('installer/systemd/david-pi-helper.service')
    assert 'host.py initialize' in setup
    assert 'david-pi-helper.service david-pi-portal.service' in setup
    assert 'david-pi-status.service david-pi-status.timer' in setup
    assert 'deploy/*.service' not in setup
    assert 'ConditionPathIsMountPoint' not in helper
    assert 'docker.sock' not in read('installer/host.py')


def test_setup_never_formats_or_changes_ssh_or_resets_tailscale():
    setup = read('installer/lib/setup.sh') + read('installer/host.py')
    for prohibited in ('mkfs', 'wipefs', 'parted', 'serve reset', 'dp_ssh_prepare', 'chmod -R', 'chown -R'):
        assert prohibited not in setup
    assert 'prepare-storage)' in read('david-pi')
    assert 'does not format or repartition' in read('david-pi')


def test_funnel_is_never_enabled():
    shell = '\n'.join(p.read_text() for p in (ROOT/'installer').rglob('*.sh'))
    assert not re.search(r'tailscale\s+funnel\s+(--bg|on|https?://)', shell)
    assert '"AllowFunnel"' in read('installer/host.py')


def test_portal_start_checks_actual_filesystem_and_installation_identity():
    service = read('installer/systemd/david-pi-portal.service')
    assert 'host.py storage-guard' in service
    assert '/etc/david-pi/compose.json' in service
    engine = read('installer/host.py')
    assert 'record.get("uuid") != info["uuid"]' in engine
    assert 'marker.read_text().strip() != cfg["instance_id"]' in engine


def test_only_supported_platforms_are_accepted():
    platform = read('installer/lib/platform.sh')
    assert 'debian:13:amd64|ubuntu:24.04:amd64' in platform
    assert 'Raspberry Pi 4' in platform and 'Raspberry Pi 5' in platform
    assert 'debian:12' not in platform
    preflight = read('installer/lib/preflight.sh')
    assert 'dp_is_wsl' in preflight and 'systemd must be the host service manager' in preflight


def test_official_package_sources_are_used():
    setup = read('installer/lib/setup.sh')
    assert 'download.docker.com/linux/${docker_os}' in setup
    assert 'pkgs.tailscale.com/stable/${tailscale_os}' in setup
    assert 'signed-by=' in setup


def test_release_bootstrap_is_checksum_first_and_rejects_unsafe_members():
    bootstrap = read('install.sh')
    assert bootstrap.index('[[ "$actual" == "$ARCHIVE_SHA256" ]]') < bootstrap.index('bundle.extractall')
    assert 'member.isfile() or member.isdir()' in bootstrap
    assert 'trap cleanup EXIT INT TERM HUP' in bootstrap
    assert 'david-pi-bootstrap-$VERSION' in bootstrap


def test_updates_capture_complete_recovery_and_do_not_auto_restore_data():
    engine = read('installer/host.py')
    assert 'self.snapshot(snapshot_path, job)' in engine
    assert 'next_schema == old["data_schema_version"]' in engine
    assert 'rollback_requires_local_review' in engine
    assert 'self.docker("stop")' in engine
    assert 'clean_host_restore_verified": False' in engine


def test_claim_is_expiring_bound_to_identity_and_single_use():
    engine = read('installer/host.py')
    assert 'Tailscale-User-Login' in engine
    assert 'with self.server.claim_lock' in engine
    assert 'state.get("expires", 0) < time.time()' in engine
    assert 'state.get("claimed")' in engine
    assert 'hmac.compare_digest' in engine
    assert 'Secure; HttpOnly; SameSite=Strict' in engine
    assert 'self.headers.get("Origin") != state["origin"]' in engine
    assert 'X-CSRF-Token' in engine
    assert 'ThreadingHTTPServer(("127.0.0.1", 8091)' in engine


def test_uninstall_preserves_content_and_does_not_remove_volumes():
    manage = read('installer/lib/manage.sh')
    assert 'REMOVE APP' in manage
    assert 'compose --project-name david-pi' in manage
    assert ' down' in manage and ' down -v' not in manage
    assert 'rm -rf' not in manage
