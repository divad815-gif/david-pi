from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def build_fake_release(
    tmp_path: Path, *, corrupt_checksum: bool = False, verify_only: bool = True,
    cli_source: str | None = None,
) -> tuple[Path, dict[str, str]]:
    version = "9.22.1"
    release_root = tmp_path / f"david-pi-{version}"
    release_root.mkdir()
    (release_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    cli = release_root / "david-pi"
    cli.write_text(cli_source or "#!/usr/bin/env bash\nprintf 'fake setup invoked\\n'\nexit 99\n", encoding="utf-8")
    cli.chmod(0o755)
    archive = tmp_path / f"david-pi-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(release_root, arcname=release_root.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if corrupt_checksum:
        digest = "0" * 64
    (tmp_path / "release-manifest.txt").write_text(
        "\n".join(
            (
                f"VERSION={version}",
                f"ARCHIVE={archive.name}",
                f"ARCHIVE_SHA256={digest}",
                "IMAGE=ghcr.io/example/david-pi@sha256:" + "1" * 64,
                "",
            )
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "DAVID_PI_REPOSITORY": "example/david-pi",
            "DAVID_PI_DOWNLOAD_BASE": tmp_path.as_uri(),
            "DAVID_PI_BOOTSTRAP_TEST_MODE": "1",
            "DAVID_PI_BOOTSTRAP_VERIFY_ONLY": "1" if verify_only else "0",
            "DAVID_PI_BOOTSTRAP_INSTALL_ROOT": str(tmp_path / "persistent-bootstrap"),
            "DAVID_PI_BOOTSTRAP_CLI_LINK": str(tmp_path / "bin" / "david-pi"),
        }
    )
    return archive, env


@pytest.mark.skipif(os.name == "nt", reason="bootstrap executes on native Linux in CI and the disposable VM")
def test_bootstrap_verifies_archive_before_setup(tmp_path: Path):
    _, env = build_fake_release(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh")], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "Release verified: 9.22.1" in result.stdout
    assert "Verification-only mode complete" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="bootstrap executes on native Linux in CI and the disposable VM")
def test_bootstrap_rejects_checksum_mismatch_before_execution(tmp_path: Path):
    _, env = build_fake_release(tmp_path, corrupt_checksum=True)
    result = subprocess.run(
        ["bash", str(ROOT / "install.sh")], env=env, text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert "Release verified" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="bootstrap executes on native Linux in CI and the disposable VM")
def test_failed_guided_setup_keeps_verified_recovery_cli(tmp_path: Path):
    _, env = build_fake_release(tmp_path, verify_only=False)
    returncode, output = run_piped_bootstrap_in_terminal(env)
    installed = tmp_path / "persistent-bootstrap" / "david-pi"
    cli_link = tmp_path / "bin" / "david-pi"
    assert returncode == 99, output
    assert "fake setup invoked" in output
    assert installed.is_file()
    assert cli_link.is_symlink()
    assert cli_link.resolve() == installed.resolve()


def run_piped_bootstrap_in_terminal(env, *, reply=None):
    """Run the documented pipe with a real controlling PTY, without sudo."""
    import pty
    pid, descriptor = pty.fork()
    if pid == 0:
        os.execve("/bin/bash", ["bash", "-c", 'cat "$1" | bash', "bootstrap-test", str(ROOT / "install.sh")], env)
    output = bytearray()
    deadline = time.monotonic() + 20
    status = None
    try:
        while time.monotonic() < deadline:
            if select.select([descriptor], [], [], 0.1)[0]:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    chunk = b""
                output.extend(chunk)
            if reply is not None and b"Your name: " in output:
                os.write(descriptor, reply.encode() + b"\n")
                reply = None
            waited, result = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = result
                break
        if status is None:
            raise AssertionError("Interactive bootstrap timed out: " + output.decode(errors="replace"))
        return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
    finally:
        if status is None:
            os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="requires a Linux-style controlling terminal")
def test_piped_bootstrap_prompts_read_terminal_not_script_stream(tmp_path):
    _, env = build_fake_release(tmp_path, verify_only=False, cli_source='''#!/usr/bin/env bash
set -eu
read -r -p 'Your name: ' answer
printf 'Selected name: %s\\n' "$answer"
''')
    returncode, output = run_piped_bootstrap_in_terminal(env, reply="John")
    assert returncode == 0, output
    assert "Selected name: John" in output


@pytest.mark.skipif(os.name == "nt", reason="bootstrap requires Linux")
def test_bootstrap_explains_missing_interactive_terminal(tmp_path):
    _, env = build_fake_release(tmp_path, verify_only=False)
    result = subprocess.run(["bash", str(ROOT / "install.sh")], env=env, text=True,
                            capture_output=True, start_new_session=True, check=False)
    assert result.returncode != 0
    assert "guided setup needs an interactive terminal" in result.stderr
    assert "fake setup invoked" not in result.stdout
    assert (tmp_path / "persistent-bootstrap" / "david-pi").is_file()
