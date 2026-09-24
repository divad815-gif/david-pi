import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "david-pi-prepare-maintenance-state"
LOADER = SourceFileLoader("maintenance_state_preparer", str(SOURCE))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
preparer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preparer)
CURRENT_UID = os.getuid()
CURRENT_GID = os.getgid()
ALT_GID = next((group for group in os.getgroups() if group != CURRENT_GID), None)
TRANSITION_GID = ALT_GID if ALT_GID is not None else CURRENT_GID + 1


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def make_data_root(parent: Path) -> Path:
    parent.chmod(0o700)
    data = parent / "family-photos"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    sentinel = data / preparer.SENTINEL_NAME
    sentinel.write_bytes(preparer.SENTINEL_VALUE + b"\n")
    sentinel.chmod(0o644)
    return data


def reviewed_chain(data: Path):
    anchor = data.parent
    return (
        (anchor, CURRENT_UID, CURRENT_GID, file_mode(anchor)),
        (data, CURRENT_UID, CURRENT_GID, 0o755),
    )


def prepare(data: Path, **changes) -> None:
    defaults = {
        "operations_uid": CURRENT_UID,
        "operations_gid": CURRENT_GID,
        "maintenance_uid": CURRENT_UID,
        "maintenance_gid": CURRENT_GID,
        "sentinel_uid": CURRENT_UID,
        "sentinel_gid": CURRENT_GID,
        "expected_chain": reviewed_chain(data),
        "require_root": False,
        "writer_check": lambda: None,
    }
    defaults.update(changes)
    preparer.prepare_state(data, **defaults)


def make_exact_directories(data: Path) -> tuple[Path, Path]:
    operations = data / preparer.OPERATIONS_NAME
    maintenance = operations / preparer.MAINTENANCE_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    maintenance.mkdir(mode=0o750)
    maintenance.chmod(0o750)
    return operations, maintenance


def make_audiobook_prerequisites(data: Path):
    operations = data / preparer.OPERATIONS_NAME
    if not operations.exists():
        operations.mkdir(mode=0o755)
        operations.chmod(0o755)
    audiobook_root = data / preparer.AUDIOBOOK_ROOT_NAME
    audiobook_root.mkdir(mode=0o755)
    audiobook_root.chmod(0o755)
    originals = audiobook_root / preparer.AUDIOBOOK_ORIGINALS_NAME
    originals.mkdir(mode=0o755)
    originals.chmod(0o755)
    incoming = audiobook_root / preparer.AUDIOBOOK_INCOMING_NAME
    incoming.mkdir(mode=0o755)
    incoming.chmod(0o755)
    return operations, audiobook_root, originals, incoming


def audiobook_targets(data: Path):
    operations = data / preparer.OPERATIONS_NAME
    audiobook_root = data / preparer.AUDIOBOOK_ROOT_NAME
    return (
        operations / preparer.AUDIOBOOK_STATE_NAME,
        audiobook_root / preparer.AUDIOBOOK_STREAMING_NAME,
        audiobook_root
        / preparer.AUDIOBOOK_INCOMING_NAME
        / preparer.AUDIOBOOK_STREAMING_NAME,
    )


def prepare_audiobook(data: Path, **changes) -> None:
    defaults = {
        "operations_uid": CURRENT_UID,
        "operations_gid": CURRENT_GID,
        "audiobook_uid": CURRENT_UID,
        "audiobook_gid": CURRENT_GID,
        "sentinel_uid": CURRENT_UID,
        "sentinel_gid": CURRENT_GID,
        "expected_chain": reviewed_chain(data),
        "require_root": False,
        "writer_check": lambda: None,
    }
    defaults.update(changes)
    preparer.prepare_audiobook_state(data, **defaults)


def audiobook_transaction_directories(data: Path) -> list[Path]:
    return sorted(
        (
            child
            for child in data.iterdir()
            if child.name.startswith(preparer.AUDIOBOOK_TRANSACTION_PREFIX)
        ),
        key=lambda child: child.name,
    )


def transaction_directories(data: Path) -> list[Path]:
    return sorted(
        (
            child
            for child in data.iterdir()
            if child.name.startswith(preparer.TRANSACTION_PREFIX)
        ),
        key=lambda child: child.name,
    )


def test_create_only_preparation_is_idempotent_and_avoids_repair_calls(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    prepare(data)
    operations = data / preparer.OPERATIONS_NAME
    maintenance = operations / preparer.MAINTENANCE_NAME
    calls = []
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    monkeypatch.setattr(preparer.os, "fchmod", lambda *_args: calls.append("chmod"))

    prepare(data)
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert list(transactions[0].iterdir()) == []

    assert calls == []
    assert transaction_directories(data) == transactions
    assert file_mode(operations) == 0o755
    assert file_mode(maintenance) == 0o750


def test_new_directory_ownership_transition_is_bounded_and_idempotent(
    tmp_path, monkeypatch
):
    if ALT_GID is None:
        pytest.skip(
            "the test identity has no supplementary group for a real transition"
        )
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)

    prepare(data, maintenance_gid=ALT_GID)

    maintenance = operations / preparer.MAINTENANCE_NAME
    details = maintenance.stat()
    assert (details.st_uid, details.st_gid, file_mode(maintenance)) == (
        CURRENT_UID,
        ALT_GID,
        0o750,
    )
    calls = []
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    monkeypatch.setattr(preparer.os, "fchmod", lambda *_args: calls.append("chmod"))
    prepare(data, maintenance_gid=ALT_GID)
    assert calls == []


def test_audiobook_preparation_creates_exact_targets_and_is_idempotent(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    _operations, _root, originals, _incoming = make_audiobook_prerequisites(data)
    original = originals / "saved-original.m4b"
    original.write_bytes(b"saved audiobook content")
    original.chmod(0o640)
    original_before = (
        original.stat().st_ino,
        _metadata(original),
        original.stat().st_size,
        original.stat().st_mtime_ns,
        original.stat().st_ctime_ns,
        original.read_bytes(),
    )

    prepare_audiobook(data)
    targets = audiobook_targets(data)
    assert all(_metadata(path) == (CURRENT_UID, CURRENT_GID, 0o750) for path in targets)
    assert len(audiobook_transaction_directories(data)) == 3

    marker = targets[1] / "rebuildable-cache-marker"
    marker.write_bytes(b"preserve existing state")
    marker.chmod(0o640)
    marker_before = (marker.stat().st_ino, _metadata(marker), marker.read_bytes())
    calls = []
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    monkeypatch.setattr(preparer.os, "fchmod", lambda *_args: calls.append("chmod"))

    prepare_audiobook(data)

    assert calls == []
    assert len(audiobook_transaction_directories(data)) == 3
    assert (marker.stat().st_ino, _metadata(marker), marker.read_bytes()) == marker_before
    assert (
        original.stat().st_ino,
        _metadata(original),
        original.stat().st_size,
        original.stat().st_mtime_ns,
        original.stat().st_ctime_ns,
        original.read_bytes(),
    ) == original_before


@pytest.mark.parametrize(
    "selected_index",
    [0, 1, 2],
    ids=["worker-state", "streaming", "incoming-streaming"],
)
def test_audiobook_existing_target_drift_fails_without_repair(
    tmp_path, monkeypatch, selected_index
):
    data = make_data_root(tmp_path)
    make_audiobook_prerequisites(data)
    for target in audiobook_targets(data):
        target.mkdir(mode=0o750)
        target.chmod(0o750)
    selected = audiobook_targets(data)[selected_index]
    marker = selected / "existing-state"
    marker.write_bytes(b"unchanged")
    selected.chmod(0o700)
    before = (selected.stat().st_ino, _metadata(selected), marker.read_bytes())
    calls = []
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    monkeypatch.setattr(preparer.os, "fchmod", lambda *_args: calls.append("chmod"))

    with pytest.raises(preparer.PreparationError, match="drifted"):
        prepare_audiobook(data)

    assert calls == []
    assert (selected.stat().st_ino, _metadata(selected), marker.read_bytes()) == before


@pytest.mark.parametrize(
    "selected_index",
    [0, 1, 2],
    ids=["worker-state", "streaming", "incoming-streaming"],
)
def test_audiobook_target_symlink_fails_without_touching_destination(
    tmp_path, selected_index
):
    data = make_data_root(tmp_path)
    make_audiobook_prerequisites(data)
    outside = tmp_path / "outside-audiobook-state"
    outside.mkdir(mode=0o711)
    outside.chmod(0o711)
    marker = outside / "operator-content"
    marker.write_bytes(b"retain")
    marker.chmod(0o640)
    target = audiobook_targets(data)[selected_index]
    target.symlink_to(outside, target_is_directory=True)
    before = (outside.stat().st_ino, _metadata(outside), marker.read_bytes())

    with pytest.raises(preparer.PreparationError):
        prepare_audiobook(data)

    assert (outside.stat().st_ino, _metadata(outside), marker.read_bytes()) == before


def test_audiobook_final_name_race_is_not_replaced_or_repaired(tmp_path):
    data = make_data_root(tmp_path)
    _operations, audiobook_root, _originals, _incoming = make_audiobook_prerequisites(data)
    attacker = audiobook_root / preparer.AUDIOBOOK_STREAMING_NAME
    snapshot = {}

    def hook(phase):
        if phase == "streaming:before_publish":
            attacker.mkdir(mode=0o700)
            attacker.chmod(0o700)
            marker = attacker / "attacker-state"
            marker.write_bytes(b"unchanged")
            marker.chmod(0o640)
            snapshot.update(
                inode=attacker.stat().st_ino,
                metadata=_metadata(attacker),
                marker_inode=marker.stat().st_ino,
            )

    with pytest.raises(preparer.PreparationError, match="appeared"):
        prepare_audiobook(data, _test_hook=hook)

    marker = attacker / "attacker-state"
    assert attacker.stat().st_ino == snapshot["inode"]
    assert _metadata(attacker) == snapshot["metadata"]
    assert marker.stat().st_ino == snapshot["marker_inode"]
    assert marker.read_bytes() == b"unchanged"
    assert not audiobook_targets(data)[2].exists()


def test_audiobook_originals_replacement_before_publication_fails_closed(tmp_path):
    data = make_data_root(tmp_path)
    _operations, audiobook_root, originals, _incoming = make_audiobook_prerequisites(data)
    marker = originals / "saved-original.m4b"
    marker.write_bytes(b"do not touch")
    marker.chmod(0o640)
    escaped = audiobook_root / "escaped-originals"
    before = (marker.stat().st_ino, _metadata(marker), marker.read_bytes())

    def hook(phase):
        if phase == "state:before_publish":
            originals.rename(escaped)
            originals.mkdir(mode=0o755)
            originals.chmod(0o755)

    with pytest.raises(preparer.PreparationError, match="changed"):
        prepare_audiobook(data, _test_hook=hook)

    escaped_marker = escaped / marker.name
    assert (
        escaped_marker.stat().st_ino,
        _metadata(escaped_marker),
        escaped_marker.read_bytes(),
    ) == before
    assert not audiobook_targets(data)[0].exists()


def test_audiobook_writer_stop_check_is_repeated_before_creation(tmp_path):
    data = make_data_root(tmp_path)
    make_audiobook_prerequisites(data)
    checks = []

    def writer_check():
        checks.append(len(checks))
        if len(checks) == 2:
            raise preparer.PreparationError("writer resumed")

    with pytest.raises(preparer.PreparationError, match="writer resumed"):
        prepare_audiobook(data, writer_check=writer_check)

    assert len(checks) == 2
    assert not any(path.exists() for path in audiobook_targets(data))


@pytest.mark.parametrize(
    "phase,completed",
    [
        ("state:after_payload", 0),
        ("state:after_publish", 1),
        ("streaming:after_publish", 2),
        ("incoming-streaming:after_publish", 3),
    ],
)
def test_audiobook_process_crash_leaves_only_complete_prefix_and_is_retryable(
    tmp_path, phase, completed
):
    data = make_data_root(tmp_path)
    make_audiobook_prerequisites(data)
    exit_code = 73
    child = os.fork()
    if child == 0:

        def crash_hook(current):
            if current == phase:
                os._exit(exit_code)

        try:
            prepare_audiobook(data, _test_hook=crash_hook)
        except BaseException:
            os._exit(74)
        os._exit(75)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == exit_code
    targets = audiobook_targets(data)
    assert [target.exists() for target in targets] == [
        index < completed for index in range(3)
    ]

    prepare_audiobook(data)

    assert all(_metadata(path) == (CURRENT_UID, CURRENT_GID, 0o750) for path in targets)
    assert audiobook_transaction_directories(data)


def test_worker_state_entry_point_orders_both_idempotent_phases(monkeypatch):
    calls = []
    monkeypatch.setattr(preparer, "prepare_state", lambda: calls.append("maintenance"))
    monkeypatch.setattr(
        preparer, "prepare_audiobook_state", lambda: calls.append("audiobook")
    )

    preparer.prepare_worker_state()

    assert calls == ["maintenance", "audiobook"]


def test_legacy_partial_final_name_remains_fail_closed_and_unmodified(tmp_path):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    partial = operations / preparer.MAINTENANCE_NAME
    partial.mkdir(mode=0o700)
    partial.chmod(0o700)
    before = (partial.stat().st_ino, _metadata(partial))

    with pytest.raises(preparer.PreparationError, match="drifted"):
        prepare(data)

    assert (partial.stat().st_ino, _metadata(partial)) == before


@pytest.mark.parametrize("target", ["ancestor", "family", "operations", "maintenance"])
def test_preexisting_owner_or_mode_drift_fails_without_repair(
    tmp_path, monkeypatch, target
):
    data = make_data_root(tmp_path)
    operations, maintenance = make_exact_directories(data)
    selected = {
        "ancestor": data.parent,
        "family": data,
        "operations": operations,
        "maintenance": maintenance,
    }[target]
    selected.chmod(0o770)
    before = file_mode(selected)
    calls = []
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    monkeypatch.setattr(preparer.os, "fchmod", lambda *_args: calls.append("chmod"))

    with pytest.raises(preparer.PreparationError, match="drifted|writable"):
        prepare(data)

    assert calls == []
    assert file_mode(selected) == before


@pytest.mark.parametrize("unsafe", ["operations", "maintenance"])
def test_protected_directory_symlinks_fail_without_mutating_outside(tmp_path, unsafe):
    data = make_data_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o711)
    outside.chmod(0o711)
    before = outside.stat()
    operations = data / preparer.OPERATIONS_NAME
    if unsafe == "operations":
        operations.symlink_to(outside, target_is_directory=True)
    else:
        operations.mkdir(mode=0o755)
        operations.chmod(0o755)
        (operations / preparer.MAINTENANCE_NAME).symlink_to(
            outside, target_is_directory=True
        )

    with pytest.raises(preparer.PreparationError):
        prepare(data)

    after = outside.stat()
    assert (after.st_uid, after.st_gid, file_mode(outside)) == (
        before.st_uid,
        before.st_gid,
        stat.S_IMODE(before.st_mode),
    )


def test_intermediate_symlink_fails_without_mutating_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    data = make_data_root(outside)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(preparer.PreparationError):
        prepare(alias / data.name)

    assert not (data / preparer.OPERATIONS_NAME).exists()


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "directory", "mismatch", "oversize", "mode"]
)
def test_unsafe_sentinel_fails_before_directory_creation(tmp_path, kind):
    data = make_data_root(tmp_path)
    sentinel = data / preparer.SENTINEL_NAME
    if kind == "symlink":
        sentinel.unlink()
        outside = tmp_path / "sentinel-outside"
        outside.write_bytes(preparer.SENTINEL_VALUE)
        sentinel.symlink_to(outside)
    elif kind == "hardlink":
        os.link(sentinel, tmp_path / "sentinel-hardlink")
    elif kind == "directory":
        sentinel.unlink()
        sentinel.mkdir()
    elif kind == "mismatch":
        sentinel.write_bytes(b"wrong-storage")
    elif kind == "oversize":
        sentinel.write_bytes(preparer.SENTINEL_VALUE + b"-unexpected")
    else:
        sentinel.chmod(0o664)

    with pytest.raises(preparer.PreparationError):
        prepare(data)

    assert not (data / preparer.OPERATIONS_NAME).exists()


def test_writer_stop_check_is_repeated_and_blocks_creation(tmp_path):
    data = make_data_root(tmp_path)
    checks = []

    def writer_check():
        checks.append(len(checks))
        if len(checks) == 2:
            raise preparer.PreparationError("writer resumed")

    with pytest.raises(preparer.PreparationError, match="writer resumed"):
        prepare(data, writer_check=writer_check)

    assert len(checks) == 2
    assert not (data / preparer.OPERATIONS_NAME).exists()


def test_running_known_writer_is_rejected(monkeypatch):
    result = SimpleNamespace(
        returncode=0,
        stdout="unrelated\ndavid-pi-maintenance\n",
    )
    monkeypatch.setattr(preparer.subprocess, "run", lambda *_args, **_kwargs: result)
    with pytest.raises(preparer.PreparationError, match="still running"):
        preparer.verify_writers_stopped()


def test_device_backup_worker_is_in_exact_stopped_writer_contract():
    assert "david-pi-device-backup-worker" in preparer.KNOWN_WRITERS
    assert "david-pi-mytube-preparer" in preparer.KNOWN_WRITERS


@pytest.mark.parametrize("euid,egid", [(1, 0), (0, 1)])
def test_production_preparation_requires_root_user_and_group(monkeypatch, euid, egid):
    monkeypatch.setattr(preparer.os, "geteuid", lambda: euid)
    monkeypatch.setattr(preparer.os, "getegid", lambda: egid)
    with pytest.raises(preparer.PreparationError, match="root:root"):
        preparer.prepare_state()


def _replace_reviewed_path(data: Path, replacement: str):
    operations = data / preparer.OPERATIONS_NAME
    if replacement == "ancestor":
        escaped = data.parent.parent / f"escaped-ancestor-{data.parent.name}"
    elif replacement == "family":
        escaped = data.parent / "escaped-family"
    else:
        escaped = data / f"escaped-{replacement}"

    def populate_family(replacement_data):
        replacement_data.mkdir(mode=0o755)
        replacement_data.chmod(0o755)
        sentinel = replacement_data / preparer.SENTINEL_NAME
        sentinel.write_bytes(preparer.SENTINEL_VALUE + b"\n")
        sentinel.chmod(0o644)
        replacement_operations = replacement_data / preparer.OPERATIONS_NAME
        replacement_operations.mkdir(mode=0o755)
        replacement_operations.chmod(0o755)

    if replacement == "ancestor":
        ancestor = data.parent
        ancestor.rename(escaped)
        ancestor.mkdir(mode=0o700)
        ancestor.chmod(0o700)
        populate_family(data)
    elif replacement == "operations":
        operations.rename(escaped)
        operations.mkdir(mode=0o755)
        operations.chmod(0o755)
    elif replacement == "family":
        data.rename(escaped)
        populate_family(data)
    else:
        sentinel = data / preparer.SENTINEL_NAME
        sentinel.rename(escaped)
        sentinel.write_bytes(preparer.SENTINEL_VALUE + b"\n")
        sentinel.chmod(0o644)
    return escaped


def _metadata(path: Path):
    details = path.stat()
    return details.st_uid, details.st_gid, stat.S_IMODE(details.st_mode)


@pytest.mark.parametrize("phase", ["before_fchown", "before_fchmod"])
@pytest.mark.parametrize(
    "replacement", ["ancestor", "family", "operations", "sentinel"]
)
def test_reviewed_path_replacement_before_private_metadata_call_fails_closed(
    tmp_path, replacement, phase
):
    if phase == "before_fchmod" and ALT_GID is None:
        pytest.skip(
            "the test identity has no supplementary group for a real transition"
        )
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    guard = operations / "preexisting-guard"
    guard.write_bytes(b"do-not-touch")
    guard.chmod(0o640)
    guard_before = (_metadata(guard), guard.read_bytes())
    escaped = None

    def hook(current):
        nonlocal escaped
        if current == phase:
            escaped = _replace_reviewed_path(data, replacement)

    transition_gid = ALT_GID if phase == "before_fchmod" else TRANSITION_GID
    with pytest.raises(preparer.PreparationError, match="changed|drifted"):
        prepare(data, maintenance_gid=transition_gid, _test_hook=hook)

    assert escaped is not None
    escaped_guard = {
        "ancestor": escaped / data.name / preparer.OPERATIONS_NAME / guard.name,
        "family": escaped / preparer.OPERATIONS_NAME / guard.name,
        "operations": escaped / guard.name,
        "sentinel": guard,
    }[replacement]
    assert escaped_guard.exists()
    assert (_metadata(escaped_guard), escaped_guard.read_bytes()) == guard_before
    assert not (data / preparer.OPERATIONS_NAME / preparer.MAINTENANCE_NAME).exists()


@pytest.mark.parametrize("call", ["fchown", "fchmod"])
def test_actual_private_metadata_syscall_race_never_mutates_preexisting_inode(
    tmp_path, monkeypatch, call
):
    if call == "fchown" and ALT_GID is None:
        pytest.skip(
            "the test identity has no supplementary group for a real transition"
        )
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    guard = operations / "preexisting-guard"
    guard.write_bytes(b"stable")
    guard.chmod(0o640)
    guard_before = (_metadata(guard), guard.read_bytes(), guard.stat().st_ino)
    escaped = data / "escaped-operations-at-syscall"
    real_call = getattr(preparer.os, call)
    raced = False
    mutated_path = None

    def swap_then_call(*args):
        nonlocal raced, mutated_path
        if not raced:
            raced = True
            operations.rename(escaped)
            operations.mkdir(mode=0o755)
            operations.chmod(0o755)
            mutated_path = os.readlink(f"/proc/self/fd/{args[0]}")
        return real_call(*args)

    monkeypatch.setattr(preparer.os, call, swap_then_call)
    maintenance_gid = ALT_GID if call == "fchown" else CURRENT_GID
    with pytest.raises(preparer.PreparationError, match="changed"):
        prepare(data, maintenance_gid=maintenance_gid)

    escaped_guard = escaped / guard.name
    assert raced
    assert (
        _metadata(escaped_guard),
        escaped_guard.read_bytes(),
        escaped_guard.stat().st_ino,
    ) == guard_before
    assert mutated_path is not None
    assert str(escaped) not in mutated_path
    assert f"/{preparer.TRANSACTION_PREFIX}" in mutated_path
    assert len(transaction_directories(data)) == 1
    assert not (operations / preparer.MAINTENANCE_NAME).exists()


def test_actual_transaction_mkdir_race_retains_created_evidence(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    guard = operations / "preexisting-guard"
    guard.write_bytes(b"stable")
    guard.chmod(0o640)
    guard_before = (_metadata(guard), guard.read_bytes(), guard.stat().st_ino)
    escaped = data / "escaped-operations-at-mkdir"
    real_mkdir = preparer.os.mkdir
    raced = False

    def mkdir_with_swap(name, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if (
            not raced
            and dir_fd is not None
            and isinstance(name, str)
            and name.startswith(preparer.TRANSACTION_PREFIX)
        ):
            raced = True
            operations.rename(escaped)
            real_mkdir(operations, 0o755)
            os.chmod(operations, 0o755)
        return real_mkdir(name, mode, dir_fd=dir_fd)

    monkeypatch.setattr(preparer.os, "mkdir", mkdir_with_swap)
    with pytest.raises(preparer.PreparationError, match="changed"):
        prepare(data)

    escaped_guard = escaped / guard.name
    assert raced
    assert (
        _metadata(escaped_guard),
        escaped_guard.read_bytes(),
        escaped_guard.stat().st_ino,
    ) == guard_before
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert list(transactions[0].iterdir()) == []
    assert not (operations / preparer.MAINTENANCE_NAME).exists()


def test_final_name_race_is_never_replaced_repaired_or_removed(tmp_path):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    attacker = operations / preparer.MAINTENANCE_NAME
    snapshot = {}

    def hook(phase):
        if phase == "before_publish":
            attacker.mkdir(mode=0o700)
            attacker.chmod(0o700)
            (attacker / "attacker-owned").write_bytes(b"unchanged")
            snapshot.update(metadata=_metadata(attacker), inode=attacker.stat().st_ino)

    with pytest.raises(preparer.PreparationError, match="appeared"):
        prepare(data, _test_hook=hook)

    assert _metadata(attacker) == snapshot["metadata"]
    assert attacker.stat().st_ino == snapshot["inode"]
    assert (attacker / "attacker-owned").read_bytes() == b"unchanged"
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert (transactions[0] / preparer.TRANSACTION_PAYLOAD).is_dir()


def test_final_name_appearing_inside_publication_call_is_untouched(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    attacker = operations / preparer.MAINTENANCE_NAME
    real_renameat2 = preparer._RENAMEAT2_FUNCTION
    snapshot = {}

    def renameat2_with_final_race(*args):
        attacker.mkdir(mode=0o700)
        attacker.chmod(0o700)
        marker = attacker / "preexisting"
        marker.write_bytes(b"stable")
        marker.chmod(0o640)
        snapshot.update(
            metadata=_metadata(attacker),
            inode=attacker.stat().st_ino,
            marker_inode=marker.stat().st_ino,
        )
        return real_renameat2(*args)

    monkeypatch.setattr(preparer, "_RENAMEAT2_FUNCTION", renameat2_with_final_race)
    with pytest.raises(preparer.PreparationError, match="appeared"):
        prepare(data)

    marker = attacker / "preexisting"
    assert _metadata(attacker) == snapshot["metadata"]
    assert attacker.stat().st_ino == snapshot["inode"]
    assert marker.stat().st_ino == snapshot["marker_inode"]
    assert marker.read_bytes() == b"stable"
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert (transactions[0] / preparer.TRANSACTION_PAYLOAD).is_dir()


def test_operations_swap_inside_publication_call_retains_published_evidence(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    guard = operations / "preexisting-guard"
    guard.write_bytes(b"stable")
    guard.chmod(0o640)
    guard_before = (_metadata(guard), guard.stat().st_ino, guard.read_bytes())
    escaped = data / "escaped-operations-at-publication"
    real_renameat2 = preparer._RENAMEAT2_FUNCTION
    raced = False

    def renameat2_with_operations_swap(*args):
        nonlocal raced
        raced = True
        operations.rename(escaped)
        operations.mkdir(mode=0o755)
        operations.chmod(0o755)
        return real_renameat2(*args)

    monkeypatch.setattr(preparer, "_RENAMEAT2_FUNCTION", renameat2_with_operations_swap)
    with pytest.raises(preparer.PreparationError, match="changed"):
        prepare(data)

    escaped_guard = escaped / guard.name
    assert raced
    assert (
        _metadata(escaped_guard),
        escaped_guard.stat().st_ino,
        escaped_guard.read_bytes(),
    ) == guard_before
    assert (escaped / preparer.MAINTENANCE_NAME).is_dir()
    assert _metadata(escaped / preparer.MAINTENANCE_NAME) == (
        CURRENT_UID,
        CURRENT_GID,
        0o750,
    )
    assert not (operations / preparer.MAINTENANCE_NAME).exists()
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert list(transactions[0].iterdir()) == []


@pytest.mark.parametrize(
    "phase", ["after_mkdir_payload", "after_fchmod", "after_publish"]
)
def test_process_crash_is_retryable_without_adopting_private_transaction(
    tmp_path, phase
):
    data = make_data_root(tmp_path)
    exit_code = 73
    child = os.fork()
    if child == 0:

        def crash_hook(current):
            if current == phase:
                os._exit(exit_code)

        try:
            prepare(data, _test_hook=crash_hook)
        except BaseException:
            os._exit(74)
        os._exit(75)

    _pid, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == exit_code
    operations = data / preparer.OPERATIONS_NAME
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    final = operations / preparer.MAINTENANCE_NAME
    assert final.exists() is (phase == "after_publish")

    # The retry either publishes a fresh payload or accepts the already exact
    # atomic publication.  It deliberately does not inspect/adopt the orphan.
    prepare(data)
    assert _metadata(final) == (CURRENT_UID, CURRENT_GID, 0o750)
    assert transactions[0].exists()


def test_preexisting_transaction_collision_is_never_opened_or_mutated(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    lookalike = data / f"{preparer.TRANSACTION_PREFIX}attacker"
    lookalike.mkdir(mode=0o711)
    lookalike.chmod(0o711)
    marker = lookalike / "payload"
    marker.write_bytes(b"attacker-data")
    marker.chmod(0o640)
    before = (
        _metadata(lookalike),
        lookalike.stat().st_ino,
        _metadata(marker),
        marker.stat().st_ino,
        marker.read_bytes(),
    )
    nonces = iter(("attacker", "fresh-transaction"))
    monkeypatch.setattr(preparer.secrets, "token_hex", lambda _length: next(nonces))

    prepare(data)

    after = (
        _metadata(lookalike),
        lookalike.stat().st_ino,
        _metadata(marker),
        marker.stat().st_ino,
        marker.read_bytes(),
    )
    assert after == before
    assert (operations / preparer.MAINTENANCE_NAME).is_dir()
    assert {path.name for path in transaction_directories(data)} == {
        lookalike.name,
        f"{preparer.TRANSACTION_PREFIX}fresh-transaction",
    }


def test_renameat2_noreplace_is_required_and_fails_without_public_state(
    tmp_path, monkeypatch
):
    data = make_data_root(tmp_path)
    monkeypatch.setattr(preparer, "_RENAMEAT2_FUNCTION", None)
    with pytest.raises(preparer.PreparationError, match="renameat2"):
        prepare(data)
    operations = data / preparer.OPERATIONS_NAME
    assert not (operations / preparer.MAINTENANCE_NAME).exists()
    transactions = transaction_directories(data)
    assert len(transactions) == 1
    assert (transactions[0] / preparer.TRANSACTION_PAYLOAD).is_dir()


def test_same_name_transaction_replacement_is_retained_and_never_path_deleted(
    tmp_path
):
    data = make_data_root(tmp_path)
    escaped = data / "escaped-completed-transaction"
    replacement = None
    replacement_before = None

    def hook(phase):
        nonlocal replacement, replacement_before
        if phase != "after_publish":
            return
        original = transaction_directories(data)[0]
        original.rename(escaped)
        replacement = data / original.name
        replacement.mkdir(mode=0o700)
        replacement.chmod(0o700)
        marker = replacement / "operator-evidence"
        marker.write_bytes(b"retain exactly")
        marker.chmod(0o600)
        replacement_before = (
            replacement.stat().st_ino,
            _metadata(replacement),
            marker.stat().st_ino,
            _metadata(marker),
            marker.read_bytes(),
        )

    prepare(data, _test_hook=hook)

    assert replacement is not None
    marker = replacement / "operator-evidence"
    assert (
        replacement.stat().st_ino,
        _metadata(replacement),
        marker.stat().st_ino,
        _metadata(marker),
        marker.read_bytes(),
    ) == replacement_before
    assert escaped.is_dir()
    assert list(escaped.iterdir()) == []
    assert (data / preparer.OPERATIONS_NAME / preparer.MAINTENANCE_NAME).is_dir()


def test_no_metadata_mutation_occurs_after_atomic_publication(tmp_path, monkeypatch):
    data = make_data_root(tmp_path)
    published = False
    real_fchown = preparer.os.fchown
    real_fchmod = preparer.os.fchmod

    def guarded_fchown(*args):
        assert not published
        return real_fchown(*args)

    def guarded_fchmod(*args):
        assert not published
        return real_fchmod(*args)

    def hook(phase):
        nonlocal published
        if phase == "after_publish":
            published = True

    monkeypatch.setattr(preparer.os, "fchown", guarded_fchown)
    monkeypatch.setattr(preparer.os, "fchmod", guarded_fchmod)
    prepare(data, _test_hook=hook)
    assert published


def test_mount_id_change_is_detected_before_fchown(tmp_path, monkeypatch):
    data = make_data_root(tmp_path)
    operations = data / preparer.OPERATIONS_NAME
    operations.mkdir(mode=0o755)
    operations.chmod(0o755)
    real_mount_id_at = preparer._mount_id_at
    changed = False
    calls = []

    def mount_id_at(parent_fd, name):
        result = real_mount_id_at(parent_fd, name)
        if changed and name == preparer.TRANSACTION_PAYLOAD:
            return result + 1
        return result

    def hook(phase):
        nonlocal changed
        if phase == "before_fchown":
            changed = True

    monkeypatch.setattr(preparer, "_mount_id_at", mount_id_at)
    monkeypatch.setattr(preparer.os, "fchown", lambda *_args: calls.append("chown"))
    with pytest.raises(preparer.PreparationError, match="mount changed"):
        prepare(data, maintenance_gid=TRANSITION_GID, _test_hook=hook)
    assert calls == []


def test_openat2_fallback_fails_closed_without_statx(tmp_path, monkeypatch):
    parent = os.open(tmp_path, preparer._DIRECTORY_FLAGS)
    (tmp_path / "child").mkdir()
    monkeypatch.setattr(preparer, "_openat2_available", False)
    monkeypatch.setattr(preparer, "_STATX_FUNCTION", None)
    try:
        with pytest.raises(preparer.PreparationError, match="statx"):
            preparer._open_at(parent, "child", preparer._DIRECTORY_FLAGS, no_xdev=True)
    finally:
        os.close(parent)


def test_openat2_enosys_uses_independent_statx_mount_guard(tmp_path, monkeypatch):
    parent = os.open(tmp_path, preparer._DIRECTORY_FLAGS)
    (tmp_path / "child").mkdir()

    def unsupported_openat2(*_args):
        import ctypes
        import errno

        ctypes.set_errno(errno.ENOSYS)
        return -1

    monkeypatch.setattr(preparer, "_openat2_available", True)
    monkeypatch.setattr(preparer, "_LIBC", SimpleNamespace(syscall=unsupported_openat2))
    child = -1
    try:
        child = preparer._open_at(
            parent, "child", preparer._DIRECTORY_FLAGS, no_xdev=True
        )
        assert preparer._mount_id_fd(child) == preparer._mount_id_fd(parent)
        assert preparer._openat2_available is False
    finally:
        if child >= 0:
            os.close(child)
        os.close(parent)


def test_device_change_is_rejected_even_when_mount_id_is_stable(tmp_path, monkeypatch):
    parent = os.open(tmp_path, preparer._DIRECTORY_FLAGS)
    (tmp_path / "child").mkdir()
    real_fstat = preparer.os.fstat

    def changed_device(fd):
        details = real_fstat(fd)
        if fd == parent:
            return details
        return SimpleNamespace(st_dev=details.st_dev + 1)

    monkeypatch.setattr(preparer.os, "fstat", changed_device)
    try:
        with pytest.raises(preparer.PreparationError, match="cross-device"):
            preparer._open_at(parent, "child", preparer._DIRECTORY_FLAGS, no_xdev=True)
    finally:
        os.close(parent)


def test_openat2_fallback_rejects_actual_same_filesystem_bind_mount():
    script = r"""
import importlib.util
from importlib.machinery import SourceFileLoader
import os
from pathlib import Path
import subprocess
import sys
import tempfile

source = Path(sys.argv[1])
loader = SourceFileLoader("isolated_preparer", str(source))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source_dir = root / "source"
    target = root / "target"
    source_dir.mkdir()
    target.mkdir()
    subprocess.run(["mount", "--bind", source_dir, target], check=True)
    try:
        assert source_dir.stat().st_dev == target.stat().st_dev
        parent = os.open(root, module._DIRECTORY_FLAGS)
        module._openat2_available = False
        try:
            module._open_at(parent, "target", module._DIRECTORY_FLAGS, no_xdev=True)
        except module.PreparationError as error:
            if "mount boundary" not in str(error):
                raise
        else:
            raise SystemExit("same-filesystem bind mount was accepted")
        finally:
            os.close(parent)
    finally:
        subprocess.run(["umount", target], check=True)
"""
    result = subprocess.run(
        ["unshare", "-Urnm", sys.executable, "-c", script, str(SOURCE)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("user mount namespaces are unavailable")
    assert result.returncode == 0, result.stderr
