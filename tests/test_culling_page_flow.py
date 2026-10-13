from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtWidgets import QMessageBox
import pytest

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling_session import AnalysisFilter, StatusFilter
from src.xuanpian.domain.technical_analysis import TechnicalAnalysisResult
from src.xuanpian.services.import_service import (
    PhotoCandidate,
    candidate_to_photo_record,
)
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


class _NoticeStore:
    def __init__(self, confirmed: bool = False) -> None:
        self.confirmed = confirmed
        self.saved_values: list[bool] = []

    def culling_notice_confirmed(self) -> bool:
        return self.confirmed

    def set_culling_notice_confirmed(self, value: bool) -> None:
        self.confirmed = value
        self.saved_values.append(value)

    def close(self) -> None:
        pass


def _model(
    photo_id: str,
    status: object = AnalysisStatus.SUCCEEDED,
    *,
    mark: str = "未处理",
    date_taken: str = "",
) -> ImageFileModel:
    model = ImageFileModel(
        f"/{photo_id}.jpg",
        photo_id=photo_id,
        mark=mark,
        date_taken=date_taken,
    )
    model.technical_status = status  # type: ignore[assignment]
    return model


def _window(store: _NoticeStore | None = None) -> MainWindow:
    window = MainWindow()
    window._project_store = store or _NoticeStore()
    return window


@pytest.mark.parametrize(
    ("statuses", "eligible"),
    [
        ([], False),
        ([AnalysisStatus.SUCCEEDED], True),
        ([AnalysisStatus.FAILED, AnalysisStatus.CANCELED], True),
        ([AnalysisStatus.PENDING, AnalysisStatus.SUCCEEDED], False),
        ([AnalysisStatus.RUNNING], False),
        (["succeeded"], False),
        ([object()], False),
    ],
)
def test_culling_button_requires_nonempty_actual_terminal_statuses(
    qapp,
    statuses: list[object],
    eligible: bool,
) -> None:
    window = _window()
    try:
        window.thumbnail_browser.set_models(
            [_model(f"p{index}", status) for index, status in enumerate(statuses)]
        )

        assert window._cull_btn.isEnabled() is eligible
    finally:
        window.close()
        qapp.processEvents()


def test_culling_button_fails_closed_without_project_or_during_import(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow()
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.question",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unavailable project must not open a modal")
        ),
    )
    try:
        window.thumbnail_browser.set_models([_model("p1")])
        assert window._cull_btn.isEnabled() is False
        window.enter_culling()
        assert window._stack.currentWidget() is window.thumbnail_browser

        window._project_store = _NoticeStore(confirmed=True)
        window._active_project_id = None
        window._refresh_culling_eligibility()
        assert window._cull_btn.isEnabled() is False

        window._active_project_id = "active-project"
        window._active_job_ids["import"] = "import-job"
        window._refresh_culling_eligibility()
        assert window._cull_btn.isEnabled() is False

        window._active_job_ids["import"] = None
        window._refresh_culling_eligibility()
        assert window._cull_btn.isEnabled() is True
    finally:
        window.close()
        qapp.processEvents()


def test_reopen_refreshes_eligibility_after_registering_import_job(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "p1.jpg"
    source.write_bytes(b"generated-test-image")
    database = tmp_path / "project.sqlite3"
    candidate = PhotoCandidate(
        "p1",
        source.name,
        jpeg_path=str(source.resolve()),
    )
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos((candidate_to_photo_record(candidate),))
        store.save_analysis(
            "p1",
            "technical",
            AnalysisStatus.SUCCEEDED,
            TechnicalAnalysisResult.review_only().to_dict(),
            None,
        )

    class _ImportCoordinator:
        def start(self, *_args):
            return SimpleNamespace(
                job_id="active-import",
                cancel=lambda: None,
                wait=lambda _timeout: True,
            )

    window = MainWindow(
        import_coordinator=_ImportCoordinator(),
        project_path_resolver=lambda _root: database,
        project_store_factory=lambda path, root: ProjectStore.open(
            path,
            root_path=root,
        ),
    )
    try:
        window._start_import(str(root))

        assert window.all_models[0].technical_status is AnalysisStatus.SUCCEEDED
        assert window._active_job_ids["import"] == "active-import"
        assert window._cull_btn.isEnabled() is False
    finally:
        window.close()
        qapp.processEvents()


def test_enter_culling_uses_all_capture_order_and_default_filters(
    qapp,
    monkeypatch,
) -> None:
    store = _NoticeStore()
    window = _window(store)
    models = [_model("p1"), _model("p2"), _model("p3")]
    window.thumbnail_browser.set_models(models)
    window.thumbnail_browser._set_group("portrait")
    assert window.thumbnail_browser.current_photo_ids() == []
    prompts: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.question",
        lambda _parent, _title, message, *_args: (
            prompts.append(message) or QMessageBox.StandardButton.Yes
        ),
    )

    try:
        window.enter_culling()

        assert window._stack.currentWidget() is window._culling_workspace
        assert window._culling_session.visible_ids() == ["p1", "p2", "p3"]
        assert window._culling_session.status_filter is StatusFilter.UNPROCESSED
        assert window._culling_session.analysis_filter is AnalysisFilter.ALL
        assert store.saved_values == [True]
        assert len(prompts) == 1
        assert "原始文件" in prompts[0]
        assert "JPEG" in prompts[0]
        assert "RAW" in prompts[0]
    finally:
        window.close()
        qapp.processEvents()


def test_notice_decline_stays_in_browse_and_does_not_persist(
    qapp,
    monkeypatch,
) -> None:
    store = _NoticeStore()
    window = _window(store)
    window.thumbnail_browser.set_models([_model("p1")])
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.question",
        lambda *_args: QMessageBox.StandardButton.No,
    )

    try:
        window.enter_culling()

        assert window._stack.currentWidget() is window.thumbnail_browser
        assert store.confirmed is False
        assert store.saved_values == []
    finally:
        window.close()
        qapp.processEvents()


def test_notice_is_only_prompted_once_for_the_same_persisted_project(
    qapp,
    monkeypatch,
) -> None:
    store = _NoticeStore()
    window = _window(store)
    window.thumbnail_browser.set_models([_model("p1")])
    prompts = 0

    def accept(*_args) -> QMessageBox.StandardButton:
        nonlocal prompts
        prompts += 1
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.question",
        accept,
    )

    try:
        window.enter_culling()
        window.exit_culling()
        window.enter_culling()

        assert prompts == 1
        assert store.saved_values == [True]
        assert window._stack.currentWidget() is window._culling_workspace
    finally:
        window.close()
        qapp.processEvents()


def test_switching_projects_uses_each_projects_persisted_notice(
    qapp,
    monkeypatch,
) -> None:
    first = _NoticeStore(confirmed=True)
    second = _NoticeStore(confirmed=False)
    window = _window(first)
    window.thumbnail_browser.set_models([_model("p1")])
    prompts = 0

    def accept(*_args) -> QMessageBox.StandardButton:
        nonlocal prompts
        prompts += 1
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.question",
        accept,
    )

    try:
        window.enter_culling()
        assert prompts == 0
        window.exit_culling()

        window._project_store = second
        window._refresh_culling_eligibility()
        window.enter_culling()

        assert prompts == 1
        assert second.saved_values == [True]
    finally:
        window.close()
        qapp.processEvents()


def test_starting_a_new_import_leaves_culling_for_browse(qapp) -> None:
    class _ImportCoordinator:
        def start(self, *_args):
            return SimpleNamespace(
                job_id="new-import",
                cancel=lambda: None,
                wait=lambda _timeout: True,
            )

    window = MainWindow(import_coordinator=_ImportCoordinator())
    window._project_store = _NoticeStore(confirmed=True)
    window.thumbnail_browser.set_models([_model("p1")])
    window.enter_culling()
    assert window._stack.currentWidget() is window._culling_workspace

    try:
        window._start_import("/generated/new-project")

        assert window._stack.currentWidget() is window.thumbnail_browser
        assert window._culling_workspace.preview_pending_count == 0
    finally:
        window.close()
        qapp.processEvents()


def test_exit_culling_preserves_browse_filter_selection_and_similar_data(
    qapp,
) -> None:
    window = _window(_NoticeStore(confirmed=True))
    models = [
        _model("p1", date_taken="2025:01:01 12:00:00"),
        _model("p2", date_taken="2025:01:01 12:00:01"),
        _model("p3", date_taken="2025:01:01 12:01:00"),
    ]
    browser = window.thumbnail_browser
    browser.set_models(models)
    browser.refresh_burst_groups()
    browser.set_burst_group("p1")
    browser.select_photo_id("p2")
    members_before = browser.burst_member_ids("p1")

    try:
        window.enter_culling()
        workspace = window._culling_workspace
        window.exit_culling()

        assert window._stack.currentWidget() is browser
        assert browser._current_group == "all"
        assert browser._current_burst_group == "p1"
        assert browser.selected_photo_ids() == ["p2"]
        assert browser.burst_member_ids("p1") == members_before == ("p1", "p2")
        assert window._culling_workspace is workspace

        window.enter_preview_photo("p1")
        assert window._stack.currentWidget() is window._preview_widget
        assert window._preview_widget._compare_btn.isHidden() is False
    finally:
        window.close()
        qapp.processEvents()
