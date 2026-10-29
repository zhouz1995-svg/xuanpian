"""Regression tests for the final safety review, using disposable assets."""

from PyQt6.QtWidgets import QPushButton
from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling_session import AnalysisFilter
from src.xuanpian.domain.culling_session import StatusFilter
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.services.photo_move_service import PhotoMoveService
from src.xuanpian.tasks.move_coordinator import MoveCoordinator
from threading import Event, Thread, get_ident
from PyQt6.QtCore import QTimer
from pathlib import Path
from time import perf_counter
import pytest
import hashlib
import sqlite3


def test_proven_sqlite_noncommit_rolls_pair_back_before_releasing_controls(qapp, qtbot, tmp_path):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    window = harness.window
    before = harness.store.get_photo("photo-0")
    hashes = {asset.kind: hashlib.sha256(Path(asset.fingerprint.path).read_bytes()).hexdigest()
              for asset in before.source_assets}
    harness.store._connection.execute("""
        CREATE TEMP TRIGGER reject_culling_commit BEFORE UPDATE OF mark ON photos
        BEGIN SELECT RAISE(ABORT, 'synthetic commit failure'); END
    """)
    try:
        window._culling_workspace._mark_buttons[0].click()
        qtbot.waitUntil(lambda: not window.culling_move_pending or window._project_recovery_blocked)
        assert not window._project_recovery_blocked
        assert harness.store.get_photo("photo-0") == before
        assert harness.store.list_pending_photo_moves() == ()
        assert window._culling_session.current_id == "photo-0"
        assert window._undo_command is None
        assert harness.models["photo-0"].mark == "未处理"
        for asset in before.source_assets:
            source = Path(asset.fingerprint.path)
            assert hashlib.sha256(source.read_bytes()).hexdigest() == hashes[asset.kind]
            assert not (source.parent / "保留" / source.name).exists()
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize("failure", ["readback", "after_commit"])
def test_ambiguous_committed_outcome_never_reverses_files(qapp, qtbot, tmp_path, failure):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    store, window = harness.store, harness.window
    original_commit, original_get = store.commit_photo_move, store.get_photo

    def commit_then_fail(*args):
        original_commit(*args)
        if failure == "after_commit":
            raise sqlite3.OperationalError("post-commit transport failure")
        store.get_photo = lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("read unavailable"))

    store.commit_photo_move = commit_then_fail
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(lambda: window._project_recovery_blocked)
        store.get_photo = original_get
        committed = store.get_photo("photo-0")
        assert committed.mark == "保留"
        assert store.list_pending_photo_moves() == ()
        assert harness.models["photo-0"].mark == "未处理"
        assert window._culling_session.current_id == "photo-0"
        assert all(Path(asset.fingerprint.path).is_file() for asset in committed.source_assets)
        assert all(not Path(asset.origin_path).exists() for asset in committed.source_assets)
    finally:
        store.get_photo, store.commit_photo_move = original_get, original_commit
        _close(harness, qapp)


@pytest.mark.parametrize("obstruct", [False, True])
def test_noncommit_with_unknown_proof_or_obstructed_rollback_retains_original_journal(
    qapp, qtbot, tmp_path, obstruct
):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    store, window = harness.store, harness.window
    before = store.get_photo("photo-0")
    original_commit = store.commit_photo_move
    original_proof = store.is_photo_move_uncommitted
    raw_origin = Path(before.raw.origin_path)

    def fail_commit(*args):
        if obstruct:
            raw_origin.write_bytes(b"foreign collision")
        raise sqlite3.OperationalError("proven injection before database mutation")

    store.commit_photo_move = fail_commit
    if not obstruct:
        store.is_photo_move_uncommitted = lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("proof unavailable"))
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(lambda: window._project_recovery_blocked)
        assert store.get_photo("photo-0") == before
        pending, = store.list_pending_photo_moves()
        assert pending.transaction.after_mark is PhotoMark.KEEP
        assert pending.transaction.inverse_of is None
        assert pending.completed_ordinals == {0, 1}
        assert window._culling_session.current_id == "photo-0"
        for item in pending.transaction.items:
            assert Path(item.target_path).is_file()
        if obstruct:
            assert raw_origin.read_bytes() == b"foreign collision"
            assert not Path(before.jpeg.origin_path).exists()
    finally:
        store.commit_photo_move = original_commit
        store.is_photo_move_uncommitted = original_proof
        _close(harness, qapp)


def test_failed_undo_commit_restores_committed_state_and_retains_undo(qapp, qtbot, tmp_path):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    store, window = harness.store, harness.window
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        before = store.get_photo("photo-0")
        command = window._undo_command
        store._connection.execute("""
            CREATE TEMP TRIGGER reject_undo_commit BEFORE UPDATE OF mark ON photos
            BEGIN SELECT RAISE(ABORT, 'synthetic undo commit failure'); END
        """)
        window.request_culling_undo()
        qtbot.waitUntil(lambda: not window.culling_move_pending or window._project_recovery_blocked)
        assert not window._project_recovery_blocked
        assert store.get_photo("photo-0") == before
        assert store.list_pending_photo_moves() == ()
        assert window._undo_command == command
        assert window._culling_session.current_id == "photo-1"
        assert all(Path(asset.fingerprint.path).is_file() for asset in before.source_assets)
        assert all(not Path(asset.origin_path).exists() for asset in before.source_assets)
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize("changed", ["raw_collision", "disk_fingerprint", "stored_fingerprint"])
def test_undo_refuses_changed_pair_without_losing_command_or_cursor(qapp, qtbot, tmp_path, changed):
    from dataclasses import replace
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    store, window = harness.store, harness.window
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        command = window._undo_command
        after = store.get_photo("photo-0")
        if changed == "raw_collision":
            Path(after.raw.origin_path).write_bytes(b"foreign raw file")
        else:
            path = Path(after.raw.fingerprint.path)
            path.write_bytes(b"changed replacement raw file")
            if changed == "stored_fingerprint":
                metadata = path.stat()
                after = replace(after, raw=replace(after.raw, fingerprint=replace(
                    after.raw.fingerprint, size=metadata.st_size, mtime_ns=metadata.st_mtime_ns,
                )))
                store.upsert_photos((after,))
        current_contents = {asset.kind: Path(asset.fingerprint.path).read_bytes() for asset in after.source_assets}
        window.request_culling_undo()
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        assert store.get_photo("photo-0") == after
        assert store.list_pending_photo_moves() == ()
        assert window._undo_command == command
        assert window._culling_session.current_id == "photo-1"
        assert all(Path(asset.fingerprint.path).read_bytes() == current_contents[asset.kind] for asset in after.source_assets)
        if changed == "raw_collision":
            assert Path(after.raw.origin_path).read_bytes() == b"foreign raw file"
    finally:
        _close(harness, qapp)


def test_cross_state_undo_restores_previous_managed_directory(qapp, qtbot, tmp_path):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    store, window = harness.store, harness.window
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        before = store.get_photo("photo-0")
        window._culling_workspace.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)
        window._culling_session.select_photo_id("photo-0")
        window._culling_workspace.refresh_current()
        window.request_culling_mark("photo-0", PhotoMark.HOLD)
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        window.request_culling_undo()
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        assert store.get_photo("photo-0") == before
        assert all(Path(asset.fingerprint.path).parent.name == "保留" for asset in before.source_assets)
        assert window._culling_session.current_id == "photo-0"
    finally:
        _close(harness, qapp)


@pytest.mark.parametrize("legacy", [False, True])
def test_exact_undo_restores_prior_pair_state_and_current_selection(qapp, qtbot, tmp_path, legacy):
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator())
    window = harness.window
    try:
        if legacy:
            harness.store.save_marks((("photo-0", "保留"),))
            harness.models["photo-0"].mark = "保留"
        before = harness.store.get_photo("photo-0")
        window._culling_workspace.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)
        window._culling_workspace._mark_buttons[1].click()
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        assert harness.store.get_photo("photo-0").mark == "待定"
        assert window._culling_session.current_id == "photo-1"
        window._culling_workspace._undo_btn.click()
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        assert harness.store.get_photo("photo-0") == before
        assert window._culling_session.current_id == "photo-0"
        assert window._culling_workspace.current_photo_id == "photo-0"
        assert window._undo_command is None
        assert all(Path(asset.fingerprint.path).is_file() for asset in before.source_assets)
    finally:
        _close(harness, qapp)

from tests.test_culling_move_integration import _make_harness, _close, _process


@pytest.mark.parametrize("launch_before_error", [False, True])
def test_rejected_start_and_failed_abort_retains_pair_journal_but_allows_close(
    qapp, tmp_path, monkeypatch, launch_before_error
):
    from src.xuanpian.store.project_store import ProjectStore
    from src.xuanpian.tasks import move_coordinator

    calls, workers = [], []

    class RecordingService(PhotoMoveService):
        def preflight(self, plan):
            calls.append("preflight")
            return super().preflight(plan)

        def execute(self, *args, **kwargs):
            calls.append("execute")
            return super().execute(*args, **kwargs)

    class RejectedThread(Thread):
        def start(self):
            workers.append(self)
            if launch_before_error:
                super().start()
            raise RuntimeError("injected thread launch failure")

    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator(RecordingService()))
    window, store = harness.window, harness.store
    before = store.get_photo("photo-0")
    contents = {asset.kind: Path(asset.fingerprint.path).read_bytes() for asset in before.source_assets}
    abort_attempts = []

    def failed_abort(transaction_id):
        abort_attempts.append(transaction_id)
        raise sqlite3.OperationalError("injected journal abort failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(move_coordinator, "Thread", RejectedThread)
            patch.setattr(store, "abort_photo_move", failed_abort)
            window.request_culling_mark("photo-0", PhotoMark.KEEP)
        assert len(workers) == 1
        if launch_before_error:
            workers[0].join(1)
            assert not workers[0].is_alive(), "rejected launch must release its denied gate"
        assert calls == []
        pending, = store.list_pending_photo_moves()
        assert abort_attempts == [pending.transaction.id]
        assert pending.completed_ordinals == set()
        assert store.get_photo("photo-0") == before
        assert window._active_culling_move.blocked
        assert window._culling_move_can_close()
        assert window._active_culling_move.handle.wait(0)
        assert window._culling_session.current_id == "photo-0"
        for item in pending.transaction.items:
            assert Path(item.source_path).read_bytes() == contents[item.kind]
            assert not Path(item.target_path).exists()
        window.close()
        assert not window.isVisible()
        assert window._project_store is None
        with ProjectStore.open(harness.database, root_path=tmp_path / "synthetic-photos") as reopened:
            assert reopened.list_pending_photo_moves() == (pending,)
            assert reopened.get_photo("photo-0") == before
    finally:
        # Only these captured test workers can exist; join before bypassing a
        # failed assertion's close gate so teardown never leaks a live worker.
        for worker in workers:
            if worker.ident is not None:
                worker.join(2)
                assert not worker.is_alive()
        window._active_culling_move = None
        _close(harness, qapp)


@pytest.mark.parametrize("unknown", ["exception", "handle"])
def test_unknown_start_outcome_retains_journal_and_refuses_close(qapp, tmp_path, unknown):
    class UnknownCoordinator:
        def start(self, *_args):
            if unknown == "exception":
                raise RuntimeError("no proof of worker state")
            return object()

    harness = _make_harness(qapp, tmp_path, coordinator=UnknownCoordinator())
    window = harness.window
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        assert len(harness.store.list_pending_photo_moves()) == 1
        assert window._active_culling_move.blocked
        assert not window._culling_move_can_close()
        window.close()
        assert window.isVisible()
        assert window._project_store is harness.store
    finally:
        # This fake has no worker; unknown production outcomes cannot do this.
        window._active_culling_move = None
        _close(harness, qapp)


def test_batch_mouse_actions_publish_metadata_only_marks_without_moves(
    qapp, tmp_path
):
    harness = _make_harness(qapp, tmp_path)
    window = harness.window
    before = harness.store.get_photo("photo-0")
    try:
        assert not window._batch_mark_btn.isEnabled()
        for action in window._batch_mark_menu.actions():
            action.trigger()
        assert harness.store.get_photo("photo-0") == before
        window.exit_culling()
        window.thumbnail_browser.select_photo_id("photo-0")
        assert window._batch_mark_btn.isEnabled()
        for action in window._batch_mark_menu.actions():
            action.trigger()
        after = harness.store.get_photo("photo-0")
        assert after is not None
        assert after.mark == "舍弃"
        assert after.source_assets == before.source_assets
        assert harness.models["photo-0"].mark == "舍弃"
        assert all(
            Path(asset.fingerprint.path).is_file()
            for asset in after.source_assets
        )
        assert harness.store.list_pending_photo_moves() == ()
    finally:
        _close(harness, qapp)


def test_legacy_marks_display_current_product_labels_in_browse_and_preview(qapp, tmp_path):
    from PyQt6.QtCore import QRect
    from PyQt6.QtGui import QPainter, QImage
    from PyQt6.QtWidgets import QStyleOptionViewItem
    from src.xuanpian.ui.thumbnail_list_delegate import ThumbnailListDelegate
    harness = _make_harness(qapp, tmp_path)
    window = harness.window
    try:
        harness.store.save_marks((("photo-0", "入选"), ("photo-1", "淘汰")))
        harness.models["photo-0"].mark = "入选"
        harness.models["photo-1"].mark = "淘汰"
        window.exit_culling()
        window._update_status_bar()
        assert "保留 1" in window._status_counts_label.text()
        assert "舍弃 1" in window._status_counts_label.text()
        window.enter_preview_photo("photo-0")
        assert window._preview_widget._mark_label.text() == "标记: 保留"
        texts = []

        class RecordingPainter(QPainter):
            def drawText(self, *args):
                texts.append(args[-1])
                return super().drawText(*args)

        image = QImage(800, 100, QImage.Format.Format_ARGB32)
        painter = RecordingPainter(image)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 800, 100)
        try:
            ThumbnailListDelegate().paint(painter, option, window.thumbnail_browser._photo_list_model.index(0, 0))
        finally:
            painter.end()
        assert "保留" in texts
        assert "入选" not in texts
    finally:
        _close(harness, qapp)


def test_normal_close_defers_without_blocking_until_paired_move_commits(qapp, qtbot, tmp_path):
    entered, release = Event(), Event()

    class SlowPairService(PhotoMoveService):
        def _move_no_clobber(self, source, target, item):
            super()._move_no_clobber(source, target, item)
            if item.ordinal == 0:
                entered.set()
                assert release.wait(5)

    service = SlowPairService()
    harness = _make_harness(qapp, tmp_path, pair=True, coordinator=MoveCoordinator(service))
    window = harness.window
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        qtbot.waitUntil(entered.is_set)
        started = perf_counter()
        window.close()
        assert perf_counter() - started < 0.1
        assert window.isVisible()
        assert window._project_store is harness.store
        assert window._move_event_processing_enabled
        assert len(harness.store.list_pending_photo_moves()) == 1
        release.set()
        qtbot.waitUntil(lambda: window._project_store is None)
        assert not window.isVisible()
        for asset in harness.records["photo-0"].source_assets:
            origin = Path(asset.origin_path)
            assert not origin.exists()
            assert (origin.parent / "保留" / origin.name).is_file()
    finally:
        release.set()
        _close(harness, qapp)


@pytest.mark.parametrize("cancel", [False, True])
def test_slow_preflight_is_off_thread_with_immediate_pending_and_safe_close(
    qapp, qtbot, tmp_path, cancel
):
    entered, release, heartbeat = Event(), Event(), Event()
    owner_thread = get_ident()
    preflight_threads = []

    class SlowPreflight(PhotoMoveService):
        def _preflight(self, plan):
            preflight_threads.append(get_ident())
            assert get_ident() != owner_thread
            entered.set()
            # Deadlock watchdog only, not a request-latency performance target.
            assert release.wait(10)
            super()._preflight(plan)

    service = SlowPreflight()
    harness = _make_harness(qapp, tmp_path, coordinator=MoveCoordinator(service))
    window = harness.window
    window._photo_move_service = service
    try:
        window.request_culling_mark("photo-0", PhotoMark.KEEP)
        assert window.culling_move_pending
        QTimer.singleShot(0, heartbeat.set)
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(heartbeat.is_set)
        assert preflight_threads and preflight_threads[0] != owner_thread
        assert not release.is_set()
        # Owner-thread reads and Qt processing remain live while storage stalls.
        assert harness.store.get_photo("photo-0").mark == "未处理"
        assert len(harness.store.list_pending_photo_moves()) == 1
        window._start_import(str(tmp_path / "other-project"))
        assert window._project_store is harness.store
        if cancel:
            window.close()
            assert window.isVisible()
        release.set()
        qtbot.waitUntil(lambda: not window.culling_move_pending)
        if cancel:
            qtbot.waitUntil(lambda: window._project_store is None)
            assert Path(harness.records["photo-0"].jpeg.origin_path).is_file()
        else:
            assert harness.store.get_photo("photo-0").mark == "保留"
            assert harness.store.list_pending_photo_moves() == ()
    finally:
        release.set()
        _close(harness, qapp)


def test_unfinished_filter_tracks_failed_jobs_and_summary_clears_current(qapp, tmp_path):
    harness = _make_harness(qapp, tmp_path)
    workspace = harness.window._culling_workspace
    try:
        assert all(
            action.text() != "分析未完成"
            for action in workspace._analysis_filter.menu().actions()
        )
        harness.models["photo-0"].technical_status = AnalysisStatus.FAILED
        workspace.refresh_current()
        next(
            action
            for action in workspace._analysis_filter.menu().actions()
            if action.text() == "分析未完成"
        ).trigger()
        assert workspace._filmstrip._model.photo_ids() == ["photo-0"]
        assert workspace._information_values["focus"].text() == "分析未完成"
        assert workspace._information_values["portrait"].text() == "不判断"
        workspace.show_summary({"保留": 2})
        assert workspace._filename_label.text() == "筛选完成"
        assert workspace._filename_label.toolTip() == ""
        assert workspace._progress_label.text() == ""
    finally:
        _close(harness, qapp)
