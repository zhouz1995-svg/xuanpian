"""Culling move orchestration tests using synthetic disposable files only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from PyQt6.QtCore import QThread, Qt
from PyQt6.QtGui import QImage

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import AnalysisFilter, StatusFilter
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.tasks.move_coordinator import (
    MoveFinished,
    MoveItemCompleted,
    MovePreflightRejected,
    MoveStarted,
)
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


class _MoveHandle:
    def __init__(self, job_id: object) -> None:
        self.job_id = job_id
        self.finished = False
        self.wait_calls: list[float | None] = []
        self.on_wait = None

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_calls.append(timeout)
        callback, self.on_wait = self.on_wait, None
        if callback is not None:
            callback()
        return self.finished


class _ManualMoveCoordinator:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.project_id: object | None = None
        self.plan = None
        self.callback = None
        self.handle: _MoveHandle | None = None
        self.start_count = 0
        self.on_start = lambda: None

    def start(self, project_id, plan, callback):
        self.start_count += 1
        self.on_start()
        if self.fail_start:
            raise RuntimeError("injected start failure")
        self.project_id = project_id
        self.plan = plan
        self.callback = callback
        self.handle = _MoveHandle(uuid4())
        return self.handle

    def emit(self, event: object) -> None:
        assert self.callback is not None
        self.callback(event)
        if isinstance(event, MoveFinished):
            assert self.handle is not None
            self.handle.finished = True

    def started(self) -> None:
        assert self.project_id is not None
        assert self.handle is not None
        self.emit(MoveStarted(self.project_id, self.handle.job_id))

    def item_completed(self, ordinal: int) -> None:
        assert self.project_id is not None
        assert self.handle is not None
        self.emit(
            MoveItemCompleted(self.project_id, self.handle.job_id, ordinal)
        )

    def finish(
        self,
        *,
        success: bool,
        completed_ordinals: tuple[int, ...] = (),
        moved_assets: tuple[PhotoAssetRecord, ...] = (),
        recovery_paths: tuple[Path, ...] = (),
        rollback_complete: bool | None = None,
        rolled_back_ordinals: tuple[int, ...] = (),
        unresolved_ordinals: tuple[int, ...] = (),
        error: str | None = None,
    ) -> None:
        assert self.project_id is not None
        assert self.handle is not None
        self.emit(
            MoveFinished(
                self.project_id,
                self.handle.job_id,
                success=success,
                completed_ordinals=completed_ordinals,
                moved_assets=moved_assets,
                recovery_paths=recovery_paths,
                error=(
                    None
                    if success
                    else "synthetic failure with /private/not-for-ui.jpg"
                )
                if error is None
                else error,
                rollback_complete=rollback_complete,
                rolled_back_ordinals=rolled_back_ordinals,
                unresolved_ordinals=unresolved_ordinals,
            )
        )


class _RecordingThumbnailCache:
    def __init__(self) -> None:
        self.invalidated: list[tuple[FileFingerprint, ...]] = []

    def invalidate_many(
        self,
        fingerprints: tuple[FileFingerprint, ...],
    ) -> None:
        self.invalidated.append(fingerprints)

    def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


@dataclass
class _Harness:
    window: MainWindow
    store: ProjectStore
    coordinator: _ManualMoveCoordinator
    records: dict[str, PhotoRecord]
    models: dict[str, ImageFileModel]
    database: Path
    cache: _RecordingThumbnailCache


def _write_jpeg(path: Path, color: Qt.GlobalColor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(24, 18, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path), "JPEG")


def _asset(path: Path, kind: AssetKind, origin: Path) -> PhotoAssetRecord:
    metadata = path.stat()
    return PhotoAssetRecord(
        kind,
        FileFingerprint(str(path), metadata.st_size, metadata.st_mtime_ns),
        str(origin),
    )


def _record(
    root: Path,
    photo_id: str,
    *,
    pair: bool = False,
    different_parents: bool = False,
    mark: PhotoMark = PhotoMark.UNPROCESSED,
) -> PhotoRecord:
    jpeg_parent = root / ("jpeg-card" if different_parents else "camera")
    raw_parent = root / ("raw-card" if different_parents else "camera")
    jpeg_origin = jpeg_parent / f"{photo_id}.jpg"
    raw_origin = raw_parent / f"{photo_id}.cr3"

    def current(origin: Path) -> Path:
        if mark is PhotoMark.UNPROCESSED:
            return origin
        return origin.parent / mark.value / origin.name

    jpeg_path = current(jpeg_origin)
    _write_jpeg(jpeg_path, Qt.GlobalColor.darkCyan)
    raw = None
    if pair:
        raw_path = current(raw_origin)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(f"synthetic-raw-{photo_id}".encode())
        raw = _asset(raw_path, AssetKind.RAW, raw_origin)
    return PhotoRecord(
        id=photo_id,
        filename=jpeg_origin.name,
        mark=mark.value,
        jpeg=_asset(jpeg_path, AssetKind.JPEG, jpeg_origin),
        raw=raw,
    )


def _model(record: PhotoRecord) -> ImageFileModel:
    model = ImageFileModel(
        record.analysis_asset.fingerprint.path,
        filename=record.filename,
        photo_id=record.id,
        jpeg_path=("" if record.jpeg is None else record.jpeg.fingerprint.path),
        raw_path=("" if record.raw is None else record.raw.fingerprint.path),
        mark=record.mark,
        camera_model="Synthetic Camera",
        focus_conclusion="清晰",
        burst_group_id="burst-preserved",
        burst_group_size=2,
    )
    model.technical_status = AnalysisStatus.SUCCEEDED
    return model


def _make_harness(
    qapp,
    tmp_path: Path,
    *,
    pair: bool = False,
    different_parents: bool = False,
    marks: tuple[PhotoMark, ...] = (
        PhotoMark.UNPROCESSED,
        PhotoMark.UNPROCESSED,
    ),
    coordinator: _ManualMoveCoordinator | None = None,
) -> _Harness:
    root = tmp_path / "synthetic-photos"
    database = tmp_path / "project.sqlite3"
    records = {
        f"photo-{index}": _record(
            root,
            f"photo-{index}",
            pair=pair and index == 0,
            different_parents=different_parents and index == 0,
            mark=mark,
        )
        for index, mark in enumerate(marks)
    }
    models = {photo_id: _model(record) for photo_id, record in records.items()}
    store = ProjectStore.open(database, root_path=root)
    store.upsert_photos(tuple(records.values()))
    store.set_culling_notice_confirmed(True)
    move_coordinator = coordinator or _ManualMoveCoordinator()
    cache = _RecordingThumbnailCache()
    window = MainWindow(
        move_coordinator=move_coordinator,
        thumbnail_cache=cache,
        shutdown_timeout=0.01,
    )
    window._project_store = store
    window.thumbnail_browser.set_models(list(models.values()))
    window.enter_culling()

    def submit_legacy_move(self, photo_id: str, mark: PhotoMark) -> None:
        target = self._normal_culling_target(photo_id, mark)
        if target is None:
            return
        target_store, record, model = target
        self._submit_culling_move(
            target_store,
            record,
            model,
            mark,
            is_undo=False,
            undo_command=None,
        )

    def submit_legacy_undo(self) -> None:
        command = self._undo_command
        assert command is not None
        record = store.get_photo(command.photo_id)
        model = self.model_for_photo_id(command.photo_id)
        assert record is not None
        assert model is not None
        self._submit_culling_move(
            store,
            record,
            model,
            command.before_mark,
            is_undo=True,
            undo_command=command,
        )

    window.request_culling_mark = MethodType(submit_legacy_move, window)
    window.request_culling_undo = MethodType(submit_legacy_undo, window)
    window.resize(900, 650)
    window.show()
    qapp.processEvents()
    return _Harness(
        window,
        store,
        move_coordinator,
        records,
        models,
        database,
        cache,
    )


def _process(qapp) -> None:
    qapp.processEvents()
    qapp.processEvents()


def _moved_assets(plan) -> tuple[PhotoAssetRecord, ...]:
    result = []
    for item in plan.items:
        target = Path(item.target_path)
        origin = (
            target
            if plan.after_mark is PhotoMark.UNPROCESSED
            else target.parent.parent / target.name
        )
        result.append(
            PhotoAssetRecord(
                item.kind,
                FileFingerprint(item.target_path, item.size, item.mtime_ns),
                str(origin),
            )
        )
    return tuple(result)


def _move_all_and_finish_success(harness: _Harness, qapp) -> None:
    plan = harness.coordinator.plan
    assert plan is not None
    for item in plan.items:
        source = Path(item.source_path)
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        harness.coordinator.item_completed(item.ordinal)
        _process(qapp)
    harness.coordinator.finish(
        success=True,
        completed_ordinals=tuple(item.ordinal for item in plan.items),
        moved_assets=_moved_assets(plan),
    )
    _process(qapp)


def _close(harness: _Harness, qapp) -> None:
    # Manual coordinators have no real worker to finish; retain their journal
    # as stopped recovery evidence so test cleanup obeys the normal close gate.
    if isinstance(harness.coordinator, _ManualMoveCoordinator):
        context = harness.window._active_culling_move
        if context is not None:
            context.blocked = True
            if context.handle is not None:
                context.handle.finished = True
    harness.window.close()
    _process(qapp)


def test_submission_is_bound_to_current_culling_identity_and_exact_mark(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    window = harness.window
    try:
        window.request_culling_mark("photo-1", PhotoMark.KEEP)
        window.request_culling_mark("photo-0", "保留")  # type: ignore[arg-type]
        assert harness.coordinator.start_count == 0
        assert harness.store.list_pending_photo_moves() == ()

        window.exit_culling()
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        assert harness.coordinator.start_count == 0

        window.enter_culling()
        harness.models["photo-0"].photo_id = "wrong-model"
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        assert harness.coordinator.start_count == 0
    finally:
        harness.models["photo-0"].photo_id = "photo-0"
        _close(harness, qapp)


def test_journal_is_durable_before_start_and_controls_gate_duplicates(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    observed_pending = []
    harness.coordinator.on_start = lambda: observed_pending.append(
        harness.store.list_pending_photo_moves()
    )
    current_id = harness.window._culling_session.current_id

    try:
        harness.window.request_culling_mark(current_id, PhotoMark.KEEP)

        assert len(observed_pending) == 1
        assert len(observed_pending[0]) == 1
        assert observed_pending[0][0].transaction.photo_id == current_id
        workspace = harness.window._culling_workspace
        assert workspace.move_pending is True
        assert workspace._back_btn.isEnabled() is False
        assert workspace._undo_btn.isEnabled() is False
        assert workspace._previous_btn.isEnabled() is False
        assert workspace._next_btn.isEnabled() is False
        assert all(not button.isEnabled() for button in workspace._mark_buttons)

        harness.window.request_culling_mark(current_id, PhotoMark.HOLD)
        harness.window.navigate_culling(1)
        harness.window.exit_culling()
        assert harness.coordinator.start_count == 1
        assert harness.window._culling_session.current_id == current_id
        assert harness.window._stack.currentWidget() is workspace
    finally:
        _close(harness, qapp)


def test_start_failure_aborts_journal_without_mark_or_advance(qapp, tmp_path, monkeypatch) -> None:
    from src.xuanpian.tasks import move_coordinator

    class RejectedThread(Thread):
        def start(self):
            raise RuntimeError("injected thread start failure")

    coordinator = move_coordinator.MoveCoordinator()
    harness = _make_harness(qapp, tmp_path, coordinator=coordinator)
    first = harness.window._culling_session.current_id
    try:
        with monkeypatch.context() as patch:
            patch.setattr(move_coordinator, "Thread", RejectedThread)
            harness.window.request_culling_mark(first, PhotoMark.KEEP)

        assert harness.store.list_pending_photo_moves() == ()
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._culling_workspace.move_pending is False
        assert harness.window._culling_workspace._back_btn.isEnabled() is True
    finally:
        _close(harness, qapp)


def test_per_item_completion_persists_exact_ordinal_and_duplicate_is_idempotent(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path, pair=True)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        harness.coordinator.item_completed(1)
        _process(qapp)
        assert harness.store.list_pending_photo_moves()[0].completed_ordinals == {
            1
        }

        harness.coordinator.item_completed(1)
        _process(qapp)
        assert harness.store.list_pending_photo_moves()[0].completed_ordinals == {
            1
        }
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
    finally:
        _close(harness, qapp)


def test_worker_bridge_delivers_store_progress_callback_on_owner_thread(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    receiving_threads: list[QThread] = []
    original = harness.store.mark_move_item_completed

    def record_owner_thread(transaction_id: str, ordinal: int) -> None:
        receiving_threads.append(QThread.currentThread())
        original(transaction_id, ordinal)

    harness.store.mark_move_item_completed = record_owner_thread
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        plan = harness.coordinator.plan
        item = plan.items[0]
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(item.source_path).rename(target)

        worker = Thread(
            target=harness.coordinator.item_completed,
            args=(item.ordinal,),
        )
        worker.start()
        worker.join()
        qtbot.waitUntil(lambda: bool(receiving_threads), timeout=1000)

        assert receiving_threads == [qapp.thread()]

        target.rename(Path(item.source_path))
        harness.coordinator.finish(
            success=False,
            completed_ordinals=(0,),
            rollback_complete=True,
            rolled_back_ordinals=(0,),
        )
        _process(qapp)
    finally:
        _close(harness, qapp)


def test_stale_events_are_ignored_and_wrong_ordinal_blocks_fail_closed(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        job_id = harness.coordinator.handle.job_id
        harness.coordinator.callback(
            MoveItemCompleted(uuid4(), job_id, 0)
        )
        harness.coordinator.callback(
            MoveItemCompleted(harness.window._active_project_id, uuid4(), 0)
        )
        _process(qapp)
        assert harness.store.list_pending_photo_moves()[0].completed_ordinals == set()

        harness.coordinator.item_completed(99)
        _process(qapp)
        assert harness.store.list_pending_photo_moves()[0].completed_ordinals == set()
        assert harness.window._project_recovery_blocked is True
        assert harness.window._culling_workspace.move_pending is True
    finally:
        _close(harness, qapp)


def test_success_commits_refreshes_then_advances_and_late_terminal_is_idempotent(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    window = harness.window
    first = window._culling_session.current_id
    second = "photo-1"
    ordering: list[str] = []
    original_commit = harness.store.commit_photo_move
    original_refresh = window.thumbnail_browser.refresh_photo
    original_advance = window._culling_session.advance_after_mark

    def commit(transaction_id, assets) -> None:
        ordering.append("commit")
        original_commit(transaction_id, assets)

    def refresh(photo_id, roles) -> None:
        assert harness.store.get_photo(photo_id).mark == PhotoMark.KEEP.value
        ordering.append("refresh")
        original_refresh(photo_id, roles)

    def advance(photo_id) -> None:
        assert harness.models[photo_id].mark == PhotoMark.KEEP.value
        ordering.append("advance")
        original_advance(photo_id)

    harness.store.commit_photo_move = commit
    window.thumbnail_browser.refresh_photo = refresh
    window._culling_session.advance_after_mark = advance
    try:
        window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)

        assert ordering == ["commit", "refresh", "advance"]
        assert harness.store.get_photo(first).mark == PhotoMark.KEEP.value
        assert harness.models[first].mark == PhotoMark.KEEP.value
        assert window._culling_session.current_id == second
        assert window._culling_workspace.move_pending is False
        before = tuple(ordering)

        harness.coordinator.finish(
            success=True,
            completed_ordinals=(0,),
            moved_assets=_moved_assets(harness.coordinator.plan),
        )
        _process(qapp)
        assert tuple(ordering) == before
        assert window._culling_session.current_id == second
    finally:
        _close(harness, qapp)


def test_success_requires_complete_item_and_asset_evidence(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path, pair=True)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        harness.coordinator.item_completed(0)
        _process(qapp)
        harness.coordinator.finish(
            success=True,
            completed_ordinals=(0,),
            moved_assets=_moved_assets(harness.coordinator.plan)[:1],
        )
        _process(qapp)

        assert harness.store.get_photo(first).mark == PhotoMark.UNPROCESSED.value
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


def test_non_boolean_terminal_success_is_rejected_fail_closed(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        plan = harness.coordinator.plan
        item = plan.items[0]
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(item.source_path).rename(target)
        harness.coordinator.item_completed(0)
        _process(qapp)
        harness.coordinator.emit(
            MoveFinished(
                harness.coordinator.project_id,
                harness.coordinator.handle.job_id,
                success=1,  # type: ignore[arg-type]
                completed_ordinals=(0,),
                moved_assets=_moved_assets(plan),
            )
        )
        _process(qapp)

        assert harness.store.get_photo(first).mark == PhotoMark.UNPROCESSED.value
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


def test_success_with_rollback_evidence_is_rejected_fail_closed(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        plan = harness.coordinator.plan
        item = plan.items[0]
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(item.source_path).rename(target)
        harness.coordinator.item_completed(0)
        _process(qapp)
        harness.coordinator.emit(
            MoveFinished(
                harness.coordinator.project_id,
                harness.coordinator.handle.job_id,
                success=True,
                completed_ordinals=(0,),
                moved_assets=_moved_assets(plan),
                error="contradictory success",
                rollback_complete=True,
                rolled_back_ordinals=(0,),
            )
        )
        _process(qapp)

        assert harness.store.get_photo(first).mark == PhotoMark.UNPROCESSED.value
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


def test_success_with_empty_string_error_is_rejected_fail_closed(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        plan = harness.coordinator.plan
        item = plan.items[0]
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(item.source_path).rename(target)
        harness.coordinator.item_completed(0)
        _process(qapp)
        harness.coordinator.finish(
            success=True,
            completed_ordinals=(0,),
            moved_assets=_moved_assets(plan),
            error="",
        )
        _process(qapp)

        assert harness.store.get_photo(first).mark == PhotoMark.UNPROCESSED.value
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


def test_commit_failure_retains_journal_until_verified_reverse_completes(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    original_commit = harness.store.commit_photo_move

    def fail_commit(*_args) -> None:
        raise RuntimeError("injected sqlite commit failure")

    harness.store.commit_photo_move = fail_commit
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)

        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.window._project_recovery_blocked is False
        assert harness.window._culling_workspace.move_pending is True
        assert "/private/" not in harness.window.statusBar().currentMessage()
        _move_all_and_finish_success(harness, qapp)
        assert harness.store.list_pending_photo_moves() == ()
        assert harness.store.get_photo(first) == harness.records[first]
        assert harness.window._culling_workspace.move_pending is False
        assert harness.window._culling_session.current_id == first
    finally:
        harness.store.commit_photo_move = original_commit
        _close(harness, qapp)


def test_confirmed_rollback_aborts_and_restores_controls_without_advancing(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        harness.coordinator.finish(
            success=False,
            rollback_complete=True,
        )
        _process(qapp)

        assert harness.store.list_pending_photo_moves() == ()
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._culling_workspace.move_pending is False
        assert harness.window._culling_workspace._back_btn.isEnabled() is True
        assert "/private/" not in harness.window.statusBar().currentMessage()
    finally:
        _close(harness, qapp)


def test_malformed_failure_evidence_never_aborts_journal(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        harness.coordinator.emit(
            MoveFinished(
                harness.coordinator.project_id,
                harness.coordinator.handle.job_id,
                success=False,
                recovery_paths=None,  # type: ignore[arg-type]
                rollback_complete=True,
                unresolved_ordinals=None,  # type: ignore[arg-type]
            )
        )
        _process(qapp)

        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


def test_contradictory_safe_failure_evidence_never_aborts_journal(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        harness.coordinator.item_completed(0)
        _process(qapp)
        harness.coordinator.finish(
            success=False,
            completed_ordinals=(0,),
            moved_assets=_moved_assets(harness.coordinator.plan),
            rollback_complete=True,
            rolled_back_ordinals=(),
        )
        _process(qapp)

        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize(
    ("completed", "rolled_back", "error"),
    [
        ((0, 0), (0,), "synthetic duplicate completion"),
        ((0,), (0, 0), "synthetic duplicate rollback"),
        ((), (), ""),
    ],
)
def test_duplicate_or_empty_error_safe_failure_evidence_never_aborts_journal(
    qapp,
    tmp_path,
    completed,
    rolled_back,
    error,
) -> None:
    harness = _make_harness(qapp, tmp_path, pair=True)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        for ordinal in dict.fromkeys(completed):
            harness.coordinator.item_completed(ordinal)
        _process(qapp)
        harness.coordinator.finish(
            success=False,
            completed_ordinals=completed,
            rollback_complete=True,
            rolled_back_ordinals=rolled_back,
            error=error,
        )
        _process(qapp)

        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._culling_workspace.move_pending is True
        assert harness.window._project_recovery_blocked is True
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize(
    ("completed", "rolled_back"),
    [
        ((), (0,)),
        ((0,), (1, 0)),
    ],
)
def test_safe_failure_accepts_real_rollback_ordinal_shapes(
    qapp,
    tmp_path,
    completed,
    rolled_back,
) -> None:
    harness = _make_harness(qapp, tmp_path, pair=True)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        for ordinal in completed:
            harness.coordinator.item_completed(ordinal)
        _process(qapp)
        harness.coordinator.finish(
            success=False,
            completed_ordinals=completed,
            rollback_complete=True,
            rolled_back_ordinals=rolled_back,
            error="synthetic verification failure after rename",
        )
        _process(qapp)

        assert harness.store.list_pending_photo_moves() == ()
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._culling_workspace.move_pending is False
        assert harness.window._project_recovery_blocked is False
    finally:
        _close(harness, qapp)


def test_stale_duplicate_terminal_cannot_settle_new_active_transaction(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = "photo-0"
    second = "photo-1"
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        old_project_id = harness.coordinator.project_id
        old_job_id = harness.coordinator.handle.job_id
        old_plan = harness.coordinator.plan
        old_callback = harness.coordinator.callback
        _move_all_and_finish_success(harness, qapp)

        harness.window.request_culling_mark(second, PhotoMark.HOLD)
        new_context = harness.window._active_culling_move
        old_callback(
            MoveFinished(
                old_project_id,
                old_job_id,
                success=True,
                completed_ordinals=tuple(
                    item.ordinal for item in old_plan.items
                ),
                moved_assets=_moved_assets(old_plan),
            )
        )
        old_callback(MoveItemCompleted(old_project_id, old_job_id, 0))
        _process(qapp)

        assert harness.window._active_culling_move is new_context
        assert harness.window._project_recovery_blocked is False
        pending = harness.store.list_pending_photo_moves()
        assert len(pending) == 1
        assert pending[0].transaction.photo_id == second
        assert pending[0].completed_ordinals == set()
        assert harness.models[second].mark == PhotoMark.UNPROCESSED.value

        harness.coordinator.finish(
            success=False,
            rollback_complete=True,
        )
        _process(qapp)
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize(
    ("rollback_complete", "recovery", "unresolved"),
    [
        (None, (), ()),
        (False, (), ()),
        (True, (Path("/synthetic/recovery.jpg"),), ()),
        (True, (), (0,)),
    ],
)
def test_unconfirmed_or_unresolved_failure_retains_journal_and_blocks(
    qapp,
    tmp_path,
    rollback_complete,
    recovery,
    unresolved,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    try:
        harness.window.request_culling_mark(first, PhotoMark.HOLD)
        harness.coordinator.finish(
            success=False,
            rollback_complete=rollback_complete,
            recovery_paths=recovery,
            unresolved_ordinals=unresolved,
        )
        _process(qapp)

        assert len(harness.store.list_pending_photo_moves()) == 1
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._culling_workspace.move_pending is True
        assert harness.window._project_recovery_blocked is True
        assert "/synthetic/" not in harness.window.statusBar().currentMessage()
    finally:
        _close(harness, qapp)


def test_pair_in_different_parents_refreshes_all_paths_and_preserves_model_state(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(
        qapp,
        tmp_path,
        pair=True,
        different_parents=True,
    )
    first = harness.window._culling_session.current_id
    before = harness.records[first]
    harness.models[first].burst_group_id = "burst-preserved"
    harness.models[first].burst_group_size = 2
    try:
        harness.window.request_culling_mark(first, PhotoMark.DISCARD)
        _move_all_and_finish_success(harness, qapp)

        persisted = harness.store.get_photo(first)
        model = harness.models[first]
        assert Path(model.jpeg_path).parent.name == PhotoMark.DISCARD.value
        assert Path(model.raw_path).parent.name == PhotoMark.DISCARD.value
        assert Path(model.jpeg_path).parent.parent.name == "jpeg-card"
        assert Path(model.raw_path).parent.parent.name == "raw-card"
        assert model.path == model.jpeg_path
        assert model.filename == persisted.filename
        assert model.is_raw is False
        assert model.photo_id == first
        assert model.camera_model == "Synthetic Camera"
        assert model.focus_conclusion == "清晰"
        assert model.burst_group_id == "burst-preserved"
        assert persisted.jpeg.origin_path == before.jpeg.origin_path
        assert persisted.raw.origin_path == before.raw.origin_path
    finally:
        _close(harness, qapp)


def test_success_invalidates_only_old_source_fingerprints_through_batch_helper(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path, pair=True)
    first = harness.window._culling_session.current_id
    old_fingerprints = {
        asset.fingerprint for asset in harness.records[first].source_assets
    }
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)

        assert len(harness.cache.invalidated) == 1
        assert set(harness.cache.invalidated[0]) == old_fingerprints
    finally:
        _close(harness, qapp)


def test_cross_state_move_uses_immutable_origin_and_replaces_one_level_undo(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path, marks=(PhotoMark.UNPROCESSED,))
    first = "photo-0"
    origin = Path(harness.records[first].jpeg.origin_path)
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)
        first_undo = harness.window._undo_command

        harness.window._culling_session.set_filters(
            StatusFilter.ALL,
            AnalysisFilter.ALL,
        )
        harness.window._culling_workspace.refresh_current()
        harness.window.request_culling_mark(first, PhotoMark.HOLD)
        assert Path(harness.coordinator.plan.items[0].target_path) == (
            origin.parent / PhotoMark.HOLD.value / origin.name
        )
        assert PhotoMark.KEEP.value not in str(
            harness.coordinator.plan.items[0].target_path
        )
        _move_all_and_finish_success(harness, qapp)

        assert harness.window._undo_command != first_undo
        assert harness.window._undo_command.before_mark is PhotoMark.KEEP
        assert harness.window._undo_command.after_mark is PhotoMark.HOLD
    finally:
        _close(harness, qapp)


def test_undo_uses_same_pipeline_restores_cursor_and_does_not_create_new_undo(
    qapp,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = "photo-0"
    second = "photo-1"
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)
        assert harness.window._culling_session.current_id == second
        normal_command = harness.window._undo_command

        harness.window.request_culling_undo()
        undo_plan = harness.coordinator.plan
        assert undo_plan.photo_id == first
        assert undo_plan.before_mark is PhotoMark.KEEP
        assert undo_plan.after_mark is PhotoMark.UNPROCESSED
        assert len(harness.store.list_pending_photo_moves()) == 1
        _move_all_and_finish_success(harness, qapp)

        assert harness.store.get_photo(first).mark == PhotoMark.UNPROCESSED.value
        assert harness.models[first].mark == PhotoMark.UNPROCESSED.value
        assert harness.window._culling_session.current_id == first
        assert harness.window._undo_command is None
        assert normal_command is not None
    finally:
        _close(harness, qapp)


def test_undo_collision_retains_command_and_cursor_for_retry(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = "photo-0"
    second = "photo-1"
    origin = Path(harness.records[first].jpeg.origin_path)
    try:
        harness.window.request_culling_mark(first, PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)
        command = harness.window._undo_command
        start_count = harness.coordinator.start_count
        origin.write_bytes(b"synthetic-collision")

        harness.window.request_culling_undo()
        assert harness.coordinator.start_count == start_count + 1
        harness.coordinator.emit(MovePreflightRejected(
            harness.coordinator.project_id, harness.coordinator.handle.job_id,
            "target collision before execute",
        ))
        _process(qapp)
        assert harness.window._undo_command == command
        assert harness.window._culling_session.current_id == second
        assert harness.models[first].mark == PhotoMark.KEEP.value

        origin.unlink()
        harness.window.request_culling_undo()
        assert harness.coordinator.start_count == start_count + 2
        harness.coordinator.finish(success=False, rollback_complete=True)
        _process(qapp)
        assert harness.window._undo_command == command
        assert harness.window._culling_session.current_id == second
        assert harness.models[first].mark == PhotoMark.KEEP.value

        harness.window.request_culling_undo()
        assert harness.coordinator.start_count == start_count + 3
        _move_all_and_finish_success(harness, qapp)
        assert harness.window._undo_command is None
        assert harness.window._culling_session.current_id == first
    finally:
        _close(harness, qapp)


def test_failed_new_normal_mark_keeps_previous_undo_command(qapp, tmp_path) -> None:
    harness = _make_harness(qapp, tmp_path)
    try:
        harness.window.request_culling_mark("photo-0", PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)
        command = harness.window._undo_command

        harness.window.request_culling_mark("photo-1", PhotoMark.HOLD)
        harness.coordinator.finish(success=False, rollback_complete=True)
        _process(qapp)

        assert harness.window._undo_command == command
        assert harness.models["photo-1"].mark == PhotoMark.UNPROCESSED.value
    finally:
        _close(harness, qapp)


def test_last_successful_mark_enters_summary_with_committed_counts(
    qapp, tmp_path
) -> None:
    harness = _make_harness(qapp, tmp_path)
    try:
        harness.window.request_culling_mark("photo-0", PhotoMark.KEEP)
        _move_all_and_finish_success(harness, qapp)
        harness.window.request_culling_mark("photo-1", PhotoMark.HOLD)
        _move_all_and_finish_success(harness, qapp)

        workspace = harness.window._culling_workspace
        assert workspace._summary.isHidden() is False
        assert workspace._body.isHidden()
        assert "保留 1" in workspace._summary_label.text()
        assert "待定 1" in workspace._summary_label.text()
        assert "舍弃 0" in workspace._summary_label.text()
    finally:
        _close(harness, qapp)


def test_close_during_active_move_waits_for_terminal_and_cleans_journal(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    harness.window.request_culling_mark(first, PhotoMark.KEEP)
    assert len(harness.store.list_pending_photo_moves()) == 1

    harness.window.close()
    _process(qapp)
    assert harness.coordinator.handle.wait_calls
    assert harness.window.isVisible()
    assert harness.window._project_store is harness.store

    harness.coordinator.finish(success=False, rollback_complete=True)
    _process(qapp)
    qtbot.waitUntil(lambda: harness.window._project_store is None)
    with ProjectStore.open(
        harness.database,
        root_path=tmp_path / "synthetic-photos",
    ) as reopened:
        assert reopened.list_pending_photo_moves() == ()


def test_close_processes_ready_terminal_before_shutting_store_and_preview(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    harness = _make_harness(qapp, tmp_path)
    first = harness.window._culling_session.current_id
    harness.window.request_culling_mark(first, PhotoMark.KEEP)
    plan = harness.coordinator.plan

    def complete_during_wait() -> None:
        for item in plan.items:
            target = Path(item.target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            Path(item.source_path).rename(target)
            harness.coordinator.item_completed(item.ordinal)
        harness.coordinator.finish(
            success=True,
            completed_ordinals=tuple(item.ordinal for item in plan.items),
            moved_assets=_moved_assets(plan),
        )

    harness.coordinator.handle.on_wait = complete_during_wait
    harness.window.close()
    _process(qapp)
    qtbot.waitUntil(lambda: harness.window._project_store is None)

    with ProjectStore.open(
        harness.database,
        root_path=tmp_path / "synthetic-photos",
    ) as reopened:
        assert reopened.get_photo(first).mark == PhotoMark.KEEP.value
        assert reopened.list_pending_photo_moves() == ()
