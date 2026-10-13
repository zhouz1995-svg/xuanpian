"""Serialize recoverable photo moves on a background thread."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from uuid import uuid4

from src.xuanpian.domain.photo import PhotoAssetRecord
from src.xuanpian.domain.photo_move import PhotoMoveTransaction
from src.xuanpian.services.photo_move_service import (
    MoveExecutionResult,
    PhotoMoveService,
)
from src.xuanpian.tasks.contracts import (
    CancellationToken,
    JobHandle,
    JobId,
    ProjectId,
)


logger = logging.getLogger(__name__)


class MoveStartRejected(RuntimeError):
    """Proof that a denied launch cannot enter preflight or filesystem work."""

    def __init__(self, message: str, handle: JobHandle) -> None:
        super().__init__(message)
        self.handle = handle


@dataclass(frozen=True, slots=True)
class MoveStarted:
    project_id: ProjectId
    job_id: JobId


@dataclass(frozen=True, slots=True)
class MoveItemCompleted:
    project_id: ProjectId
    job_id: JobId
    ordinal: int


@dataclass(frozen=True, slots=True)
class MovePreflightRejected:
    """Terminal proof that execute was never called and no rename started."""

    project_id: ProjectId
    job_id: JobId
    error: str


@dataclass(frozen=True, slots=True)
class MoveFinished:
    project_id: ProjectId
    job_id: JobId
    success: bool
    completed_ordinals: tuple[int, ...] = ()
    moved_assets: tuple[PhotoAssetRecord, ...] = ()
    recovery_paths: tuple[Path, ...] = ()
    error: str | None = None
    rollback_complete: bool | None = None
    rolled_back_ordinals: tuple[int, ...] = ()
    unresolved_ordinals: tuple[int, ...] = ()


class MoveCoordinator:
    """Run at most one complete filesystem move transaction at a time."""

    def __init__(self, service: PhotoMoveService | None = None) -> None:
        self._service = service or PhotoMoveService()
        self._active_move: JobHandle | None = None
        self._active_lock = Lock()

    def start(
        self,
        project_id: ProjectId,
        plan: PhotoMoveTransaction,
        on_event: Callable[[object], None],
    ) -> JobHandle:
        with self._active_lock:
            if (
                self._active_move is not None
                and not self._active_move._done.is_set()
            ):
                raise RuntimeError("photo move already running")
            handle = JobHandle(uuid4(), CancellationToken(), Event())
            self._active_move = handle
            launch_decided = Event()
            launch_allowed = False

            def run_after_launch() -> None:
                launch_decided.wait()
                if launch_allowed:
                    self._run_move(project_id, handle, plan, on_event)

            try:
                worker = Thread(
                    target=run_after_launch,
                    name=f"move-{handle.job_id}",
                    daemon=True,
                )
                worker.start()
            except BaseException as error:
                # A start wrapper may launch the OS thread and then raise.
                # Release that thread with a permanently denied gate before
                # publishing completion; it can never touch photos or SQLite.
                launch_decided.set()
                self._active_move = None
                handle._done.set()
                if isinstance(error, Exception):
                    raise MoveStartRejected(str(error), handle) from error
                raise
            launch_allowed = True
            launch_decided.set()
            return handle

    @staticmethod
    def _emit(on_event: Callable[[object], None], event: object) -> None:
        try:
            on_event(event)
        except BaseException:
            logger.exception("Photo move job event callback failed")

    def _deactivate(self, handle: JobHandle) -> None:
        with self._active_lock:
            if self._active_move is handle:
                self._active_move = None

    def _run_move(
        self,
        project_id: ProjectId,
        handle: JobHandle,
        plan: PhotoMoveTransaction,
        on_event: Callable[[object], None],
    ) -> None:
        result: MoveExecutionResult
        completed_ordinals: list[int] = []
        fatal_error: BaseException | None = None

        def item_completed(ordinal: int) -> None:
            completed_ordinals.append(ordinal)
            self._emit(
                on_event,
                MoveItemCompleted(project_id, handle.job_id, ordinal),
            )

        self._emit(on_event, MoveStarted(project_id, handle.job_id))
        try:
            if handle._token.is_canceled():
                raise RuntimeError("canceled before filesystem execution")
            self._service.preflight(plan)
            if handle._token.is_canceled():
                raise RuntimeError("canceled before filesystem execution")
        except BaseException as error:
            self._deactivate(handle)
            try:
                self._emit(on_event, MovePreflightRejected(
                    project_id, handle.job_id, f"{type(error).__name__}: {error}",
                ))
            finally:
                handle._done.set()
            if not isinstance(error, Exception):
                raise
            return
        try:
            if handle._token.is_canceled():
                result = MoveExecutionResult(
                    success=False,
                    error="canceled before filesystem execution",
                )
            else:
                result = self._service.execute(
                    plan,
                    item_completed,
                    should_cancel_before_first_move=(
                        handle._token.is_canceled
                    ),
                )
        except BaseException as error:
            logger.exception("Background photo move failed")
            result = MoveExecutionResult(
                success=False,
                completed_ordinals=tuple(completed_ordinals),
                recovery_paths=tuple(
                    path
                    for path in getattr(error, "recovery_paths", ())
                    if isinstance(path, Path)
                ),
                error=f"{type(error).__name__}: {error}",
                rollback_complete=getattr(error, "rollback_complete", None),
                rolled_back_ordinals=tuple(
                    getattr(error, "rolled_back_ordinals", ())
                ),
                unresolved_ordinals=tuple(
                    getattr(error, "unresolved_ordinals", ())
                ),
            )
            if not isinstance(error, Exception):
                fatal_error = error

        self._deactivate(handle)
        try:
            self._emit(
                on_event,
                MoveFinished(
                    project_id=project_id,
                    job_id=handle.job_id,
                    success=result.success,
                    completed_ordinals=result.completed_ordinals,
                    moved_assets=result.moved_assets,
                    recovery_paths=result.recovery_paths,
                    error=(
                        None
                        if result.success is True and result.error == ""
                        else result.error
                    ),
                    rollback_complete=result.rollback_complete,
                    rolled_back_ordinals=result.rolled_back_ordinals,
                    unresolved_ordinals=result.unresolved_ordinals,
                ),
            )
        finally:
            handle._done.set()

        if fatal_error is not None:
            raise fatal_error
