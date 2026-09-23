#!/usr/bin/env python3
"""Descriptor-pinned, mount-confined tree inspection and copying.

This module intentionally does not offer a pathname-based destination API.  A
caller must open and retain the destination root directory and every traversal
below that root uses openat2(RESOLVE_BENEATH|RESOLVE_NO_XDEV|...).  Metadata is
changed only through pinned descriptors.  That makes a late bind mount a
fail-closed event rather than a route to an unrelated filesystem.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fnmatch
import hashlib
import os
import posixpath
from pathlib import PurePosixPath
import stat


SYS_OPENAT2 = 437
RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
AT_EMPTY_PATH = 0x1000
AT_SYMLINK_NOFOLLOW = 0x100
RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)


def after_directory_create(_parent_fd: int, _name: str) -> None:
    """Test seam immediately before NO_XDEV opens a created directory."""


def before_entry_mutation(_parent_fd: int, _name: str, _operation: str) -> None:
    """Test seam immediately before an entry is pinned for mutation."""


def after_symlink_create(_parent_fd: int, _name: str) -> None:
    """Test seam after creation and before a destination symlink is pinned."""


class _OpenHow(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_ulonglong),
        ("mode", ctypes.c_ulonglong),
        ("resolve", ctypes.c_ulonglong),
    ]


def fd_mount_id(descriptor: int) -> int:
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", "r", encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as error:
        raise RuntimeError("could not establish pinned mount identity") from error
    for line in lines:
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdigit():
                return int(value)
    raise RuntimeError("pinned mount identity is unavailable")


def root_identity(descriptor: int) -> tuple[int, int, int, int, int]:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("pinned recovery root is not a directory")
    return (
        metadata.st_dev,
        metadata.st_ino,
        fd_mount_id(descriptor),
        metadata.st_uid,
        stat.S_IMODE(metadata.st_mode),
    )


def format_identity(identity: tuple[int, int, int, int, int]) -> str:
    return ":".join(str(value) for value in identity)


def parse_identity(value: str) -> tuple[int, int, int, int, int]:
    parts = value.split(":") if isinstance(value, str) else []
    if len(parts) != 5 or any(not part.isdigit() for part in parts):
        raise ValueError("pinned root identity is invalid")
    return tuple(int(part) for part in parts)


def path_identity(path) -> tuple[int, int, int, int, int]:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        return root_identity(descriptor)
    finally:
        os.close(descriptor)


def require_path_binding(path, descriptor: int, expected_identity=None) -> None:
    pinned = root_identity(descriptor)
    if expected_identity is not None and pinned != tuple(expected_identity):
        raise RuntimeError("retained recovery root descriptor changed identity")
    try:
        current = path_identity(path)
    except (FileNotFoundError, NotADirectoryError, OSError) as error:
        raise RuntimeError("pinned recovery root pathname was replaced") from error
    if current != pinned:
        raise RuntimeError("pinned recovery root pathname was replaced")


def _component(value: str, label="path component") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\x00" in value
    ):
        raise ValueError(f"{label} is unsafe")
    return value


def relative_parts(value, label="relative path", *, allow_root=False) -> tuple[str, ...]:
    value = str(value)
    if allow_root and value == ".":
        return ()
    relative = PurePosixPath(value)
    if (
        not value
        or relative.is_absolute()
        or "\\" in value
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} is not a canonical relative path")
    return tuple(_component(part, label) for part in relative.parts)


def openat2_component(
    directory_fd: int,
    name: str,
    flags: int,
    *,
    mode: int = 0,
    allow_final_symlink: bool = False,
) -> int:
    _component(name)
    resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV
    if not allow_final_symlink:
        resolve |= RESOLVE_NO_SYMLINKS
    how = _OpenHow(flags=flags | os.O_CLOEXEC, mode=mode, resolve=resolve)
    descriptor = _LIBC.syscall(
        SYS_OPENAT2,
        directory_fd,
        os.fsencode(name),
        ctypes.byref(how),
        ctypes.sizeof(how),
    )
    if descriptor < 0:
        error_number = ctypes.get_errno()
        if error_number == errno.ENOSYS:
            raise RuntimeError("openat2 is required for protected recovery traversal")
        if error_number == errno.EXDEV:
            raise ValueError("protected recovery traversal crossed a mount boundary")
        if error_number in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("protected recovery traversal encountered a link redirect")
        raise OSError(error_number, os.strerror(error_number), name)
    return descriptor


def pin_child(directory_fd: int, name: str) -> int:
    return openat2_component(
        directory_fd,
        name,
        getattr(os, "O_PATH", os.O_RDONLY) | os.O_NOFOLLOW,
        allow_final_symlink=True,
    )


def open_child_directory(directory_fd: int, name: str) -> int:
    return openat2_component(
        directory_fd,
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )


def open_child_file(directory_fd: int, name: str) -> int:
    return openat2_component(directory_fd, name, os.O_RDONLY | os.O_NOFOLLOW)


def create_child_file(
    directory_fd: int,
    name: str,
    *,
    flags: int = os.O_RDWR,
    mode: int = 0o600,
) -> int:
    """Exclusively create a direct regular child without crossing a mount."""
    if flags & (os.O_CREAT | os.O_EXCL):
        raise ValueError("protected file creation flags are managed internally")
    return openat2_component(
        directory_fd,
        name,
        flags | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode=mode,
    )


def rename_child_noreplace(directory_fd: int, source: str, destination: str) -> None:
    """Atomically publish a new direct child without replacing any entry."""
    _component(source, "rename source")
    _component(destination, "rename destination")
    result = _LIBC.renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def open_directory_chain(root_fd: int, parts, *, create=False, mode=0o700) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in tuple(parts):
            _component(part)
            try:
                next_fd = open_child_directory(current_fd, part)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=mode, dir_fd=current_fd)
                os.fsync(current_fd)
                after_directory_create(current_fd, part)
                # If a bind mount was installed over the just-created child,
                # RESOLVE_NO_XDEV rejects it before chmod/chown/copy can occur.
                next_fd = open_child_directory(current_fd, part)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _proc_child(directory_fd: int, name: str) -> str:
    _component(name)
    return f"/proc/self/fd/{directory_fd}/{name}"


def _xattrs_from_fd(descriptor: int) -> list[dict[str, str]]:
    try:
        names = sorted(os.listxattr(descriptor), key=os.fsencode)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return []
        raise RuntimeError("could not read protected extended attributes") from error
    records = []
    for name in names:
        value = os.getxattr(descriptor, name)
        records.append(
            {
                "name": base64.b64encode(os.fsencode(name)).decode("ascii"),
                "value": base64.b64encode(value).decode("ascii"),
            }
        )
    return records


def _xattrs_from_symlink(parent_fd: int, name: str, pinned_fd: int) -> list[dict[str, str]]:
    before = os.fstat(pinned_fd)
    path = _proc_child(parent_fd, name)
    try:
        names = sorted(
            os.listxattr(path, follow_symlinks=False), key=os.fsencode
        )
    except OSError as error:
        if error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            names = []
        else:
            raise RuntimeError("could not read protected symlink attributes") from error
    records = []
    for attr_name in names:
        value = os.getxattr(path, attr_name, follow_symlinks=False)
        records.append(
            {
                "name": base64.b64encode(os.fsencode(attr_name)).decode("ascii"),
                "value": base64.b64encode(value).decode("ascii"),
            }
        )
    after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (after.st_dev, after.st_ino, after.st_mode) != (
        before.st_dev,
        before.st_ino,
        before.st_mode,
    ):
        raise RuntimeError("protected symlink changed during attribute inventory")
    return records


def _decode_xattrs(records) -> list[tuple[bytes, bytes]]:
    if not isinstance(records, list):
        raise ValueError("signed xattr evidence is invalid")
    decoded = []
    previous = None
    for record in records:
        if not isinstance(record, dict) or set(record) != {"name", "value"}:
            raise ValueError("signed xattr evidence is invalid")
        try:
            name = base64.b64decode(record["name"], validate=True)
            value = base64.b64decode(record["value"], validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("signed xattr evidence is invalid") from error
        if not name or b"\x00" in name or previous is not None and name <= previous:
            raise ValueError("signed xattr evidence is not canonical")
        previous = name
        decoded.append((name, value))
    return decoded


def _hash_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(descriptor, 1024 * 1024, offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()


def _safe_symlink_target(relative: str, target: str) -> str:
    if not isinstance(target, str) or not target or os.path.isabs(target):
        raise RuntimeError("protected tree contains an unsafe symlink")
    normalized = posixpath.normpath(
        posixpath.join(posixpath.dirname(relative), target)
    )
    if normalized == ".." or normalized.startswith("../"):
        raise RuntimeError("protected tree contains an escaping symlink")
    return target


def _base_record(metadata, entry_type: str, relative: str, xattrs):
    return {
        "path": relative,
        "type": entry_type,
        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "xattrs": xattrs,
    }


def inventory_tree_fd(root_fd: int, *, excluded=None) -> list[dict]:
    """Return canonical per-entry evidence without following links or mounts."""
    excluded = excluded or (lambda _path, _kind: False)
    root_metadata = os.fstat(root_fd)
    root_mount = fd_mount_id(root_fd)
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("protected tree root is not a directory")
    records = [
        _base_record(root_metadata, "directory", ".", _xattrs_from_fd(root_fd))
    ]
    hardlinks: dict[tuple[int, int], str] = {}

    def walk(directory_fd: int, prefix: str):
        directory_before = os.fstat(directory_fd)
        if directory_before.st_dev != root_metadata.st_dev or fd_mount_id(directory_fd) != root_mount:
            raise ValueError("protected tree crossed a mount boundary")
        for name in sorted(os.listdir(directory_fd), key=os.fsencode):
            pinned_fd = pin_child(directory_fd, name)
            try:
                metadata = os.fstat(pinned_fd)
                if metadata.st_dev != root_metadata.st_dev or fd_mount_id(pinned_fd) != root_mount:
                    raise ValueError("protected tree crossed a mount boundary")
                relative = f"{prefix}/{name}" if prefix else name
                kind = (
                    "directory" if stat.S_ISDIR(metadata.st_mode)
                    else "file" if stat.S_ISREG(metadata.st_mode)
                    else "symlink" if stat.S_ISLNK(metadata.st_mode)
                    else "unsupported"
                )
                if excluded(relative, kind):
                    continue
                if kind == "directory":
                    child_fd = open_child_directory(directory_fd, name)
                    try:
                        records.append(
                            _base_record(
                                os.fstat(child_fd),
                                "directory",
                                relative,
                                _xattrs_from_fd(child_fd),
                            )
                        )
                        walk(child_fd, relative)
                    finally:
                        os.close(child_fd)
                    continue
                if kind == "file":
                    file_fd = open_child_file(directory_fd, name)
                    try:
                        opened = os.fstat(file_fd)
                        inode = (opened.st_dev, opened.st_ino)
                        hardlink_to = hardlinks.get(inode)
                        if hardlink_to is None:
                            hardlinks[inode] = relative
                        record = _base_record(
                            opened,
                            "file",
                            relative,
                            _xattrs_from_fd(file_fd),
                        )
                        record.update(
                            {
                                "size": opened.st_size,
                                "sha256": _hash_fd(file_fd),
                                "hardlink_to": hardlink_to,
                            }
                        )
                        after = os.fstat(file_fd)
                        if (
                            after.st_dev != opened.st_dev
                            or after.st_ino != opened.st_ino
                            or after.st_size != opened.st_size
                            or after.st_mtime_ns != opened.st_mtime_ns
                            or after.st_ctime_ns != opened.st_ctime_ns
                        ):
                            raise RuntimeError("protected source file changed during inventory")
                        records.append(record)
                    finally:
                        os.close(file_fd)
                    continue
                if kind == "symlink":
                    target = _safe_symlink_target(
                        relative, os.readlink(name, dir_fd=directory_fd)
                    )
                    record = _base_record(
                        metadata,
                        "symlink",
                        relative,
                        _xattrs_from_symlink(directory_fd, name, pinned_fd),
                    )
                    record["target"] = target
                    records.append(record)
                    continue
                raise ValueError("protected tree contains an unsupported entry")
            finally:
                os.close(pinned_fd)
        directory_after = os.fstat(directory_fd)
        if (
            directory_after.st_dev != directory_before.st_dev
            or directory_after.st_ino != directory_before.st_ino
            or fd_mount_id(directory_fd) != root_mount
        ):
            raise RuntimeError("protected source directory changed during inventory")

    walk(root_fd, "")
    return records


def data_backup_excluded(relative: str, kind: str) -> bool:
    parts = PurePosixPath(relative).parts
    if parts[:1] in {("incoming",), ("tmp",)}:
        return True
    if parts[:2] == ("files", "incoming"):
        return True
    if kind == "file" and any(
        PurePosixPath(relative).name.endswith(suffix)
        for suffix in (".db", ".db-wal", ".db-shm")
    ):
        return True
    return False


def basename_glob_excluded(pattern: str):
    if not isinstance(pattern, str) or "/" in pattern or not pattern:
        raise ValueError("copy basename pattern is invalid")

    def excluded(relative: str, kind: str) -> bool:
        # Matching top-level files are copied. Directories are never traversed.
        return kind != "file" or "/" in relative or not fnmatch.fnmatchcase(relative, pattern)

    return excluded


def validate_evidence(records) -> list[dict]:
    if not isinstance(records, list) or not records:
        raise ValueError("signed tree evidence is missing")
    seen = set()
    file_paths = set()
    canonical = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError("signed tree evidence is invalid")
        kind = record.get("type")
        common = {"path", "type", "mode", "uid", "gid", "xattrs"}
        expected = (
            common
            if kind == "directory"
            else common | {"size", "sha256", "hardlink_to"}
            if kind == "file"
            else common | {"target"}
            if kind == "symlink"
            else set()
        )
        if not expected or set(record) != expected:
            raise ValueError("signed tree evidence has an invalid schema")
        path = record["path"]
        parts = relative_parts(path, "signed evidence path", allow_root=True)
        if index == 0 and (path != "." or kind != "directory"):
            raise ValueError("signed tree evidence has no canonical root")
        if index and path == ".":
            raise ValueError("signed tree evidence duplicates its root")
        if path in seen:
            raise ValueError("signed tree evidence contains duplicate paths")
        seen.add(path)
        if not isinstance(record["mode"], str) or not record["mode"].isdigit() or len(record["mode"]) != 4:
            raise ValueError("signed tree evidence mode is invalid")
        mode = int(record["mode"], 8)
        if mode < 0 or mode > 0o7777:
            raise ValueError("signed tree evidence mode is invalid")
        for field in ("uid", "gid"):
            if type(record[field]) is not int or record[field] < 0:
                raise ValueError("signed tree evidence ownership is invalid")
        _decode_xattrs(record["xattrs"])
        if kind == "file":
            if type(record["size"]) is not int or record["size"] < 0:
                raise ValueError("signed tree evidence size is invalid")
            if (
                not isinstance(record["sha256"], str)
                or len(record["sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in record["sha256"])
            ):
                raise ValueError("signed tree evidence hash is invalid")
            linked = record["hardlink_to"]
            if linked is not None and (
                linked not in file_paths
                or len(relative_parts(linked, "signed hardlink path")) == 0
            ):
                raise ValueError("signed hardlink evidence is invalid")
            file_paths.add(path)
        elif kind == "symlink":
            _safe_symlink_target(path, record["target"])
            # Linux does not expose an fd-only API for changing a symlink's
            # xattrs. Refuse such snapshots instead of using a raceable path.
            if record["xattrs"]:
                raise ValueError("signed symlink xattrs cannot be restored safely")
        if parts:
            parent = PurePosixPath(*parts[:-1]).as_posix() if len(parts) > 1 else "."
            if parent not in seen:
                raise ValueError("signed tree evidence is not parent ordered")
        canonical.append(record)
    return canonical


def _record_xattrs_equal(descriptor: int, record: dict) -> bool:
    return _xattrs_from_fd(descriptor) == record["xattrs"]


def _verify_open_record(descriptor: int, record: dict, root_dev: int, root_mount: int):
    metadata = os.fstat(descriptor)
    expected_type = record["type"]
    actual = (
        "directory" if stat.S_ISDIR(metadata.st_mode)
        else "file" if stat.S_ISREG(metadata.st_mode)
        else "symlink" if stat.S_ISLNK(metadata.st_mode)
        else "unsupported"
    )
    if (
        actual != expected_type
        or metadata.st_dev != root_dev
        or fd_mount_id(descriptor) != root_mount
        or format(stat.S_IMODE(metadata.st_mode), "04o") != record["mode"]
        or metadata.st_uid != record["uid"]
        or metadata.st_gid != record["gid"]
    ):
        raise RuntimeError("source entry does not match signed evidence")
    if expected_type == "file" and (
        metadata.st_size != record["size"]
        or _hash_fd(descriptor) != record["sha256"]
        or not _record_xattrs_equal(descriptor, record)
    ):
        raise RuntimeError("source file does not match signed evidence")
    if expected_type == "directory" and not _record_xattrs_equal(descriptor, record):
        raise RuntimeError("source directory does not match signed evidence")
    return metadata


def verify_open_evidence_record(
    descriptor: int,
    record: dict,
    root_fd: int,
    *,
    require_unique: bool = False,
):
    """Verify an already-pinned file/directory against immutable evidence."""
    root = os.fstat(root_fd)
    metadata = _verify_open_record(
        descriptor,
        record,
        root.st_dev,
        fd_mount_id(root_fd),
    )
    if require_unique and metadata.st_nlink != 1:
        raise RuntimeError("protected evidence target is not uniquely contained")
    return metadata


def _open_record(root_fd: int, path: str, expected_type: str):
    parts = relative_parts(path, "signed evidence path", allow_root=True)
    if not parts:
        return os.dup(root_fd), None, None
    parent_fd = open_directory_chain(root_fd, parts[:-1], create=False)
    try:
        name = parts[-1]
        descriptor = (
            open_child_directory(parent_fd, name)
            if expected_type == "directory"
            else open_child_file(parent_fd, name)
            if expected_type == "file"
            else pin_child(parent_fd, name)
        )
        return descriptor, parent_fd, name
    except Exception:
        os.close(parent_fd)
        raise


def _apply_fd_metadata(descriptor: int, record: dict):
    os.fchown(descriptor, record["uid"], record["gid"])
    os.fchmod(descriptor, int(record["mode"], 8))
    expected_names = set()
    for raw_name, value in _decode_xattrs(record["xattrs"]):
        name = os.fsdecode(raw_name)
        os.setxattr(descriptor, name, value)
        expected_names.add(raw_name)
    for current in os.listxattr(descriptor):
        if os.fsencode(current) not in expected_names:
            os.removexattr(descriptor, current)


def _fchown_symlink(descriptor: int, uid: int, gid: int):
    result = _LIBC.fchownat(
        descriptor,
        ctypes.c_char_p(b""),
        uid,
        gid,
        AT_EMPTY_PATH | AT_SYMLINK_NOFOLLOW,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def clear_directory_fd(directory_fd: int, *, root_dev=None, root_mount=None):
    """Remove contents while every descent is descriptor-pinned and NO_XDEV."""
    metadata = os.fstat(directory_fd)
    root_dev = metadata.st_dev if root_dev is None else root_dev
    root_mount = fd_mount_id(directory_fd) if root_mount is None else root_mount
    if metadata.st_dev != root_dev or fd_mount_id(directory_fd) != root_mount:
        raise ValueError("mutable tree crossed a mount boundary")
    for name in sorted(os.listdir(directory_fd), key=os.fsencode):
        before_entry_mutation(directory_fd, name, "remove")
        pinned_fd = pin_child(directory_fd, name)
        try:
            child = os.fstat(pinned_fd)
            if child.st_dev != root_dev or fd_mount_id(pinned_fd) != root_mount:
                raise ValueError("mutable tree crossed a mount boundary")
            if stat.S_ISDIR(child.st_mode):
                child_fd = open_child_directory(directory_fd, name)
                try:
                    clear_directory_fd(
                        child_fd, root_dev=root_dev, root_mount=root_mount
                    )
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            elif stat.S_ISREG(child.st_mode) or stat.S_ISLNK(child.st_mode):
                if child.st_nlink != 1:
                    raise ValueError("mutable tree contains a shared inode")
                os.unlink(name, dir_fd=directory_fd)
            else:
                raise ValueError("mutable tree contains an unsafe entry")
        finally:
            os.close(pinned_fd)
    os.fsync(directory_fd)


def _selected_records(records: list[dict], selected_paths=None) -> list[dict]:
    if selected_paths is None:
        return records
    selected = set(selected_paths)
    available_files = {
        record["path"] for record in records if record["type"] == "file"
    }
    if not selected <= available_files:
        raise ValueError("selected restore set is not in signed evidence")
    needed = {"."}
    for path in selected:
        parts = relative_parts(path)
        for index in range(1, len(parts)):
            needed.add(PurePosixPath(*parts[:index]).as_posix())
        needed.add(path)
    return [record for record in records if record["path"] in needed]


def copy_evidence_tree(
    source_root_fd: int,
    destination_root_fd: int,
    evidence,
    *,
    selected_paths=None,
    preserve_destination_root=False,
    clear_destination=False,
    require_distinct_filesystem=True,
) -> list[dict]:
    """Copy exactly signed/current evidence through pinned descriptors."""
    records = validate_evidence(evidence)
    records_to_copy = _selected_records(records, selected_paths)
    source_root = os.fstat(source_root_fd)
    destination_root = os.fstat(destination_root_fd)
    source_mount = fd_mount_id(source_root_fd)
    destination_mount = fd_mount_id(destination_root_fd)
    if (
        require_distinct_filesystem
        and (
            source_root.st_dev == destination_root.st_dev
            or source_mount == destination_mount
        )
    ):
        # This primitive is used for restore and physically independent backup;
        # same-tree copying would make clearing catastrophically ambiguous.
        raise ValueError("source and destination trees must be filesystem-distinct")
    if clear_destination:
        clear_directory_fd(
            destination_root_fd,
            root_dev=destination_root.st_dev,
            root_mount=destination_mount,
        )

    root_record = records[0]
    source_root_copy = os.dup(source_root_fd)
    try:
        _verify_open_record(
            source_root_copy, root_record, source_root.st_dev, source_mount
        )
    finally:
        os.close(source_root_copy)

    # Create directories before leaves. Metadata is applied bottom-up after
    # population so restrictive source modes do not impede construction.
    for record in records_to_copy:
        if record["path"] == "." or record["type"] != "directory":
            continue
        parts = relative_parts(record["path"])
        parent_fd = open_directory_chain(destination_root_fd, parts[:-1], create=False)
        try:
            os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
            after_directory_create(parent_fd, parts[-1])
            child_fd = open_child_directory(parent_fd, parts[-1])
            os.close(child_fd)
        finally:
            os.close(parent_fd)

    copied_files: dict[str, tuple[int, str]] = {}
    try:
        for record in records_to_copy:
            if record["type"] == "directory":
                continue
            source_fd, source_parent_fd, source_name = _open_record(
                source_root_fd, record["path"], record["type"]
            )
            parts = relative_parts(record["path"])
            destination_parent_fd = open_directory_chain(
                destination_root_fd, parts[:-1], create=False
            )
            destination_fd = None
            try:
                if record["type"] == "file":
                    _verify_open_record(
                        source_fd, record, source_root.st_dev, source_mount
                    )
                    linked = record["hardlink_to"]
                    if linked is not None and linked in copied_files:
                        first_fd, _first_path = copied_files[linked]
                        os.link(
                            f"/proc/self/fd/{first_fd}",
                            parts[-1],
                            dst_dir_fd=destination_parent_fd,
                            follow_symlinks=True,
                        )
                        destination_fd = open_child_file(
                            destination_parent_fd, parts[-1]
                        )
                    else:
                        destination_fd = create_child_file(
                            destination_parent_fd,
                            parts[-1],
                            flags=os.O_RDWR,
                            mode=0o600,
                        )
                        digest = hashlib.sha256()
                        offset = 0
                        while True:
                            block = os.pread(source_fd, 1024 * 1024, offset)
                            if not block:
                                break
                            digest.update(block)
                            written = 0
                            while written < len(block):
                                count = os.write(destination_fd, block[written:])
                                if count <= 0:
                                    raise OSError("short protected restore write")
                                written += count
                            offset += len(block)
                        if digest.hexdigest() != record["sha256"] or offset != record["size"]:
                            raise RuntimeError("source file changed after signed verification")
                        _apply_fd_metadata(destination_fd, record)
                        os.fsync(destination_fd)
                        copied_files[record["path"]] = (
                            os.dup(destination_fd),
                            record["path"],
                        )
                    copied = os.fstat(destination_fd)
                    if (
                        copied.st_dev != destination_root.st_dev
                        or fd_mount_id(destination_fd) != destination_mount
                        or copied.st_size != record["size"]
                        or _hash_fd(destination_fd) != record["sha256"]
                        or format(stat.S_IMODE(copied.st_mode), "04o") != record["mode"]
                        or copied.st_uid != record["uid"]
                        or copied.st_gid != record["gid"]
                        or not _record_xattrs_equal(destination_fd, record)
                    ):
                        raise RuntimeError("protected copy does not match signed evidence")
                    _verify_open_record(
                        source_fd, record, source_root.st_dev, source_mount
                    )
                else:
                    source_metadata = os.fstat(source_fd)
                    if (
                        source_metadata.st_dev != source_root.st_dev
                        or fd_mount_id(source_fd) != source_mount
                        or format(stat.S_IMODE(source_metadata.st_mode), "04o")
                        != record["mode"]
                        or source_metadata.st_uid != record["uid"]
                        or source_metadata.st_gid != record["gid"]
                        or record["xattrs"]
                    ):
                        raise RuntimeError("source symlink does not match signed evidence")
                    actual_target = os.readlink(source_name, dir_fd=source_parent_fd)
                    if actual_target != record["target"]:
                        raise RuntimeError("source symlink does not match signed evidence")
                    os.symlink(
                        record["target"], parts[-1], dir_fd=destination_parent_fd
                    )
                    after_symlink_create(destination_parent_fd, parts[-1])
                    destination_fd = pin_child(destination_parent_fd, parts[-1])
                    copied = os.fstat(destination_fd)
                    if (
                        not stat.S_ISLNK(copied.st_mode)
                        or copied.st_dev != destination_root.st_dev
                        or fd_mount_id(destination_fd) != destination_mount
                        or os.readlink("", dir_fd=destination_fd)
                        != record["target"]
                    ):
                        raise RuntimeError(
                            "protected symlink changed before metadata application"
                        )
                    _fchown_symlink(destination_fd, record["uid"], record["gid"])
                    copied = os.fstat(destination_fd)
                    if (
                        not stat.S_ISLNK(copied.st_mode)
                        or copied.st_dev != destination_root.st_dev
                        or fd_mount_id(destination_fd) != destination_mount
                        or copied.st_uid != record["uid"]
                        or copied.st_gid != record["gid"]
                        or os.readlink("", dir_fd=destination_fd)
                        != record["target"]
                    ):
                        raise RuntimeError("protected symlink copy does not match evidence")
                os.fsync(destination_parent_fd)
            except Exception:
                if destination_fd is not None:
                    os.close(destination_fd)
                    destination_fd = None
                # Do not clean up by pathname here.  A concurrent replacement
                # could turn cleanup into an unlink of an unrelated hardlink.
                # The disposable/incomplete destination remains failed closed.
                raise
            finally:
                if destination_fd is not None:
                    os.close(destination_fd)
                os.close(destination_parent_fd)
                os.close(source_fd)
                if source_parent_fd is not None:
                    os.close(source_parent_fd)

        for record in sorted(
            (item for item in records_to_copy if item["type"] == "directory"),
            key=lambda item: len(relative_parts(item["path"], allow_root=True)),
            reverse=True,
        ):
            if record["path"] == "." and preserve_destination_root:
                continue
            directory_fd, parent_fd, _name = _open_record(
                destination_root_fd, record["path"], "directory"
            )
            try:
                if directory_fd != destination_root_fd:
                    _apply_fd_metadata(directory_fd, record)
                    os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
                if parent_fd is not None:
                    os.close(parent_fd)
        os.fsync(destination_root_fd)
    finally:
        for descriptor, _path in copied_files.values():
            os.close(descriptor)
    return records_to_copy


def signed_sample_paths(records, count: int) -> list[str]:
    records = validate_evidence(records)
    if type(count) is not int or count < 0:
        raise ValueError("sample count must be a non-negative integer")
    files = [record["path"] for record in records if record["type"] == "file"]
    return sorted(
        files,
        key=lambda path: hashlib.sha256(path.encode("utf-8")).digest(),
    )[:count]
