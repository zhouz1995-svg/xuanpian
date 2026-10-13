from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QUrl

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import AnalysisFilter, StatusFilter
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui import main_window
from src.xuanpian.ui.main_window import MainWindow


class _CompletedStore:
    def __init__(self, root: Path) -> None:
        self.root_path = str(root)

    def culling_notice_confirmed(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _model(photo_id: str, mark: PhotoMark) -> ImageFileModel:
    return ImageFileModel(
        f"/moved/{mark.value}/{photo_id}.jpg",
        filename=f"{photo_id}.jpg",
        photo_id=photo_id,
        mark=mark.value,
        technical_status=AnalysisStatus.SUCCEEDED,
    )


def test_completed_project_enters_summary_and_review_actions_show_results(
    qapp, tmp_path: Path
) -> None:
    window = MainWindow()
    window._project_store = _CompletedStore(tmp_path)  # type: ignore[assignment]
    models = [
        _model("keep", PhotoMark.KEEP),
        _model("hold", PhotoMark.HOLD),
        _model("discard", PhotoMark.DISCARD),
    ]
    window.thumbnail_browser.set_models(models)

    try:
        window.enter_culling()

        workspace = window._culling_workspace
        assert workspace._summary.isHidden() is False
        assert "保留 1" in workspace._summary_label.text()
        assert "待定 1" in workspace._summary_label.text()
        assert "舍弃 1" in workspace._summary_label.text()

        workspace._review_hold_btn.click()
        assert workspace._body.isHidden() is False
        assert window._culling_session.status_filter is StatusFilter.HOLD
        assert window._culling_session.analysis_filter is AnalysisFilter.ALL
        assert workspace._filmstrip._model.photo_ids() == ["hold"]

        workspace.show_summary(window._culling_session.counts())
        workspace._review_discard_btn.click()
        assert workspace._body.isHidden() is False
        assert window._culling_session.status_filter is StatusFilter.DISCARD
        assert workspace._filmstrip._model.photo_ids() == ["discard"]
    finally:
        window.close()
        qapp.processEvents()


def test_open_project_root_and_summary_action_use_store_root(
    qapp, tmp_path: Path, monkeypatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    assert main_window.open_project_root(str(tmp_path)) is True
    assert opened == [QUrl.fromLocalFile(str(tmp_path)).toLocalFile()]

    opened.clear()
    window = MainWindow()
    window._project_store = _CompletedStore(tmp_path)  # type: ignore[assignment]
    window.thumbnail_browser.set_models([_model("moved", PhotoMark.KEEP)])
    try:
        window.enter_culling()
        window._culling_workspace._open_directory_btn.click()
        assert opened == [QUrl.fromLocalFile(str(tmp_path)).toLocalFile()]
    finally:
        window.close()
        qapp.processEvents()
