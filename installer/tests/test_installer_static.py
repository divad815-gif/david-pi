from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_portal_is_loopback_only():
    compose = read("compose.yaml")
    assert '"127.0.0.1:8090:8000"' in compose
    assert '"0.0.0.0:80' not in compose
    assert '"80:8000"' not in compose


def test_container_hardening_is_declared():
    compose = read("compose.yaml")
    for required in ("cap_drop:", "- ALL", "no-new-privileges:true", "read_only: true", "pids_limit:"):
        assert required in compose
    assert "/var/run/docker.sock" not in compose


def test_storage_formatting_is_separate_and_confirmed():
    setup = read("installer/lib/setup.sh")
    storage = read("installer/lib/storage.sh")
    assert "dp_prepare_storage" not in setup
    assert 'Type ERASE $(basename "$device")' in storage
    assert '[[ "$device" != "$root_disk" ]]' in storage
    assert "wipefs --all" in storage


def test_mount_and_sentinel_fail_closed():
    unit = read("deploy/david-pi-portal.service")
    assert "ConditionPathIsMountPoint=/srv/data" in unit
    assert "ConditionPathExists=/srv/data/family-photos/.david-pi-storage" in unit
    assert "ExecStartPre=" in unit


def test_ssh_has_safety_rollback_and_validation():
    ssh = read("installer/lib/ssh.sh")
    assert "--on-active=5m" in ssh
    assert "sshd -t" in ssh
    assert "second key-authenticated SSH session" in ssh
    assert "PasswordAuthentication no" in ssh
    assert "SSH hardening was deferred safely" in ssh
    assert "return 0" not in ssh
    assert "/run/david-pi-ssh-fragment.before" in ssh


def test_external_primary_cannot_be_skipped():
    setup = read("installer/lib/setup.sh")
    assert '[[ "$selection" != none ]]' in setup
    assert "A dedicated primary disk is required" in setup


def test_update_preserves_runtime_secrets_and_rolls_back_on_failure():
    manage = read("installer/lib/manage.sh")
    assert "dp_install_application" not in manage
    assert "--exclude .env --exclude secrets" in manage
    assert "old-compose" in manage
    assert "previous release was restored" in manage


def test_funnel_is_never_enabled():
    shell = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.rglob("*.sh"))
    assert not re.search(r"tailscale\s+funnel\s+(--bg|on|https?://)", shell)
    assert "tailscale serve --bg http://127.0.0.1:8090" in shell


def test_independent_backup_uses_installer_uuid_not_a_literal():
    backup = read("deploy/david-pi-data-backup")
    assert '"$BACKUP_UUID"' in backup
    assert not re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', backup, re.I)


def test_cli_commands_have_implementations():
    cli = read("david-pi")
    for command in ("setup", "preflight", "prepare-storage", "status", "verify", "update", "backup", "restore-test", "repair", "support-bundle", "uninstall-app"):
        assert f"{command})" in cli
    assert (ROOT / "installer/lib/verify.sh").exists()
    assert (ROOT / "installer/lib/manage.sh").exists()


def test_generic_linux_hosts_are_supported_without_weakening_preflight():
    preflight = read("installer/lib/preflight.sh")
    platform = read("installer/lib/platform.sh")
    assert "Raspberry Pi 4 and Pi 5 only" not in preflight
    assert "amd64/x86_64 or arm64/aarch64" in preflight
    assert "debian:12|debian:13|ubuntu:22|ubuntu:24" in platform
    assert "dp_is_wsl" in preflight
    assert "WSL is for testing only" in preflight
    assert "systemd must be the host service manager" in preflight
    assert "at least 4 GB" in preflight


def test_laptop_policy_is_explicit_and_resource_limits_are_generated():
    setup = read("installer/lib/setup.sh")
    compose = read("compose.yaml")
    assert "Configure this dedicated laptop" in setup
    assert "HandleLidSwitch=ignore" in setup
    assert "DAVID_PI_PORTAL_MEMORY" in compose
    assert "DAVID_PI_PORTAL_CPUS" in compose
    assert "DAVID_PI_PORTAL_PIDS" in compose
    assert 'INSTANCE_NAME="${INSTANCE_NAME:-David-Pi}"' in setup
    assert "safe characters" in setup


def test_docker_and_tailscale_repositories_follow_detected_distribution():
    setup = read("installer/lib/setup.sh")
    assert "dp_docker_repo_os" in setup
    assert "download.docker.com/linux/${docker_os}" in setup
    assert "pkgs.tailscale.com/stable/${tailscale_os}" in setup


def test_release_builds_both_supported_architectures():
    release = read(".github/workflows/release.yml")
    ci = read(".github/workflows/ci.yml")
    assert "linux/amd64,linux/arm64" in release
    assert "linux/amd64,linux/arm64" in ci


def test_production_uses_pinned_release_image_and_dev_build_is_separate():
    compose = read("compose.yaml")
    dev = read("compose.dev.yaml")
    setup = read("installer/lib/setup.sh")
    assert "build:" not in compose
    assert "${DAVID_PI_IMAGE:-" in compose
    assert "build:" in dev
    assert 'docker pull "$DAVID_PI_IMAGE"' in setup
    assert "DAVID_PI_IMAGE=$DAVID_PI_IMAGE" in setup


def test_release_bootstrap_is_checksum_first_and_cleans_up():
    bootstrap = read("install.sh")
    release = read(".github/workflows/release.yml")
    assert "release-manifest.txt" in bootstrap
    assert '[[ "$actual" == "$ARCHIVE_SHA256" ]]' in bootstrap
    assert bootstrap.index('[[ "$actual" == "$ARCHIVE_SHA256" ]]') < bootstrap.index("tar -xzf")
    assert "trap cleanup EXIT INT TERM HUP" in bootstrap
    assert "DAVID_PI_IMAGE_OVERRIDE" in bootstrap
    assert "release-manifest.txt" in release
    assert "steps.build.outputs.digest" in release
    assert "Verify anonymous release and image access" in release


def test_update_requires_verified_archive_and_pinned_image():
    manage = read("installer/lib/manage.sh")
    assert "--image" in manage
    assert 'docker pull "$image"' in manage
    assert "dp_validate_image_reference" in manage
