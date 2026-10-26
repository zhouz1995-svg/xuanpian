"""Cross-process lifecycle lock for one project database."""

from __future__ import annotations

import errno
from pathlib import Path
import sys
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _lock_file(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        # Windows permits byte-range locks beyond EOF; keep the lock file
        # unchanged and always lock the same byte, including when empty.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProjectDatabaseLockedError(RuntimeError):
    """Raised when another live owner already has a project open."""


class ProjectDatabaseUnsafeLinkError(RuntimeError):
    """Raised when path locking cannot uniquely identify the database inode."""


class ProjectDatabaseLock:
    """An exclusive, non-blocking lock held outside the replaceable database."""

    def __init__(self, database_path: Path, handle: BinaryIO) -> None:
        self._database_path = database_path
        self._handle: BinaryIO | None = handle

    @classmethod
    def acquire(cls, path: Path) -> ProjectDatabaseLock:
        database_path = Path(path).expanduser().resolve()
        lock_path = database_path.with_name(f"{database_path.name}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            _lock_file(handle)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ProjectDatabaseLockedError(
                    "project database is already open; close it and retry: "
                    f"{database_path}"
                ) from error
            raise
        return cls(database_path, handle)

    def assert_owns(self, path: Path) -> None:
        database_path = Path(path).expanduser().resolve()
        if self._handle is None:
            raise RuntimeError("project database lifecycle lock is released")
        if database_path != self._database_path:
            raise RuntimeError(
                "project database lifecycle lock belongs to a different path"
            )

    def assert_safe_database_link(self) -> None:
        self.assert_owns(self._database_path)
        try:
            link_count = self._database_path.stat().st_nlink
        except FileNotFoundError:
            return
        if link_count != 1:
            raise ProjectDatabaseUnsafeLinkError(
                "project database has multiple hard links and cannot be "
                f"opened safely: {self._database_path}"
            )

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock_file(handle)
        finally:
            handle.close()

    def __enter__(self) -> ProjectDatabaseLock:
        self.assert_owns(self._database_path)
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.release()
