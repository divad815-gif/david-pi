"""Descriptor-pinned Linux filesystem operations for managed content.

The portal stores database-selected object names in service-owned directories.
Path validation alone is insufficient because a pathname can be replaced after
validation.  ``PinnedStorageRoot`` records the directory inode once and opens
every entry relative to a freshly verified directory descriptor.  Callers keep
the returned file descriptor through serving or publication, so later pathname
changes cannot redirect the operation.
"""

from __future__ import annotations

import errno
import os
import stat
from contextlib import contextmanager
from pathlib import Path


class StorageSafetyError(OSError):
    """The managed root or one of its entries no longer matches its contract."""


def safe_component(value: str) -> str:
    name = str(value or "")
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\x00" in name
        or os.path.altsep and os.path.altsep in name
    ):
        raise StorageSafetyError("Managed storage name is invalid")
    return name


def safe_relative_parts(value: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return validated POSIX components for one managed relative path.

    Database-backed media predates the flat object store and can contain paths
    such as ``2026/08/object.jpg``.  Parsing the value ourselves avoids the
    normalization performed by ``Path``: empty, dot, and parent components are
    rejected instead of silently collapsed.
    """
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if not raw or raw.startswith(("/", "\\")) or "\x00" in raw or "\\" in raw:
        raise StorageSafetyError("Managed storage path is invalid")
    parts = tuple(raw.split("/"))
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise StorageSafetyError("Managed storage path is invalid")
    return tuple(safe_component(part) for part in parts)


def identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def ensure_restricted_directory(path: Path | str, mode: int = 0o700) -> bool:
    """Create a managed directory with an explicit private mode.

    ``Path.mkdir`` applies the ambient process umask.  Asking for ``0o700``
    prevents a cooperative ``0002`` umask from creating a group-writable
    storage root, while ``fchmod`` ensures unusually restrictive umasks do not
    leave a newly created directory unusable.  Existing directories are never
    chmodded implicitly; their current policy is validated by
    :class:`PinnedStorageRoot`.
    """
    path = Path(path)
    created = False
    try:
        path.mkdir(parents=True, mode=mode)
        created = True
    except FileExistsError:
        pass

    descriptor = -1
    try:
        pathname = path.lstat()
        if path.is_symlink() or not stat.S_ISDIR(pathname.st_mode):
            raise StorageSafetyError("Managed storage root is not a directory")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or identity(opened) != identity(pathname):
            raise StorageSafetyError("Managed storage root changed while opening")
        if created:
            os.fchmod(descriptor, mode)
        return created
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class PinnedStorageRoot:
    """A directory root whose inode is fixed for this application process."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        descriptor = -1
        try:
            pathname = self.path.lstat()
            if self.path.is_symlink() or not stat.S_ISDIR(pathname.st_mode):
                raise StorageSafetyError("Managed storage root is not a directory")
            descriptor = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode) or identity(opened) != identity(pathname):
                raise StorageSafetyError("Managed storage root changed while opening")
            if stat.S_IMODE(opened.st_mode) & 0o022:
                raise StorageSafetyError("Managed storage root is writable by another account")
            self._identity = identity(opened)
            self._device = opened.st_dev
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @contextmanager
    def directory_descriptor(self):
        descriptor = -1
        try:
            try:
                pathname = self.path.lstat()
                if self.path.is_symlink() or identity(pathname) != self._identity:
                    raise StorageSafetyError("Managed storage root identity changed")
                descriptor = os.open(
                    self.path,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                )
                opened = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or identity(opened) != self._identity
                    or opened.st_dev != self._device
                    or stat.S_IMODE(opened.st_mode) & 0o022
                ):
                    raise StorageSafetyError("Managed storage root identity changed")
            except (FileNotFoundError, NotADirectoryError) as error:
                raise StorageSafetyError("Managed storage root is unavailable") from error
            # Exceptions raised by the descriptor-relative operation must keep
            # their original type (especially FileNotFoundError for recovery).
            yield descriptor
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def entry_stat(self, name: str, *, allow_missing: bool = False):
        name = safe_component(name)
        with self.directory_descriptor() as directory:
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                if allow_missing:
                    return None
                raise
            return metadata

    def create_regular(self, name: str, mode: int = 0o600) -> tuple[int, os.stat_result]:
        name = safe_component(name)
        descriptor = -1
        with self.directory_descriptor() as directory:
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    mode,
                    dir_fd=directory,
                )
                opened = os.fstat(descriptor)
                pathname = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != self._device
                    or identity(opened) != identity(pathname)
                    or opened.st_nlink != 1
                ):
                    raise StorageSafetyError("Staged file identity changed")
                os.fchmod(descriptor, mode)
                return descriptor, opened
            except Exception:
                if descriptor >= 0:
                    os.close(descriptor)
                raise

    def open_regular(
        self,
        name: str,
        *,
        expected_size: int | None = None,
    ) -> tuple[int, os.stat_result]:
        name = safe_component(name)
        descriptor = -1
        with self.directory_descriptor() as directory:
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
                opened = os.fstat(descriptor)
                pathname = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != self._device
                    or identity(opened) != identity(pathname)
                    or (expected_size is not None and opened.st_size != int(expected_size))
                ):
                    raise StorageSafetyError("Managed file identity changed")
                return descriptor, opened
            except Exception:
                if descriptor >= 0:
                    os.close(descriptor)
                raise

    @contextmanager
    def descendant_directory_descriptor(self, relative: str | os.PathLike[str]):
        """Open a descendant directory without following any path component.

        Every lookup is relative to the previously opened descriptor.  Once a
        component is opened, replacing its pathname cannot redirect later
        lookups in this walk.
        """
        parts = safe_relative_parts(relative)
        current = -1
        try:
            try:
                with self.directory_descriptor() as root_descriptor:
                    current = os.dup(root_descriptor)
                for component in parts:
                    parent = current
                    current = -1
                    try:
                        current = os.open(
                            component,
                            os.O_RDONLY
                            | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0)
                            | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=parent,
                        )
                        opened = os.fstat(current)
                        pathname = os.stat(
                            component, dir_fd=parent, follow_symlinks=False
                        )
                        if (
                            not stat.S_ISDIR(opened.st_mode)
                            or opened.st_dev != self._device
                            or identity(opened) != identity(pathname)
                            or stat.S_IMODE(opened.st_mode) & 0o022
                        ):
                            raise StorageSafetyError(
                                "Managed storage directory identity changed"
                            )
                    finally:
                        os.close(parent)
            except (FileNotFoundError, NotADirectoryError) as error:
                raise StorageSafetyError(
                    "Managed storage directory is unavailable"
                ) from error
            # Preserve errors from the descriptor-relative caller.  In
            # particular, recovery distinguishes a missing final file from an
            # unsafe or missing ancestor directory.
            yield current
        finally:
            if current >= 0:
                os.close(current)

    def open_regular_path(
        self,
        relative: str | os.PathLike[str],
        *,
        expected_size: int | None = None,
    ) -> tuple[int, os.stat_result]:
        """Open a regular file beneath this root through pinned ancestors."""
        parts = safe_relative_parts(relative)
        if len(parts) == 1:
            return self.open_regular(parts[0], expected_size=expected_size)
        descriptor = -1
        try:
            with self.descendant_directory_descriptor("/".join(parts[:-1])) as directory:
                descriptor = os.open(
                    parts[-1],
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
                opened = os.fstat(descriptor)
                pathname = os.stat(
                    parts[-1], dir_fd=directory, follow_symlinks=False
                )
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_dev != self._device
                    or identity(opened) != identity(pathname)
                    or (
                        expected_size is not None
                        and opened.st_size != int(expected_size)
                    )
                ):
                    raise StorageSafetyError("Managed file identity changed")
                return descriptor, opened
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    def matches_descriptor_path(
        self, relative: str | os.PathLike[str], descriptor: int
    ) -> bool:
        parts = safe_relative_parts(relative)
        if len(parts) == 1:
            return self.matches_descriptor(parts[0], descriptor)
        opened = os.fstat(descriptor)
        with self.descendant_directory_descriptor("/".join(parts[:-1])) as directory:
            try:
                pathname = os.stat(
                    parts[-1], dir_fd=directory, follow_symlinks=False
                )
            except FileNotFoundError:
                return False
            return stat.S_ISREG(pathname.st_mode) and identity(pathname) == identity(
                opened
            )

    def unlink_if_identity_path(
        self, relative: str | os.PathLike[str], expected: tuple[int, int]
    ) -> bool:
        """Unlink a nested entry only while it still names the expected inode."""
        parts = safe_relative_parts(relative)
        if len(parts) == 1:
            return self.unlink_if_identity(parts[0], expected)
        with self.descendant_directory_descriptor("/".join(parts[:-1])) as directory:
            try:
                current = os.stat(
                    parts[-1], dir_fd=directory, follow_symlinks=False
                )
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(current.st_mode) or identity(current) != expected:
                return False
            os.unlink(parts[-1], dir_fd=directory)
            os.fsync(directory)
            return True

    def link_descriptor(self, source_descriptor: int, name: str) -> os.stat_result:
        """Create ``name`` as a hard link to the exact open source descriptor."""
        name = safe_component(name)
        source = os.fstat(source_descriptor)
        if not stat.S_ISREG(source.st_mode) or source.st_dev != self._device:
            raise StorageSafetyError("Upload source is not on the managed filesystem")
        with self.directory_descriptor() as directory:
            try:
                # Following this procfs descriptor symlink binds link(2) to the
                # already-open inode.  It avoids a second lookup of the incoming
                # filename and therefore closes the source-entry swap race.
                os.link(
                    f"/proc/self/fd/{source_descriptor}",
                    name,
                    dst_dir_fd=directory,
                    follow_symlinks=True,
                )
            except FileExistsError:
                raise
            except OSError as error:
                if error.errno in {errno.EXDEV, errno.ENOENT, errno.EPERM}:
                    raise StorageSafetyError("Descriptor-pinned publication is unavailable") from error
                raise
            published = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(published.st_mode) or identity(published) != identity(source):
                raise StorageSafetyError("Published object does not match its source descriptor")
            os.fsync(directory)
            return published

    def matches_descriptor(self, name: str, descriptor: int) -> bool:
        name = safe_component(name)
        opened = os.fstat(descriptor)
        with self.directory_descriptor() as directory:
            try:
                pathname = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return stat.S_ISREG(pathname.st_mode) and identity(pathname) == identity(opened)

    def unlink_if_identity(self, name: str, expected: tuple[int, int]) -> bool:
        """Best-effort cleanup that never deliberately removes a different inode."""
        name = safe_component(name)
        with self.directory_descriptor() as directory:
            try:
                current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not stat.S_ISREG(current.st_mode) or identity(current) != expected:
                return False
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
            return True
