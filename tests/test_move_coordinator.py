"""Background photo-move coordination tests using disposable files only."""

from __future__ import annotations

from pathlib import Path
import threading
from threading import Event, get_ident
from uuid import uuid4

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.services.photo_move_service import (
    MoveExecutionResult,
    PhotoMoveService,
)
from src.xuanpian.tasks.move_coordinator import (
    MoveCoordinator,
    MoveFinished,
    MoveItemCompleted,
    MovePreflightRejected,
    MoveStarted,
)


def _asset(kind: AssetKind, path: Path) -> PhotoAssetRecord:
    metadata = path.stat()
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=FileFingerprint(
            path=str(path.absolute()),
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
        ),
        origin_path=str(path.absolute()),
    )


@pytest.fixture
def move_plan(tmp_path: Path):
    jpeg = tmp_path / "pair.jpg"
    raw = tmp_path / "pair.cr3"
    jpeg.write_bytes(b"disposable-jpeg")
    raw.write_bytes(b"disposable-raw")
    photo = PhotoRecord(
        id="photo-1",
        filename=jpeg.name,
        mark=PhotoMark.UNPROCESSED.value,
        jpeg=_asset(AssetKind.JPEG, jpeg),
        raw=_asset(AssetKind.RAW, raw),
    )
    return PhotoMoveService().plan(photo, PhotoMark.KEEP)


def test_move_events_are_ordered_and_worker_preserves_success_evidence(
    move_plan,
) -> None:
    project_id = uuid4()
    events: list[object] = []
    callback_threads: list[int] = []
    owner_thread = get_ident()

    def collect(event: object) -> None:
        events.append(event)
        callback_threads.append(get_ident())

    handle = MoveCoordinator().start(project_id, move_plan, collect)

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [
        MoveStarted,
        MoveItemCompleted,
        MoveItemCompleted,
        MoveFinished,
    ]
    assert all(thread != owner_thread for thread in callback_threads)
    assert [event.ordinal for event in events[1:3]] == [0, 1]
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert finished.project_id == project_id
    assert finished.job_id == handle.job_id
    assert finished.success
    assert finished.completed_ordinals == (0, 1)
    assert finished.moved_assets == tuple(
        PhotoAssetRecord(
            kind=item.kind,
            fingerprint=FileFingerprint(
                path=item.target_path,
                size=item.size,
                mtime_ns=item.mtime_ns,
            ),
            origin_path=item.source_path,
        )
        for item in move_plan.items
    )
    assert finished.recovery_paths == ()
    assert finished.error is None
    assert finished.rollback_complete is None
    assert finished.rolled_back_ordinals == ()
    assert finished.unresolved_ordinals == ()


class _BlockingMoveService(PhotoMoveService):
    def __init__(self, result: MoveExecutionResult) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self._result = result

    def execute(
        self,
        plan,
        on_completed,
        *,
        should_cancel_before_first_move=None,
    ):
        self.calls += 1
        if (
            should_cancel_before_first_move is not None
            and should_cancel_before_first_move()
        ):
            return MoveExecutionResult(
                success=False,
                error="canceled before filesystem execution",
            )
        self.entered.set()
        assert self.release.wait(2.0)
        for ordinal in self._result.completed_ordinals:
            on_completed(ordinal)
        return self._result


def test_only_one_move_can_be_active_per_coordinator(move_plan) -> None:
    service = _BlockingMoveService(MoveExecutionResult(success=True))
    coordinator = MoveCoordinator(service)
    first = coordinator.start(uuid4(), move_plan, lambda _event: None)
    assert service.entered.wait(2.0)

    with pytest.raises(RuntimeError, match="^photo move already running$"):
        coordinator.start(uuid4(), move_plan, lambda _event: None)

    service.release.set()
    assert first.wait(2.0)
    second = coordinator.start(uuid4(), move_plan, lambda _event: None)
    assert second.wait(2.0)


def test_cancel_before_execution_prevents_the_filesystem_transaction(
    move_plan,
) -> None:
    service = _BlockingMoveService(MoveExecutionResult(success=True))
    coordinator = MoveCoordinator(service)
    started = Event()
    release_started_callback = Event()
    events: list[object] = []

    def collect(event: object) -> None:
        events.append(event)
        if isinstance(event, MoveStarted):
            started.set()
            assert release_started_callback.wait(2.0)

    handle = coordinator.start(uuid4(), move_plan, collect)
    assert started.wait(2.0)
    handle.cancel()
    release_started_callback.set()

    assert handle.wait(2.0)
    assert service.calls == 0
    assert [type(event) for event in events] == [MoveStarted, MovePreflightRejected]
    finished = events[-1]
    assert isinstance(finished, MovePreflightRejected)
    assert "canceled" in finished.error
    assert all(Path(item.source_path).is_file() for item in move_plan.items)
    assert not any(Path(item.target_path).exists() for item in move_plan.items)


def test_cancel_after_execution_begins_does_not_interrupt_the_transaction(
    move_plan,
) -> None:
    result = MoveExecutionResult(
        success=True,
        completed_ordinals=(0, 1),
        moved_assets=tuple(
            PhotoAssetRecord(
                kind=item.kind,
                fingerprint=FileFingerprint(
                    path=item.target_path,
                    size=item.size,
                    mtime_ns=item.mtime_ns,
                ),
                origin_path=item.source_path,
            )
            for item in move_plan.items
        ),
    )
    service = _BlockingMoveService(result)
    events: list[object] = []
    handle = MoveCoordinator(service).start(uuid4(), move_plan, events.append)
    assert service.entered.wait(2.0)

    handle.cancel()
    service.release.set()

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [
        MoveStarted,
        MoveItemCompleted,
        MoveItemCompleted,
        MoveFinished,
    ]
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert finished.success
    assert finished.completed_ordinals == (0, 1)


class _PausedAfterPreflightMoveService(PhotoMoveService):
    def __init__(self) -> None:
        super().__init__()
        self.preflight_complete = Event()
        self.release_first_move = Event()

    def _create_target_directories(self, items):
        created = super()._create_target_directories(items)
        self.preflight_complete.set()
        assert self.release_first_move.wait(2.0)
        return created


def test_cancel_after_preflight_prevents_first_atomic_rename_and_cleans_directories(
    move_plan,
) -> None:
    service = _PausedAfterPreflightMoveService()
    events: list[object] = []
    handle = MoveCoordinator(service).start(uuid4(), move_plan, events.append)
    assert service.preflight_complete.wait(2.0)

    handle.cancel()
    service.release_first_move.set()

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [MoveStarted, MoveFinished]
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert not finished.success
    assert "canceled" in finished.error
    assert all(Path(item.source_path).is_file() for item in move_plan.items)
    assert not any(Path(item.target_path).exists() for item in move_plan.items)
    assert not Path(move_plan.items[0].target_path).parent.exists()


class _ImmediateMoveService(PhotoMoveService):
    def execute(self, plan, on_completed, **kwargs):
        return MoveExecutionResult(success=True)


def test_terminal_callback_can_start_the_next_move(move_plan) -> None:
    coordinator = MoveCoordinator(_ImmediateMoveService())
    restarted = []
    second_events: list[object] = []

    def collect_first(event: object) -> None:
        if isinstance(event, MoveFinished):
            restarted.append(
                coordinator.start(uuid4(), move_plan, second_events.append)
            )

    first = coordinator.start(uuid4(), move_plan, collect_first)

    assert first.wait(2.0)
    assert len(restarted) == 1
    assert restarted[0].wait(2.0)
    assert [type(event) for event in second_events] == [
        MoveStarted,
        MoveFinished,
    ]


def test_failure_terminal_preserves_rollback_and_recovery_evidence(
    move_plan,
    tmp_path: Path,
) -> None:
    recovery_path = tmp_path / "retained-target.jpg"
    result = MoveExecutionResult(
        success=False,
        completed_ordinals=(0,),
        recovery_paths=(recovery_path,),
        error="injected failure",
        rollback_complete=False,
        rolled_back_ordinals=(0,),
        unresolved_ordinals=(1,),
    )
    service = _BlockingMoveService(result)
    service.release.set()
    events: list[object] = []

    handle = MoveCoordinator(service).start(uuid4(), move_plan, events.append)

    assert handle.wait(2.0)
    assert sum(isinstance(event, MoveFinished) for event in events) == 1
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert not finished.success
    assert finished.completed_ordinals == (0,)
    assert finished.moved_assets == ()
    assert finished.recovery_paths == (recovery_path,)
    assert finished.error == "injected failure"
    assert finished.rollback_complete is False
    assert finished.rolled_back_ordinals == (0,)
    assert finished.unresolved_ordinals == (1,)


class _ExplodingMoveService(PhotoMoveService):
    def execute(self, plan, on_completed, **kwargs):
        raise RuntimeError("injected service crash")


def test_unexpected_service_error_still_emits_one_terminal_event(move_plan) -> None:
    events: list[object] = []

    handle = MoveCoordinator(_ExplodingMoveService()).start(
        uuid4(),
        move_plan,
        events.append,
    )

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [MoveStarted, MoveFinished]
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert not finished.success
    assert finished.error == "RuntimeError: injected service crash"


class _PartialThenExplodingMoveService(PhotoMoveService):
    def execute(self, plan, on_completed, **kwargs):
        on_completed(0)
        raise RuntimeError("injected crash after callback")


def test_exception_terminal_retains_observed_completion_ordinals(move_plan) -> None:
    events: list[object] = []

    handle = MoveCoordinator(_PartialThenExplodingMoveService()).start(
        uuid4(),
        move_plan,
        events.append,
    )

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [
        MoveStarted,
        MoveItemCompleted,
        MoveFinished,
    ]
    finished = events[-1]
    assert isinstance(finished, MoveFinished)
    assert finished.completed_ordinals == (0,)


class _FatalMoveService(PhotoMoveService):
    def execute(self, plan, on_completed, **kwargs):
        raise KeyboardInterrupt("injected fatal service error")


def test_fatal_service_error_is_not_masked_and_still_finishes(
    move_plan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    raised = Event()
    fatal_errors: list[BaseException | None] = []

    def capture(args: threading.ExceptHookArgs) -> None:
        fatal_errors.append(args.exc_value)
        raised.set()

    monkeypatch.setattr(threading, "excepthook", capture)
    handle = MoveCoordinator(_FatalMoveService()).start(
        uuid4(),
        move_plan,
        events.append,
    )

    assert handle.wait(2.0)
    assert raised.wait(2.0)
    assert [type(event) for event in events] == [MoveStarted, MoveFinished]
    assert len(fatal_errors) == 1
    assert isinstance(fatal_errors[0], KeyboardInterrupt)
    assert str(fatal_errors[0]) == "injected fatal service error"


def test_fatal_started_callback_cannot_prevent_terminal_or_completion(
    move_plan,
) -> None:
    events: list[object] = []

    def collect(event: object) -> None:
        events.append(event)
        if isinstance(event, MoveStarted):
            raise KeyboardInterrupt("injected callback failure")

    handle = MoveCoordinator().start(uuid4(), move_plan, collect)

    assert handle.wait(2.0)
    assert [type(event) for event in events] == [
        MoveStarted,
        MoveItemCompleted,
        MoveItemCompleted,
        MoveFinished,
    ]
