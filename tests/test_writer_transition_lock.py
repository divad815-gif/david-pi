import fcntl
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "deploy" / "david-pi-portal-lifecycle"
BACKUP = ROOT / "deploy" / "david-pi-data-backup"
SCRIPTS = (PORTAL, BACKUP)


def _chat_secret_environment(tmp_path: Path):
    secret_directory = tmp_path / "chat-secrets"
    secret_directory.mkdir(exist_ok=True)
    master = secret_directory / "chat-master.key"
    vapid = secret_directory / "chat-vapid-private.pem"
    master.write_bytes(b"m" * 32)
    vapid.write_text("test-only-private-key-fixture\n", encoding="utf-8")
    master.chmod(0o440)
    vapid.chmod(0o440)
    return {
        "DAVID_PI_CHAT_MASTER_KEY": str(master),
        "DAVID_PI_CHAT_VAPID_KEY": str(vapid),
        "DAVID_PI_CHAT_SECRET_UID": str(os.getuid()),
        "DAVID_PI_CHAT_SECRET_GID": str(os.getgid()),
    }


def _lock_function(script: Path) -> str:
    source = script.read_text(encoding="utf-8")
    return source.split("acquire_writer_transition_lock() {", 1)[1].split("\n}", 1)[0]


def _attempt(script: Path, directory: Path):
    environment = os.environ.copy()
    command = (
        f"source {shlex.quote(str(script))}\n"
        f"WRITER_TRANSITION_LOCK_DIRECTORY={shlex.quote(str(directory))}\n"
        "acquire_writer_transition_lock\n"
    )
    return subprocess.run(
        ["bash", "-c", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _portal_start_attempt(
    tmp_path: Path,
    *,
    fail_command: str | None = None,
    signal_command: str | None = None,
):
    lock_directory = tmp_path / "writer-transition"
    lock_directory.mkdir(mode=0o700)
    lock_directory.chmod(0o700)
    log = tmp_path / "commands.log"
    checker = tmp_path / "lifecycle-command"
    checker.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, os, signal, sys\n"
        "fd = os.open(os.environ['LOCK_DIRECTORY'], os.O_RDONLY | os.O_DIRECTORY)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    command = ' '.join(sys.argv[1:])\n"
        "    with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as output:\n"
        "        output.write(command + '\\n')\n"
        "else:\n"
        "    raise SystemExit(91)\n"
        "if command == os.environ.get('SIGNAL_COMMAND'):\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
        "if command == os.environ.get('FAIL_COMMAND'):\n"
        "    raise SystemExit(42)\n",
        encoding="utf-8",
    )
    checker.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "LOCK_DIRECTORY": str(lock_directory),
            "COMMAND_LOG": str(log),
            "FAIL_COMMAND": fail_command if fail_command is not None else "never-fail",
            "SIGNAL_COMMAND": (
                signal_command if signal_command is not None else "never-signal"
            ),
        }
    )
    environment.update(_chat_secret_environment(tmp_path))
    command = (
        f"source {shlex.quote(str(PORTAL))}\n"
        f"WRITER_TRANSITION_LOCK_DIRECTORY={shlex.quote(str(lock_directory))}\n"
        f"DOCKER_BIN={shlex.quote(str(checker))}\n"
        f"PREPARE_MAINTENANCE_STATE={shlex.quote(str(checker))}\n"
        f"WRITER_READINESS={shlex.quote(str(checker))}\n"
        f"SYSTEMCTL_BIN={shlex.quote(str(checker))}\n"
        "acquire_writer_transition_lock\n"
        "start_portal\n"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_portal_and_backup_use_one_exact_lock_contract():
    sources = [script.read_text(encoding="utf-8") for script in SCRIPTS]
    default = "/run/david-pi-writer-transition"
    assert all(default in source for source in sources)
    assert _lock_function(PORTAL) == _lock_function(BACKUP)


def test_portal_holds_transition_lock_through_every_lifecycle_command(tmp_path):
    lock_directory = tmp_path / "writer-transition"
    lock_directory.mkdir(mode=0o700)
    lock_directory.chmod(0o700)
    log = tmp_path / "commands.log"
    checker = tmp_path / "locked-command"
    checker.write_text(
        "#!/usr/bin/env python3\n"
        "import fcntl, os, sys\n"
        "fd = os.open(os.environ['LOCK_DIRECTORY'], os.O_RDONLY | os.O_DIRECTORY)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    with open(os.environ['COMMAND_LOG'], 'a', encoding='utf-8') as output:\n"
        "        output.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(91)\n",
        encoding="utf-8",
    )
    checker.chmod(0o755)
    environment = os.environ.copy()
    environment.update({"LOCK_DIRECTORY": str(lock_directory), "COMMAND_LOG": str(log)})
    environment.update(_chat_secret_environment(tmp_path))
    command = (
        f"source {shlex.quote(str(PORTAL))}\n"
        f"WRITER_TRANSITION_LOCK_DIRECTORY={shlex.quote(str(lock_directory))}\n"
        f"DOCKER_BIN={shlex.quote(str(checker))}\n"
        f"PREPARE_MAINTENANCE_STATE={shlex.quote(str(checker))}\n"
        f"WRITER_READINESS={shlex.quote(str(checker))}\n"
        f"SYSTEMCTL_BIN={shlex.quote(str(checker))}\n"
        "acquire_writer_transition_lock\n"
        "start_portal\n"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "compose stop --timeout 45",
        "",
        "compose up -d --no-build",
        "--timeout 90 --interval 2 --stable-samples 3",
        "--no-block start david-pi-server-status.service",
    ]


def test_readiness_failure_synchronously_takes_started_stack_down_under_lock(
    tmp_path,
):
    readiness = "--timeout 90 --interval 2 --stable-samples 3"
    result, commands = _portal_start_attempt(tmp_path, fail_command=readiness)

    assert result.returncode == 42
    assert commands == [
        "compose stop --timeout 45",
        "",
        "compose up -d --no-build",
        readiness,
        "compose down",
    ]


@pytest.mark.parametrize(
    "failed,expected",
    [
        (
            "compose up -d --no-build",
            ["compose stop --timeout 45", "", "compose up -d --no-build", "compose down"],
        ),
        (
            "--no-block start david-pi-server-status.service",
            [
                "compose stop --timeout 45",
                "",
                "compose up -d --no-build",
                "--timeout 90 --interval 2 --stable-samples 3",
                "--no-block start david-pi-server-status.service",
                "compose down",
            ],
        ),
    ],
)
def test_every_post_up_error_path_synchronously_takes_stack_down(
    tmp_path, failed, expected
):
    result, commands = _portal_start_attempt(tmp_path, fail_command=failed)
    assert result.returncode == 42
    assert commands == expected


def test_signal_during_readiness_synchronously_takes_stack_down(tmp_path):
    readiness = "--timeout 90 --interval 2 --stable-samples 3"
    result, commands = _portal_start_attempt(tmp_path, signal_command=readiness)

    assert result.returncode == 143
    assert commands == [
        "compose stop --timeout 45",
        "",
        "compose up -d --no-build",
        readiness,
        "compose down",
    ]


def test_pre_up_error_does_not_take_an_unstarted_stack_down(tmp_path):
    result, commands = _portal_start_attempt(tmp_path, fail_command="")
    assert result.returncode == 42
    assert commands == ["compose stop --timeout 45", ""]


def test_chat_secret_metadata_drift_is_rejected_before_stopping_the_stack(tmp_path):
    environment = _chat_secret_environment(tmp_path)
    master = Path(environment["DAVID_PI_CHAT_MASTER_KEY"])
    before = master.read_bytes()
    master.chmod(0o400)
    command = f"source {shlex.quote(str(PORTAL))}\nstart_portal\n"
    result = subprocess.run(
        ["bash", "-c", command],
        env=os.environ | environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "protected Chat secret metadata drifted" in result.stderr
    assert master.read_bytes() == before


@pytest.mark.parametrize("script", SCRIPTS)
def test_each_workflow_rejects_the_same_held_transition_inode(tmp_path, script):
    lock_directory = tmp_path / "writer-transition"
    lock_directory.mkdir(mode=0o700)
    lock_directory.chmod(0o700)
    held = os.open(lock_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _attempt(script, lock_directory)
        assert result.returncode != 0
        assert "another writer transition is already running" in result.stderr
    finally:
        os.close(held)

    result = _attempt(script, lock_directory)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_lock_path_swap_during_flock_is_detected(tmp_path, script):
    lock_directory = tmp_path / "writer-transition"
    escaped = tmp_path / "escaped-transition"
    lock_directory.mkdir(mode=0o700)
    lock_directory.chmod(0o700)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_flock = shutil.which("flock")
    assert real_flock is not None
    fake_flock = fake_bin / "flock"
    fake_flock.write_text(
        "#!/bin/bash\n"
        'mv -- "$LOCK_DIRECTORY" "$ESCAPED_DIRECTORY"\n'
        'mkdir -m 0700 -- "$LOCK_DIRECTORY"\n'
        f'exec {shlex.quote(real_flock)} "$@"\n',
        encoding="utf-8",
    )
    fake_flock.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "LOCK_DIRECTORY": str(lock_directory),
            "ESCAPED_DIRECTORY": str(escaped),
        }
    )
    command = (
        f"source {shlex.quote(str(script))}\n"
        f"WRITER_TRANSITION_LOCK_DIRECTORY={shlex.quote(str(lock_directory))}\n"
        f"FLOCK_BIN={shlex.quote(str(fake_flock))}\n"
        "acquire_writer_transition_lock\n"
    )
    result = subprocess.run(
        ["bash", "-c", command],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "writer-transition lock path changed" in result.stderr
    assert escaped.is_dir()


@pytest.mark.parametrize("script", SCRIPTS)
def test_lock_directory_symlink_or_metadata_drift_fails_before_flock(tmp_path, script):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    result = _attempt(script, alias)
    assert result.returncode != 0
    assert "lock directory is unavailable" in result.stderr

    real.chmod(0o755)
    result = _attempt(script, real)
    assert result.returncode != 0
    assert "metadata drifted" in result.stderr
