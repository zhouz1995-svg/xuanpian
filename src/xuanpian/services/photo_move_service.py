"""Plan and execute recoverable, no-clobber logical-photo moves."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from time import time_ns
from uuid import uuid4

from ..domain.culling import PhotoMark, normalize_legacy_mark
from ..domain.photo import FileFingerprint, PhotoAssetRecord, PhotoRecord
from ..domain.photo_move import MoveItem, PhotoMoveTransaction


_STATUS_DIRECTORY_BY_MARK = {
    PhotoMark.KEEP: PhotoMark.KEEP.value,
    PhotoMark.HOLD: PhotoMark.HOLD.value,
    PhotoMark.DISCARD: PhotoMark.DISCARD.value,
}

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_WINDOWS_ERROR_FILE_NOT_FOUND = 2
_WINDOWS_ERROR_PATH_NOT_FOUND = 3
_WINDOWS_ERROR_ACCESS_DENIED = 5
_WINDOWS_ERROR_NOT_SAME_DEVICE = 17
_WINDOWS_ERROR_FILE_EXISTS = 80
_WINDOWS_ERROR_ALREADY_EXISTS = 183


def _raise_posix_rename_error(source: Path, target: Path) -> None:
    error_number = ctypes.get_errno() or errno.EIO
    message = os.strerror(error_number)
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, message, str(target))
    raise OSError(error_number, message, str(source), str(target))


def _rename_no_replace_macos(
    source: Path,
    target: Path,
    *,
    operation: Callable[[bytes, bytes, int], int] | None = None,
) -> None:
    """Use Darwin's atomic ``renamex_np(..., RENAME_EXCL)`` primitive."""
    if operation is None:
        library = ctypes.CDLL(None, use_errno=True)
        operation = getattr(library, "renamex_np", None)
        if operation is None:
            raise OSError(
                errno.ENOTSUP,
                "renamex_np(RENAME_EXCL) is unavailable",
                str(target),
            )
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = operation(os.fsencode(source), os.fsencode(target), _RENAME_EXCL)
    if result != 0:
        _raise_posix_rename_error(source, target)


def _rename_no_replace_linux(
    source: Path,
    target: Path,
    *,
    operation: Callable[[int, bytes, int, bytes, int], int] | None = None,
) -> None:
    """Use Linux's atomic ``renameat2(..., RENAME_NOREPLACE)`` primitive."""
    if operation is None:
        library = ctypes.CDLL(None, use_errno=True)
        operation = getattr(library, "renameat2", None)
        if operation is None:
            raise OSError(
                errno.ENOTSUP,
                "renameat2(RENAME_NOREPLACE) is unavailable",
                str(target),
            )
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        operation.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = operation(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in (errno.ENOSYS, errno.EINVAL):
            raise OSError(
                errno.ENOTSUP,
                "renameat2(RENAME_NOREPLACE) is unsupported",
                str(target),
            )
        _raise_posix_rename_error(source, target)


def _rename_no_replace_windows(
    source: Path,
    target: Path,
    *,
    operation: Callable[[str, str], int] | None = None,
    get_last_error: Callable[[], int] | None = None,
) -> None:
    """Use Windows ``MoveFileW`` without any replacement flag."""
    if operation is None:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise OSError(
                errno.ENOTSUP,
                "Windows MoveFileW is unavailable",
                str(target),
            )
        kernel32 = win_dll("kernel32", use_last_error=True)
        operation = kernel32.MoveFileW
        operation.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        operation.restype = ctypes.c_int
    if get_last_error is None:
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
    if operation(str(source), str(target)):
        return
    windows_error = get_last_error()
    if windows_error in (
        _WINDOWS_ERROR_FILE_EXISTS,
        _WINDOWS_ERROR_ALREADY_EXISTS,
    ):
        raise FileExistsError(errno.EEXIST, "target already exists", str(target))
    mapped_errno = {
        _WINDOWS_ERROR_FILE_NOT_FOUND: errno.ENOENT,
        _WINDOWS_ERROR_PATH_NOT_FOUND: errno.ENOENT,
        _WINDOWS_ERROR_ACCESS_DENIED: errno.EACCES,
        _WINDOWS_ERROR_NOT_SAME_DEVICE: errno.EXDEV,
    }.get(windows_error, errno.EIO)
    raise OSError(
        mapped_errno,
        f"MoveFileW failed with Windows error {windows_error}",
        str(target),
    )


def _native_rename_no_replace(source: Path, target: Path) -> None:
    """Dispatch only to a proven atomic no-replace platform primitive."""
    if sys.platform == "darwin":
        _rename_no_replace_macos(source, target)
        return
    if sys.platform.startswith("linux"):
        _rename_no_replace_linux(source, target)
        return
    if sys.platform == "win32":
        _rename_no_replace_windows(source, target)
        return
    raise OSError(
        errno.ENOTSUP,
        f"atomic no-replace rename is unsupported on {sys.platform}",
        str(target),
    )


class PhotoMoveVerificationError(OSError):
    """Raised when a path no longer has the transaction's fingerprint."""


class PhotoMoveTargetConflictError(FileExistsError):
    """Raised with every target collision found by one preflight pass."""

    def __init__(
        self,
        recovery_paths: tuple[Path, ...],
        unresolved_ordinals: tuple[int, ...],
    ) -> None:
        message = "target already exists: " + ", ".join(
            str(path) for path in recovery_paths
        )
        super().__init__(errno.EEXIST, message, str(recovery_paths[0]))
        self.recovery_paths = recovery_paths
        self.unresolved_ordinals = unresolved_ordinals


class PhotoMoveAtomicConflictError(FileExistsError):
    """Raised when a foreign target wins the atomic rename race."""

    def __init__(self, target: Path) -> None:
        super().__init__(
            errno.EEXIST,
            f"target appeared before atomic rename: {target}",
            str(target),
        )
        self.recovery_paths = (target,)


@dataclass(frozen=True, slots=True)
class MoveExecutionResult:
    """Immutable evidence from one attempted logical-photo move."""

    success: bool
    completed_ordinals: tuple[int, ...] = ()
    moved_assets: tuple[PhotoAssetRecord, ...] = ()
    recovery_paths: tuple[Path, ...] = ()
    error: str = ""
    rollback_complete: bool | None = None
    rolled_back_ordinals: tuple[int, ...] = ()
    unresolved_ordinals: tuple[int, ...] = ()


class RecoveryDecision(Enum):
    """One safe startup action derived from the current filesystem state."""

    ABORTED = "aborted"
    ROLLED_BACK = "rolled_back"
    READY_TO_COMMIT = "ready_to_commit"
    NEEDS_USER = "needs_user"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    """Structured evidence for a caller recovering one persisted move."""

    decision: RecoveryDecision
    moved_assets: tuple[PhotoAssetRecord, ...] = ()
    recovery_paths: tuple[Path, ...] = ()
    error: str = ""
    rolled_back_ordinals: tuple[int, ...] = ()
    unresolved_ordinals: tuple[int, ...] = ()
    observed_completed_ordinals: frozenset[int] = frozenset()
    journal_completed_ordinals: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class _CreatedDirectory:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _RollbackOutcome:
    recovery_paths: tuple[Path, ...]
    errors: tuple[str, ...]
    rolled_back_ordinals: tuple[int, ...]
    unresolved_ordinals: tuple[int, ...]


class _RecoveryLocation(Enum):
    ABSENT = "absent"
    EXACT = "exact"
    OTHER = "other"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class _RecoveryObservation:
    location: _RecoveryLocation
    error: str = ""


class PhotoMoveService:
    """Filesystem-only planner/executor for one logical photo at a time."""

    def plan(
        self,
        photo: PhotoRecord,
        target_mark: PhotoMark,
    ) -> PhotoMoveTransaction:
        """Return a fully preflighted immutable move transaction.

        Planning is deliberately side-effect free: missing status directories
        are checked through their original parent but are not created here.
        Execution performs the same preflight again after the journal can have
        been persisted by the caller.
        """
        transaction = self.prepare_plan(photo, target_mark)
        self.preflight(transaction)
        return transaction

    def prepare_plan(
        self, photo: PhotoRecord, target_mark: PhotoMark,
    ) -> PhotoMoveTransaction:
        """Construct a journal from persisted values, without filesystem I/O."""
        if not isinstance(photo, PhotoRecord):
            raise ValueError("photo must be a PhotoRecord")
        if not isinstance(photo.id, str) or not photo.id:
            raise ValueError("photo id must be stable and non-empty")
        if not isinstance(target_mark, PhotoMark):
            raise ValueError("target_mark must be a managed PhotoMark")
        if not isinstance(photo.mark, str):
            raise ValueError("photo mark is not managed")
        before_mark = normalize_legacy_mark(photo.mark)
        if before_mark is None:
            raise ValueError("photo mark is not managed")
        assets = photo.source_assets
        if not assets:
            raise ValueError("photo has no source assets")

        items: list[MoveItem] = []
        for ordinal, asset in enumerate(assets):
            source = Path(asset.fingerprint.path)
            origin = Path(asset.origin_path)
            if not source.is_absolute():
                raise ValueError("source path must be absolute")
            if not origin.is_absolute():
                raise ValueError("origin path must be absolute")
            if source.name != origin.name:
                raise ValueError("photo move cannot silently rename an asset")
            target = self._target_for_origin(origin, target_mark)
            items.append(
                MoveItem(
                    ordinal=ordinal,
                    kind=asset.kind,
                    source_path=asset.fingerprint.path,
                    target_path=str(target),
                    size=asset.fingerprint.size,
                    mtime_ns=asset.fingerprint.mtime_ns,
                )
            )

        transaction = PhotoMoveTransaction(
            id=str(uuid4()),
            photo_id=photo.id,
            before_mark=before_mark,
            after_mark=target_mark,
            created_at_ns=time_ns(),
            items=tuple(items),
        )
        self._validate_item_relationships(transaction)
        return transaction

    def preflight(self, plan: PhotoMoveTransaction) -> None:
        """Check storage without mutating it; callers must run off the UI."""
        self._preflight(plan)

    def prepare_inverse(self, original: PhotoMoveTransaction) -> PhotoMoveTransaction:
        """Build the exact inverse, including legacy marked-at-origin paths."""
        self._validate_item_relationships(original)
        if original.inverse_of is not None:
            # Reversing an undo for persistence rollback is the same forward
            # action, never a recursively nested provenance chain.
            return replace(original.inverse_of, id=str(uuid4()), created_at_ns=time_ns())
        return PhotoMoveTransaction(
            id=str(uuid4()), photo_id=original.photo_id,
            before_mark=original.after_mark, after_mark=original.before_mark,
            created_at_ns=time_ns(),
            items=tuple(replace(item, source_path=item.target_path, target_path=item.source_path)
                        for item in original.items),
            inverse_of=original,
        )

    def execute(
        self,
        plan: PhotoMoveTransaction,
        on_completed: Callable[[int], None],
        *,
        should_cancel_before_first_move: Callable[[], bool] | None = None,
    ) -> MoveExecutionResult:
        """Execute a plan and roll every known move back on any failure."""
        moved: list[MoveItem] = []
        completed_ordinals: list[int] = []
        created_directories: list[_CreatedDirectory] = []
        current_item: MoveItem | None = None
        try:
            if not callable(on_completed):
                raise ValueError("on_completed must be callable")
            if (
                should_cancel_before_first_move is not None
                and not callable(should_cancel_before_first_move)
            ):
                raise ValueError(
                    "should_cancel_before_first_move must be callable"
                )
            self._preflight(plan)
            created_directories = self._create_target_directories(plan.items)
            for item in plan.items:
                current_item = item
                source = Path(item.source_path)
                target = Path(item.target_path)
                if (
                    not moved
                    and should_cancel_before_first_move is not None
                    and should_cancel_before_first_move()
                ):
                    self._remove_owned_empty_directories(created_directories)
                    return MoveExecutionResult(
                        success=False,
                        error="canceled before filesystem execution",
                        rollback_complete=True,
                    )
                self._move_no_clobber(source, target, item)
                moved.append(item)
                self._verify_fingerprint(target, item)
                on_completed(item.ordinal)
                completed_ordinals.append(item.ordinal)
                current_item = None
        except BaseException as error:
            # A backend wrapper can fail after the atomic rename reached the
            # filesystem.  If the source vanished and an exact target remains,
            # include it in rollback even though no journal callback ran.
            if (
                current_item is not None
                and current_item not in moved
                and not self._lexists(Path(current_item.source_path))
                and self._matches_fingerprint(
                    Path(current_item.target_path),
                    current_item,
                )
            ):
                moved.append(current_item)
            rollback = self._rollback(moved)
            recovery_paths = self._recovery_paths_not_confirmed_absent(
                (
                    *self._error_recovery_paths(error),
                    *rollback.recovery_paths,
                )
            )
            self._remove_owned_empty_directories(created_directories)
            message = self._error_text(error)
            if rollback.errors:
                message = f"{message}; rollback: {'; '.join(rollback.errors)}"
            unresolved = set(rollback.unresolved_ordinals)
            unresolved.update(self._error_unresolved_ordinals(error))
            for item in plan.items if isinstance(plan, PhotoMoveTransaction) else ():
                if not self._matches_fingerprint(Path(item.source_path), item):
                    unresolved.add(item.ordinal)
            if current_item is not None and any(
                path in recovery_paths
                for path in self._error_recovery_paths(error)
            ):
                unresolved.add(current_item.ordinal)
            unresolved_ordinals = tuple(sorted(unresolved))
            return MoveExecutionResult(
                success=False,
                completed_ordinals=tuple(completed_ordinals),
                recovery_paths=recovery_paths,
                error=message,
                rollback_complete=(
                    not unresolved_ordinals and not recovery_paths
                ),
                rolled_back_ordinals=rollback.rolled_back_ordinals,
                unresolved_ordinals=unresolved_ordinals,
            )

        return MoveExecutionResult(
            success=True,
            completed_ordinals=tuple(completed_ordinals),
            moved_assets=self._moved_assets(plan),
        )

    def recover_pending_move(
        self,
        transaction: PhotoMoveTransaction,
        *,
        completed_ordinals: Iterable[int] = (),
    ) -> RecoveryResult:
        """Recover one durable journal using filesystem truth without guessing.

        Completion flags are retained as corroborating evidence only.  The
        decision is derived from non-symlink regular files whose exact size and
        nanosecond mtime match the immutable move plan.
        """
        journal_completed = self._validated_completed_ordinals(
            transaction,
            completed_ordinals,
        )
        try:
            self._validate_item_relationships(transaction)
        except (TypeError, ValueError):
            return self._needs_user_result(
                transaction,
                tuple(item.ordinal for item in transaction.items),
                error="invalid persisted move plan",
                journal_completed=journal_completed,
            )

        source_ordinals: list[int] = []
        target_ordinals: list[int] = []
        unresolved_ordinals: list[int] = []
        errors: list[str] = []
        for item in transaction.items:
            source = self._observe_recovery_path(
                Path(item.source_path),
                item,
            )
            target = self._observe_recovery_path(
                Path(item.target_path),
                item,
            )
            if (
                source.location is _RecoveryLocation.EXACT
                and target.location is _RecoveryLocation.ABSENT
            ):
                source_ordinals.append(item.ordinal)
            elif (
                source.location is _RecoveryLocation.ABSENT
                and target.location is _RecoveryLocation.EXACT
            ):
                target_ordinals.append(item.ordinal)
            else:
                unresolved_ordinals.append(item.ordinal)
                for observation in (source, target):
                    if observation.error:
                        errors.append(observation.error)

        observed_completed = frozenset(target_ordinals)
        if unresolved_ordinals:
            # Exact targets cannot be published while any sibling asset is
            # ambiguous, so they remain part of the recovery set as well.
            unresolved = tuple(
                sorted(set(unresolved_ordinals).union(target_ordinals))
            )
            return self._needs_user_result(
                transaction,
                unresolved,
                error=(
                    "; ".join(dict.fromkeys(errors))
                    or "filesystem state does not match the move journal"
                ),
                observed_completed=observed_completed,
                journal_completed=journal_completed,
            )

        if not target_ordinals:
            return RecoveryResult(
                decision=RecoveryDecision.ABORTED,
                observed_completed_ordinals=observed_completed,
                journal_completed_ordinals=journal_completed,
            )
        if not source_ordinals:
            return RecoveryResult(
                decision=RecoveryDecision.READY_TO_COMMIT,
                moved_assets=self._moved_assets(transaction),
                observed_completed_ordinals=observed_completed,
                journal_completed_ordinals=journal_completed,
            )

        rollback = self._rollback_recovery(
            transaction.items[ordinal] for ordinal in target_ordinals
        )
        if rollback.unresolved_ordinals:
            return RecoveryResult(
                decision=RecoveryDecision.NEEDS_USER,
                recovery_paths=rollback.recovery_paths,
                error=(
                    "; ".join(rollback.errors)
                    or "partial move rollback could not be completed safely"
                ),
                rolled_back_ordinals=rollback.rolled_back_ordinals,
                unresolved_ordinals=rollback.unresolved_ordinals,
                observed_completed_ordinals=observed_completed,
                journal_completed_ordinals=journal_completed,
            )
        return RecoveryResult(
            decision=RecoveryDecision.ROLLED_BACK,
            rolled_back_ordinals=rollback.rolled_back_ordinals,
            observed_completed_ordinals=observed_completed,
            journal_completed_ordinals=journal_completed,
        )

    @staticmethod
    def _validated_completed_ordinals(
        transaction: PhotoMoveTransaction,
        completed_ordinals: Iterable[int],
    ) -> frozenset[int]:
        if not isinstance(transaction, PhotoMoveTransaction):
            raise ValueError("transaction must be a PhotoMoveTransaction")
        completed = frozenset(completed_ordinals)
        expected = {item.ordinal for item in transaction.items}
        if any(type(ordinal) is not int for ordinal in completed):
            raise ValueError("completed ordinals must be integers")
        if not completed.issubset(expected):
            raise ValueError("completed ordinals must belong to the transaction")
        return completed

    @staticmethod
    def _observe_recovery_path(
        path: Path,
        item: MoveItem,
    ) -> _RecoveryObservation:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return _RecoveryObservation(_RecoveryLocation.ABSENT)
        except OSError:
            return _RecoveryObservation(
                _RecoveryLocation.UNKNOWN,
                error=f"cannot inspect move path: {path}",
            )
        if not stat.S_ISREG(metadata.st_mode):
            return _RecoveryObservation(_RecoveryLocation.OTHER)
        if metadata.st_size == item.size and metadata.st_mtime_ns == item.mtime_ns:
            return _RecoveryObservation(_RecoveryLocation.EXACT)
        return _RecoveryObservation(_RecoveryLocation.OTHER)

    def _needs_user_result(
        self,
        transaction: PhotoMoveTransaction,
        unresolved_ordinals: Iterable[int],
        *,
        error: str,
        observed_completed: frozenset[int] = frozenset(),
        journal_completed: frozenset[int] = frozenset(),
    ) -> RecoveryResult:
        unresolved = tuple(sorted(set(unresolved_ordinals)))
        by_ordinal = {item.ordinal: item for item in transaction.items}
        recovery_paths = tuple(
            dict.fromkeys(
                path
                for ordinal in unresolved
                for path in (
                    Path(by_ordinal[ordinal].source_path),
                    Path(by_ordinal[ordinal].target_path),
                )
            )
        )
        return RecoveryResult(
            decision=RecoveryDecision.NEEDS_USER,
            recovery_paths=recovery_paths,
            error=error,
            unresolved_ordinals=unresolved,
            observed_completed_ordinals=observed_completed,
            journal_completed_ordinals=journal_completed,
        )

    def _rollback_recovery(
        self,
        moved: Iterable[MoveItem],
    ) -> _RollbackOutcome:
        recovery: list[Path] = []
        errors: list[str] = []
        rolled_back: list[int] = []
        unresolved: list[int] = []
        for item in reversed(tuple(moved)):
            original = Path(item.source_path)
            current = Path(item.target_path)
            source_state = self._observe_recovery_path(
                original,
                item,
            )
            target_state = self._observe_recovery_path(
                current,
                item,
            )
            if (
                source_state.location is not _RecoveryLocation.ABSENT
                or target_state.location is not _RecoveryLocation.EXACT
            ):
                recovery.extend((original, current))
                errors.append(f"move paths changed before rollback: {current}")
                unresolved.append(item.ordinal)
                continue
            try:
                self._move_no_clobber(current, original, item)
                self._verify_fingerprint(original, item)
            except BaseException as error:
                recovery.extend((original, current))
                recovery.extend(self._error_recovery_paths(error))
                errors.append(self._error_text(error))
                unresolved.append(item.ordinal)
                continue
            rolled_back.append(item.ordinal)
        return _RollbackOutcome(
            recovery_paths=tuple(dict.fromkeys(recovery)),
            errors=tuple(errors),
            rolled_back_ordinals=tuple(rolled_back),
            unresolved_ordinals=tuple(unresolved),
        )

    @staticmethod
    def _target_for_origin(origin: Path, mark: PhotoMark) -> Path:
        if mark is PhotoMark.UNPROCESSED:
            return origin
        return origin.parent / _STATUS_DIRECTORY_BY_MARK[mark] / origin.name

    def _preflight(self, plan: PhotoMoveTransaction) -> None:
        if not isinstance(plan, PhotoMoveTransaction):
            raise ValueError("plan must be a PhotoMoveTransaction")
        self._validate_item_relationships(plan)

        for item in plan.items:
            source = Path(item.source_path)
            metadata = self._regular_file_metadata(source, "source")
            if metadata.st_size != item.size:
                raise PhotoMoveVerificationError(
                    errno.EIO,
                    f"source size changed: {source}",
                    str(source),
                )
            if metadata.st_mtime_ns != item.mtime_ns:
                raise PhotoMoveVerificationError(
                    errno.EIO,
                    f"source mtime changed: {source}",
                    str(source),
                )
        # Check every collision before checking or creating any directory.
        collisions = tuple(
            (item.ordinal, Path(item.target_path))
            for item in plan.items
            if self._lexists(Path(item.target_path))
        )
        if collisions:
            raise PhotoMoveTargetConflictError(
                recovery_paths=tuple(path for _ordinal, path in collisions),
                unresolved_ordinals=tuple(
                    ordinal for ordinal, _path in collisions
                ),
            )

        for item in plan.items:
            source = Path(item.source_path)
            target = Path(item.target_path)
            self._require_writable_directory(source.parent)
            target_ancestor = self._target_directory_capability_path(target)
            self._require_writable_directory(target_ancestor)
            if self._device_id(source) != self._device_id(target_ancestor):
                raise OSError(
                    errno.EXDEV,
                    f"source and target are on different filesystems: {target}",
                    str(target),
                )

    @classmethod
    def _validate_item_relationships(cls, plan: PhotoMoveTransaction) -> None:
        plan.validate_inverse()
        if plan.inverse_of is not None:
            cls._validate_item_relationships(plan.inverse_of)
            return
        source_keys: set[str] = set()
        target_keys: set[str] = set()
        kinds = set()
        for item in plan.items:
            source = Path(item.source_path)
            target = Path(item.target_path)
            if not source.is_absolute() or not target.is_absolute():
                raise ValueError("move paths must be absolute")
            if source.name != target.name:
                raise ValueError("photo move cannot silently rename an asset")
            origin = cls._origin_implied_by_target(target, plan.after_mark)
            expected_target = cls._target_for_origin(origin, plan.after_mark)
            source_key = cls._path_key(source)
            target_key = cls._path_key(target)
            if target_key != cls._path_key(expected_target):
                raise ValueError("target path does not match target_mark")
            allowed_source_keys = {cls._path_key(origin)}
            if plan.before_mark is not PhotoMark.UNPROCESSED:
                allowed_source_keys.add(
                    cls._path_key(
                        cls._target_for_origin(origin, plan.before_mark)
                    )
                )
            if source_key not in allowed_source_keys:
                raise ValueError(
                    "source path is unrelated to the immutable origin"
                )
            if source_key == target_key:
                raise ValueError("asset is already at the requested target")
            if source_key in source_keys:
                raise ValueError("move plan has duplicate source paths")
            if target_key in target_keys:
                raise ValueError("move plan has duplicate target paths")
            if item.kind in kinds:
                raise ValueError("move plan has duplicate asset kinds")
            source_keys.add(source_key)
            target_keys.add(target_key)
            kinds.add(item.kind)

        if source_keys.intersection(target_keys):
            raise ValueError("move plan source and target paths overlap")

    @staticmethod
    def _origin_implied_by_target(target: Path, mark: PhotoMark) -> Path:
        if mark is PhotoMark.UNPROCESSED:
            return target
        expected_directory = _STATUS_DIRECTORY_BY_MARK[mark]
        if target.parent.name != expected_directory:
            raise ValueError("target path does not match target_mark")
        return target.parent.parent / target.name

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(os.fspath(path)))

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(os.fspath(path))

    @staticmethod
    def _regular_file_metadata(path: Path, role: str) -> os.stat_result:
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise FileNotFoundError(
                errno.ENOENT,
                f"{role} file is missing: {path}",
                str(path),
            ) from error
        except OSError as error:
            raise OSError(
                error.errno,
                f"cannot inspect {role} file: {path}",
                str(path),
            ) from error
        if not stat.S_ISREG(metadata.st_mode):
            raise IsADirectoryError(
                errno.EISDIR,
                f"{role} path is not a regular file: {path}",
                str(path),
            )
        return metadata

    @staticmethod
    def _is_regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            return False

    @staticmethod
    def _require_writable_directory(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as error:
            raise FileNotFoundError(
                errno.ENOENT,
                f"target parent is missing: {path}",
                str(path),
            ) from error
        except OSError as error:
            raise OSError(
                error.errno,
                f"cannot inspect target parent: {path}",
                str(path),
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError(
                errno.ENOTDIR,
                f"target parent is not a directory: {path}",
                str(path),
            )
        if os.name != "nt":
            write_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
            execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if not metadata.st_mode & write_bits or not metadata.st_mode & execute_bits:
                raise PermissionError(
                    errno.EACCES,
                    f"target parent is not writable: {path}",
                    str(path),
                )
        if not os.access(path, os.W_OK | os.X_OK):
            raise PermissionError(
                errno.EACCES,
                f"target parent is not writable: {path}",
                str(path),
            )

    def _target_directory_capability_path(self, target: Path) -> Path:
        parent = target.parent
        if self._lexists(parent):
            return parent
        # The service creates only the one managed status directory.  It does
        # not recreate missing immutable-origin ancestry.
        return parent.parent

    @staticmethod
    def _device_id(path: Path) -> int:
        return path.stat(follow_symlinks=False).st_dev

    def _create_target_directories(
        self,
        items: Iterable[MoveItem],
    ) -> list[_CreatedDirectory]:
        created: list[_CreatedDirectory] = []
        seen: set[str] = set()
        try:
            for item in items:
                parent = Path(item.target_path).parent
                key = self._path_key(parent)
                if key in seen:
                    continue
                seen.add(key)
                if self._lexists(parent):
                    continue
                parent.mkdir(mode=0o700)
                metadata = parent.lstat()
                if not stat.S_ISDIR(metadata.st_mode):
                    raise NotADirectoryError(
                        errno.ENOTDIR,
                        f"created target parent is not a directory: {parent}",
                        str(parent),
                    )
                created.append(
                    _CreatedDirectory(parent, metadata.st_dev, metadata.st_ino)
                )
        except BaseException:
            self._remove_owned_empty_directories(created)
            raise
        return created

    @staticmethod
    def _same_identity(metadata: os.stat_result, device: int, inode: int) -> bool:
        return metadata.st_dev == device and metadata.st_ino == inode

    def _remove_owned_empty_directories(
        self,
        created: Iterable[_CreatedDirectory],
    ) -> None:
        for directory in reversed(tuple(created)):
            try:
                metadata = directory.path.lstat()
                if not self._same_identity(
                    metadata,
                    directory.device,
                    directory.inode,
                ):
                    continue
                directory.path.rmdir()
            except OSError:
                # A non-empty, replaced, or inaccessible directory is not ours
                # to remove.  Recovery paths report files, not empty folders.
                continue

    def _move_no_clobber(
        self,
        source: Path,
        target: Path,
        expected: MoveItem | None = None,
    ) -> None:
        """Atomically rename ``source`` only when ``target`` is absent."""
        try:
            _native_rename_no_replace(source, target)
        except FileExistsError as error:
            raise PhotoMoveAtomicConflictError(target) from error

    def _verify_fingerprint(self, path: Path, item: MoveItem) -> None:
        metadata = self._regular_file_metadata(path, "moved")
        if metadata.st_size != item.size:
            raise PhotoMoveVerificationError(
                errno.EIO,
                f"moved file size mismatch: {path}",
                str(path),
            )
        if metadata.st_mtime_ns != item.mtime_ns:
            raise PhotoMoveVerificationError(
                errno.EIO,
                f"moved file mtime mismatch: {path}",
                str(path),
            )

    def _matches_fingerprint(self, path: Path, item: MoveItem) -> bool:
        try:
            self._verify_fingerprint(path, item)
        except OSError:
            return False
        return True

    def _rollback(
        self,
        moved: Iterable[MoveItem],
    ) -> _RollbackOutcome:
        recovery: list[Path] = []
        errors: list[str] = []
        rolled_back: list[int] = []
        unresolved: list[int] = []
        for item in reversed(tuple(moved)):
            original = Path(item.source_path)
            current = Path(item.target_path)
            if not self._is_regular_file(current):
                self._append_existing(recovery, original, current)
                errors.append(f"moved file unavailable for rollback: {current}")
                unresolved.append(item.ordinal)
                continue
            try:
                self._move_no_clobber(current, original, item)
            except BaseException as error:
                self._append_existing(recovery, original, current)
                self._append_existing(
                    recovery,
                    *self._error_recovery_paths(error),
                )
                errors.append(self._error_text(error))
                unresolved.append(item.ordinal)
                continue
            try:
                self._verify_fingerprint(original, item)
            except BaseException as error:
                self._append_existing(recovery, original)
                errors.append(self._error_text(error))
                unresolved.append(item.ordinal)
                continue
            rolled_back.append(item.ordinal)
        return _RollbackOutcome(
            recovery_paths=tuple(dict.fromkeys(recovery)),
            errors=tuple(errors),
            rolled_back_ordinals=tuple(rolled_back),
            unresolved_ordinals=tuple(unresolved),
        )

    def _append_existing(self, paths: list[Path], *candidates: Path) -> None:
        for candidate in candidates:
            if self._lexists(candidate):
                paths.append(candidate)

    @staticmethod
    def _error_text(error: BaseException) -> str:
        message = str(error)
        return f"{type(error).__name__}: {message}" if message else type(error).__name__

    @staticmethod
    def _error_recovery_paths(error: BaseException) -> tuple[Path, ...]:
        paths = getattr(error, "recovery_paths", ())
        if not isinstance(paths, tuple):
            return ()
        return tuple(path for path in paths if isinstance(path, Path))

    @staticmethod
    def _error_unresolved_ordinals(error: BaseException) -> tuple[int, ...]:
        ordinals = getattr(error, "unresolved_ordinals", ())
        if not isinstance(ordinals, tuple):
            return ()
        return tuple(
            ordinal
            for ordinal in ordinals
            if type(ordinal) is int and ordinal >= 0
        )

    def _recovery_paths_not_confirmed_absent(
        self,
        paths: Iterable[Path],
    ) -> tuple[Path, ...]:
        retained: list[Path] = []
        for path in dict.fromkeys(paths):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                # An uninspectable path is unresolved, not absent.
                pass
            retained.append(path)
        return tuple(retained)

    @staticmethod
    def _moved_assets(
        plan: PhotoMoveTransaction,
    ) -> tuple[PhotoAssetRecord, ...]:
        assets: list[PhotoAssetRecord] = []
        for item in plan.items:
            reference = plan.inverse_of or plan
            original_item = reference.items[item.ordinal]
            origin = PhotoMoveService._origin_implied_by_target(
                Path(original_item.target_path), reference.after_mark,
            )
            assets.append(
                PhotoAssetRecord(
                    kind=item.kind,
                    fingerprint=FileFingerprint(
                        path=item.target_path,
                        size=item.size,
                        mtime_ns=item.mtime_ns,
                    ),
                    origin_path=str(origin),
                )
            )
        return tuple(assets)
