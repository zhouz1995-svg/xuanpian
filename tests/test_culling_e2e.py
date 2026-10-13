"""End-to-end culling acceptance with real store, bridge, worker, and files."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.services.photo_move_service import PhotoMoveService
from src.xuanpian.domain.technical_analysis import TechnicalAnalysisResult
from src.xuanpian.services.analysis_service import (
    AnalyzerOutcome,
    PhotoAnalysisResponse,
)
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.tasks.analysis_coordinator import AnalysisResult
from src.xuanpian.tasks.contracts import JobFinished, JobState
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


@dataclass
class _Project:
    root: Path
    database: Path
    store: ProjectStore
    records: dict[str, PhotoRecord]
    models: dict[str, ImageFileModel]
    hashes: dict[tuple[str, AssetKind], str]


def _wait(qapp, predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.001)
    assert predicate()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jpeg(path: Path, color: Qt.GlobalColor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path), "JPEG")


def _asset(path: Path, kind: AssetKind) -> PhotoAssetRecord:
    stat = path.stat()
    return PhotoAssetRecord(
        kind,
        FileFingerprint(str(path), stat.st_size, stat.st_mtime_ns),
        str(path),
    )


def _model(record: PhotoRecord, *, date_taken: str = "") -> ImageFileModel:
    model = ImageFileModel(
        record.analysis_asset.fingerprint.path,
        filename=record.filename,
        photo_id=record.id,
        jpeg_path=("" if record.jpeg is None else record.jpeg.fingerprint.path),
        raw_path=("" if record.raw is None else record.raw.fingerprint.path),
        mark=record.mark,
        date_taken=date_taken,
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
    )
    return model


def _make_project(tmp_path: Path) -> _Project:
    root = tmp_path / "photos"
    root.mkdir()
    pair_jpeg = root / "pair.jpg"
    pair_raw = root / "pair.cr3"
    raw_only = root / "raw-only.nef"
    jpeg_only = root / "jpeg-only.jpg"
    _write_jpeg(pair_jpeg, Qt.GlobalColor.darkCyan)
    pair_raw.write_bytes(b"synthetic paired raw payload\x00\x01")
    raw_only.write_bytes(b"synthetic raw-only payload\x02\x03")
    _write_jpeg(jpeg_only, Qt.GlobalColor.darkMagenta)

    records = {
        "pair": PhotoRecord(
            id="pair",
            filename=pair_jpeg.name,
            mark=PhotoMark.UNPROCESSED.value,
            jpeg=_asset(pair_jpeg, AssetKind.JPEG),
            raw=_asset(pair_raw, AssetKind.RAW),
        ),
        "raw-only": PhotoRecord(
            id="raw-only",
            filename=raw_only.name,
            mark=PhotoMark.UNPROCESSED.value,
            jpeg=None,
            raw=_asset(raw_only, AssetKind.RAW),
        ),
        "jpeg-only": PhotoRecord(
            id="jpeg-only",
            filename=jpeg_only.name,
            mark=PhotoMark.UNPROCESSED.value,
            jpeg=_asset(jpeg_only, AssetKind.JPEG),
            raw=None,
        ),
    }
    database = tmp_path / "project.sqlite3"
    store = ProjectStore.open(database, root_path=root)
    store.upsert_photos(tuple(records.values()))
    store.set_culling_notice_confirmed(True)
    models = {photo_id: _model(record) for photo_id, record in records.items()}
    hashes = {
        (photo_id, asset.kind): _sha256(Path(asset.fingerprint.path))
        for photo_id, record in records.items()
        for asset in record.source_assets
    }
    return _Project(root, database, store, records, models, hashes)


def _window(project: _Project, **kwargs) -> MainWindow:
    window = MainWindow(shutdown_timeout=3, **kwargs)
    window._project_store = project.store
    window.thumbnail_browser.set_models(list(project.models.values()))
    window.resize(900, 650)
    window.show()
    return window


def _finish_move(qapp, window: MainWindow) -> None:
    _wait(qapp, lambda: window._active_culling_move is None)
    assert window._project_recovery_blocked is False


def _submit_current(qapp, window: MainWindow, mark: PhotoMark) -> None:
    current_id = window._culling_session.current_id
    assert current_id is not None
    window.request_culling_mark(current_id, mark)
    _finish_move(qapp, window)


def _assert_persisted_hashes(project: _Project, store: ProjectStore) -> None:
    for photo_id, expected_record in project.records.items():
        current = store.get_photo(photo_id)
        assert current is not None
        expected_kinds = {asset.kind for asset in expected_record.source_assets}
        assert {asset.kind for asset in current.source_assets} == expected_kinds
        for asset in current.source_assets:
            path = Path(asset.fingerprint.path)
            assert path.is_file()
            assert _sha256(path) == project.hashes[(photo_id, asset.kind)]


def test_real_product_flow_moves_pair_and_raw_undoes_finishes_and_reopens(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    window = _window(project)
    try:
        window.enter_culling()
        assert window._stack.currentWidget() is window._culling_workspace

        _submit_current(qapp, window, PhotoMark.KEEP)
        kept = project.store.get_photo("pair")
        assert kept is not None
        assert {Path(asset.fingerprint.path).parent.name for asset in kept.source_assets} == {
            PhotoMark.KEEP.value
        }
        _assert_persisted_hashes(project, project.store)

        window.request_culling_undo()
        _finish_move(qapp, window)
        undone = project.store.get_photo("pair")
        assert undone is not None
        assert undone.mark == PhotoMark.UNPROCESSED.value
        assert {Path(asset.fingerprint.path).parent for asset in undone.source_assets} == {
            project.root
        }

        window.navigate_culling(-1)
        assert window._culling_session.current_id == "pair"
        _submit_current(qapp, window, PhotoMark.KEEP)
        _submit_current(qapp, window, PhotoMark.HOLD)
        _submit_current(qapp, window, PhotoMark.DISCARD)

        workspace = window._culling_workspace
        assert workspace._summary.isHidden() is False
        assert workspace._body.isHidden() is True
        assert "保留 1" in workspace._summary_label.text()
        assert "待定 1" in workspace._summary_label.text()
        assert "舍弃 1" in workspace._summary_label.text()
        assert {
            path.name for path in project.root.iterdir() if path.is_dir()
        } == {"保留", "待定", "舍弃"}
        assert project.store.list_pending_photo_moves() == ()
        _assert_persisted_hashes(project, project.store)
    finally:
        window.close()
        qapp.processEvents()

    with ProjectStore.open(project.database, root_path=project.root) as reopened:
        _assert_persisted_hashes(project, reopened)
        assert {
            record.id: record.mark for record in reopened.list_photos()
        } == {"pair": "保留", "raw-only": "待定", "jpeg-only": "舍弃"}
        reopened_models = {
            record.id: _model(record) for record in reopened.list_photos()
        }
        reopened_window = MainWindow(shutdown_timeout=3)
        reopened_window._project_store = reopened
        reopened_window.thumbnail_browser.set_models(list(reopened_models.values()))
        try:
            reopened_window.enter_culling()
            assert reopened_window._culling_workspace._summary.isHidden() is False
            assert "保留 1" in reopened_window._culling_workspace._summary_label.text()
        finally:
            reopened_window._project_store = None
            reopened_window.close()
            qapp.processEvents()


def test_target_collision_fails_closed_without_mutating_source_or_store(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    target = project.root / PhotoMark.KEEP.value / "pair.jpg"
    target.parent.mkdir()
    target.write_bytes(b"foreign target must not be replaced")
    foreign_hash = _sha256(target)
    window = _window(project)
    try:
        window.enter_culling()
        current_before = window._culling_session.current_id
        workspace_current_before = window._culling_workspace.current_photo_id
        jpeg_source = project.root / "pair.jpg"
        raw_source = project.root / "pair.cr3"
        jpeg_source_hash = _sha256(jpeg_source)
        raw_source_hash = _sha256(raw_source)
        window.request_culling_mark("pair", PhotoMark.KEEP)
        _wait(qapp, lambda: not window.culling_move_pending)

        assert window.culling_move_pending is False
        assert window._culling_workspace.move_pending is False
        assert window._culling_session.current_id == current_before == "pair"
        assert window._culling_workspace.current_photo_id == workspace_current_before
        assert project.store.list_pending_photo_moves() == ()
        assert project.store.get_photo("pair") == project.records["pair"]
        assert _sha256(jpeg_source) == jpeg_source_hash
        assert _sha256(raw_source) == raw_source_hash
        assert _sha256(target) == foreign_hash
    finally:
        window.close()
        qapp.processEvents()


class _SlowMoveService(PhotoMoveService):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.worker_thread_id: int | None = None

    def execute(self, *args, **kwargs):
        self.worker_thread_id = threading.get_ident()
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError("test did not release slow move")
        return super().execute(*args, **kwargs)


class _ControlledAnalysisCoordinator:
    def __init__(self) -> None:
        self.project_id = None
        self.job_id = uuid4()
        self.requests = ()
        self.on_event = None

    def start(self, project_id, requests, on_event):
        self.project_id = project_id
        self.requests = tuple(requests)
        self.on_event = on_event
        return SimpleNamespace(
            job_id=self.job_id,
            cancel=lambda: None,
            wait=lambda _timeout=None: True,
        )

    def finish_success(self) -> None:
        assert self.project_id is not None
        assert self.on_event is not None
        for request in self.requests:
            self.on_event(
                AnalysisResult(
                    self.project_id,
                    self.job_id,
                    PhotoAnalysisResponse(
                        request.photo_id,
                        (
                            AnalyzerOutcome(
                                "technical",
                                AnalysisStatus.SUCCEEDED,
                                TechnicalAnalysisResult.review_only(),
                            ),
                        ),
                    ),
                )
            )
        self.on_event(
            JobFinished(
                self.project_id,
                self.job_id,
                JobState.SUCCEEDED,
            )
        )


def test_pending_feedback_is_visible_next_event_turn_while_worker_is_slow(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    service = _SlowMoveService()
    window = _window(project, photo_move_service=service)
    next_turn: list[bool] = []
    try:
        window.enter_culling()
        QTimer.singleShot(
            0,
            lambda: next_turn.append(window._culling_workspace.move_pending),
        )
        window._culling_workspace._mark_buttons[0].click()
        _wait(qapp, lambda: service.started.is_set() and bool(next_turn))

        assert next_turn == [True]
        assert window._culling_workspace.move_pending is True
        assert service.worker_thread_id != threading.get_ident()
    finally:
        service.release.set()
        if window._active_culling_move is not None:
            _finish_move(qapp, window)
        window.close()
        qapp.processEvents()


def test_browse_analyze_cull_back_preserves_similar_scope_sidebar_and_compare(
    qapp,
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    project.models["pair"].date_taken = "2026:05:02 16:40:31"
    project.models["raw-only"].date_taken = "2026:05:02 16:40:32"
    project.models["jpeg-only"].date_taken = "2026:05:02 16:50:00"
    for model in project.models.values():
        model.technical_status = AnalysisStatus.PENDING
    analysis = _ControlledAnalysisCoordinator()
    window = _window(project, analysis_coordinator=analysis)
    browser = window.thumbnail_browser
    browser.refresh_burst_groups()
    browser.set_burst_group("pair")
    browser.select_photo_id("raw-only")
    members_before = browser.burst_member_ids("raw-only")
    sidebar_row_before = browser._sidebar._burst_list.currentRow()
    try:
        assert members_before == ("pair", "raw-only")
        assert browser.current_photo_ids() == ["pair", "raw-only"]

        window._on_analyze_all()
        assert all(
            model.technical_status is AnalysisStatus.RUNNING
            for model in project.models.values()
        )
        analysis.finish_success()
        _wait(qapp, lambda: window._active_job_ids["analysis"] is None)
        assert all(
            model.technical_status is AnalysisStatus.SUCCEEDED
            for model in project.models.values()
        )
        assert browser._current_burst_group == "pair"
        assert browser.current_photo_ids() == ["pair", "raw-only"]

        window.enter_preview_photo("raw-only")
        assert window._preview_widget._compare_btn.isHidden() is False
        window.exit_preview()
        window.enter_culling()
        window.exit_culling()

        assert window._stack.currentWidget() is browser
        assert browser._current_burst_group == "pair"
        assert browser.current_photo_ids() == ["pair", "raw-only"]
        assert browser.selected_photo_ids() == ["raw-only"]
        assert browser.burst_member_ids("raw-only") == members_before
        assert browser._sidebar._burst_list.currentRow() == sidebar_row_before

        window.enter_preview_photo("raw-only")
        assert window._preview_widget._compare_btn.isHidden() is False
        compare_model = window._preview_widget._compare_strip._list.model()
        assert [
            compare_model.photo_id_at(row)
            for row in range(compare_model.rowCount())
        ] == ["pair", "raw-only"]
    finally:
        window.close()
        qapp.processEvents()
