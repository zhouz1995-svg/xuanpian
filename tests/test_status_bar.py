"""Status bar: filter label, counts, and analysis progress."""

from PyQt6.QtCore import Qt

from src.xuanpian.tasks.contracts import JobFinished, JobProgress, JobState
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


def _models() -> list[ImageFileModel]:
    return [
        ImageFileModel(f"/fake/{photo_id}.jpg", photo_id=photo_id)
        for photo_id in ("a", "b", "c")
    ]


def test_status_bar_defaults(qapp) -> None:
    window = MainWindow()
    assert window._status_filter_label.text() == "筛选：全部"
    assert window._status_counts_label.text().startswith("共 0 张")
    assert window._analysis_progress.isVisible() is False
    window.close()
    qapp.processEvents()


def test_empty_project_disables_export_and_offers_import(qapp) -> None:
    window = MainWindow()
    requested: list[bool] = []
    window.thumbnail_browser.import_requested.disconnect(window._on_import)
    window.thumbnail_browser.import_requested.connect(
        lambda: requested.append(True)
    )
    window.show()
    qapp.processEvents()

    assert not window._export_btn.isEnabled()
    assert window.thumbnail_browser._empty_import_btn.isVisible()
    window.thumbnail_browser._empty_import_btn.click()
    assert requested == [True]

    window.thumbnail_browser.set_models(_models())
    assert window._export_btn.isEnabled()
    window.close()
    qapp.processEvents()


def test_thumbnail_slider_names_its_purpose(qapp) -> None:
    window = MainWindow()

    assert window._size_slider.accessibleName() == "缩略图大小"
    assert "缩略图" in window._size_slider.toolTip()

    window.close()
    qapp.processEvents()


def test_status_bar_counts_and_filter_label(qapp) -> None:
    window = MainWindow()
    models = _models()
    models[0].mark = "入选"
    models[1].mark = "待定"
    window.thumbnail_browser.set_models(models)

    assert "共 3 张" in window._status_counts_label.text()
    assert "已选 0" in window._status_counts_label.text()
    assert "保留 1" in window._status_counts_label.text()
    assert "待定 1" in window._status_counts_label.text()

    window.thumbnail_browser._set_group("review")
    window._update_status_bar()
    assert window._status_filter_label.text() == "筛选：建议复查"
    window.close()
    qapp.processEvents()


def test_status_bar_analysis_progress_shows_and_hides(qapp) -> None:
    window = MainWindow()
    window.thumbnail_browser.set_models(_models())
    window.show()
    qapp.processEvents()
    window._active_project_id = "proj"
    window._active_job_ids["analysis"] = "job"

    window._on_job_progress(
        JobProgress("proj", "job", completed=2, total=4, message="分析中…")
    )
    assert window._analysis_progress.isVisible() is True
    assert window._analysis_progress.value() == 50

    window._on_job_finished(
        JobFinished("proj", "job", JobState.SUCCEEDED, message="done")
    )
    assert window._analysis_progress.isVisible() is False
    window.close()
    qapp.processEvents()


def test_status_bar_counts_current_and_legacy_semantic_marks(qapp) -> None:
    window = MainWindow()
    models = [
        ImageFileModel("/fake/current-keep.jpg", mark="保留"),
        ImageFileModel("/fake/legacy-keep.jpg", mark="入选"),
        ImageFileModel("/fake/current-discard.jpg", mark="舍弃"),
        ImageFileModel("/fake/legacy-discard.jpg", mark="淘汰"),
    ]

    window.thumbnail_browser.set_models(models)

    assert "保留 2" in window._status_counts_label.text()
    assert "舍弃 2" in window._status_counts_label.text()
    window.close()
    qapp.processEvents()
