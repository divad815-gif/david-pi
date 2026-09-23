"""Exercise the real package flow while keeping all host/network writes fake."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
PUBLIC_FILES = (
    "etc/apt/keyrings/docker.asc",
    "etc/apt/sources.list.d/docker.list",
    "usr/share/keyrings/tailscale-archive-keyring.gpg",
    "etc/apt/sources.list.d/tailscale.list",
)


@pytest.mark.parametrize("old_mode", [None, 0o600])
def test_package_repositories_remain_readable_under_private_umask(tmp_path, old_mode):
    sandbox = tmp_path / "host"
    for name in PUBLIC_FILES:
        destination = sandbox / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if old_mode:
            destination.write_text("old public repository file")
            destination.chmod(old_mode)
    script = r'''
source "$DAVID_PI_CLI_ROOT/installer/lib/setup.sh"
# Preserve the actual file-writing helper; only relocate its fixed destinations.
eval "real_$(declare -f dp_public_repository_file)"
dp_public_repository_file() { real_dp_public_repository_file "$TEST_HOST$1"; }
install() {
  if [[ "$*" == '-m 0755 -d /etc/apt/keyrings' ]]; then
    /usr/bin/install -m 0755 -d "$TEST_HOST/etc/apt/keyrings"
  else
    /usr/bin/install "$@"
  fi
}
command() {
  if [[ "$1" == -v && ( "$2" == docker || "$2" == tailscale ) ]]; then return 1; fi
  builtin command "$@"
}
dp_docker_repo_os() { echo debian; }
dp_os_codename() { echo trixie; }
dpkg() { [[ "$*" == '--print-architecture' ]]; echo amd64; }
curl() {
  case "${!#}" in
    https://download.docker.com/linux/debian/gpg) echo 'public Docker signing key' ;;
    https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg) echo 'public Tailscale signing key' ;;
    https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list)
      echo 'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian trixie main' ;;
    *) echo unexpected-download >&2; return 91 ;;
  esac
}
updates=0
apt-get() {
  if [[ "$1" == update ]]; then
    updates=$((updates+1))
    # Check the files *before* the simulated apt signature check, not just at
    # function return; the first update is for the OS's existing repositories.
    if (( updates >= 2 )); then
      [[ "$(stat -c %a "$TEST_HOST/etc/apt/keyrings/docker.asc")" == 644 ]]
      [[ "$(stat -c %a "$TEST_HOST/etc/apt/sources.list.d/docker.list")" == 644 ]]
    fi
    if (( updates == 3 )); then
      [[ "$(stat -c %a "$TEST_HOST/usr/share/keyrings/tailscale-archive-keyring.gpg")" == 644 ]]
      [[ "$(stat -c %a "$TEST_HOST/etc/apt/sources.list.d/tailscale.list")" == 644 ]]
    fi
  else
    [[ "$1" == install ]]
  fi
}
docker() { [[ "$*" == 'compose version' ]]; }
systemctl() { [[ "$*" == 'enable --now docker' || "$*" == 'enable --now tailscaled' ]]; }
umask 077
printf 'private before' > "$TEST_HOST/private-before"
dp_install_packages
[[ "$updates" == 3 ]]
[[ "$(umask)" == 0077 ]]
printf 'private after' > "$TEST_HOST/private-after"
'''
    result = subprocess.run(["bash", "-c", script], env={**os.environ, "DAVID_PI_CLI_ROOT": str(ROOT), "TEST_HOST": str(sandbox)}, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    for name in PUBLIC_FILES:
        path = sandbox / name
        assert path.stat().st_mode & 0o777 == 0o644
        assert path.read_text() != "old public repository file"
    for name in ("private-before", "private-after"):
        assert (sandbox / name).stat().st_mode & 0o777 == 0o600
    assert "signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg" in (sandbox / PUBLIC_FILES[3]).read_text()
    assert "signed-by=/etc/apt/keyrings/docker.asc" in (sandbox / PUBLIC_FILES[1]).read_text()


def test_failed_public_key_download_stops_before_apt_uses_it(tmp_path):
    # A failed HTTPS pipeline must still stop setup under pipefail.
    script = r'''
source "$DAVID_PI_CLI_ROOT/installer/lib/setup.sh"
umask 077
false | dp_public_repository_file "$TEST_KEY"
printf 'must not continue' > "$TEST_SENTINEL"
'''
    result = subprocess.run(["bash", "-c", script], env={**os.environ, "DAVID_PI_CLI_ROOT": str(ROOT), "TEST_KEY": str(tmp_path / "public.gpg"), "TEST_SENTINEL": str(tmp_path / "continued")}, text=True, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / "continued").exists()
