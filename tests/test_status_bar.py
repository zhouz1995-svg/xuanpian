"""Status bar: filter label, counts, and analysis progress."""

from PyQt6.QtCore import Qt
import pytest

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


@pytest.mark.parametrize("unsupported_only", [False, True])
def test_successful_empty_import_explains_result_and_offers_retry(
    qapp, qtbot, tmp_path, unsupported_only
) -> None:
    if unsupported_only:
        (tmp_path / "notes.txt").write_text("synthetic non-photo input")
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    try:
        window._start_import(str(tmp_path))
        qtbot.waitUntil(lambda: window._active_job_ids["import"] is None)

        browser = window.thumbnail_browser
        assert window.all_models == []
        assert "未找到可导入的照片" in browser._empty_label.text()
        assert "重新选择" in browser._empty_label.text()
        assert "未找到可导入的照片" in window.statusBar().currentMessage()
        assert not window._analyze_btn.isEnabled()
        assert not window._export_btn.isEnabled()
        assert not browser._loading_bar.isVisible()
        assert browser._empty_import_btn.isVisible()
        requested = []
        browser.import_requested.disconnect(window._on_import)
        browser.import_requested.connect(lambda: requested.append(True))
        browser._empty_import_btn.click()
        assert requested == [True]

        browser.set_models([])
        assert browser._empty_label.text() == "导入图片文件夹开始选片"
    finally:
        window.close()


@pytest.mark.parametrize("state", [JobState.FAILED, JobState.CANCELED])
def test_unsuccessful_empty_import_is_not_reported_as_no_photos(qapp, qtbot, state):
    window = MainWindow()
    qtbot.addWidget(window)
    window._active_project_id = "project"
    window._active_job_ids["import"] = "import"
    window._on_job_finished(JobFinished("project", "import", state, message="synthetic failure"))

    assert "未找到可导入的照片" not in window.thumbnail_browser._empty_label.text()
    assert "未找到可导入的照片" not in window.statusBar().currentMessage()
    assert ("失败" if state is JobState.FAILED else "取消") in window.statusBar().currentMessage()


def test_successful_import_preserves_filtered_empty_message(qapp, qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    browser = window.thumbnail_browser
    browser.set_models(_models())
    browser.toggle_picks_only()
    assert browser.current_photo_ids() == []
    window._active_project_id = "project"
    window._active_job_ids["import"] = "import"

    window._on_job_finished(JobFinished("project", "import", JobState.SUCCEEDED))

    assert len(window.all_models) == 3
    assert browser._empty_label.text() == "此筛选条件下没有照片"
    assert window._analyze_btn.isEnabled()
    assert "3" in window.statusBar().currentMessage()


@pytest.mark.parametrize("group", ["all", "review", "burst"])
def test_filtered_empty_action_restores_all_photos_without_import(qapp, qtbot, group):
    window = MainWindow()
    qtbot.addWidget(window)
    browser = window.thumbnail_browser
    models = _models()
    if group == "burst":
        for model in models:
            model.date_taken = "2026:11:07 12:00:00"
    browser.set_models(models)
    if group == "burst":
        assert models[0].burst_group_id
        browser.set_burst_group(models[0].burst_group_id)
        browser.toggle_picks_only()
    elif group == "all":
        browser.toggle_picks_only()
    else:
        browser._set_group(group)
    assert browser.current_photo_ids() == []
    before_marks = [model.mark for model in models]
    requested = []
    browser.import_requested.disconnect(window._on_import)
    browser.import_requested.connect(lambda: requested.append(True))
    assert browser._empty_import_btn.text() == "显示全部照片"
    browser._empty_import_btn.click()
    assert browser.current_photo_ids() == [model.photo_id for model in models]
    assert not browser.picks_only
    assert browser._current_burst_group is None
    assert browser._group_bar._current == "all"
    assert window._status_filter_label.text() == "筛选：全部"
    assert [model.mark for model in models] == before_marks
    assert requested == []
    browser.set_models([])
    assert browser._empty_import_btn.text() == "导入照片"
    browser._empty_import_btn.click()
    assert requested == [True]


def test_stale_empty_import_does_not_replace_current_welcome(qapp, qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window._active_project_id = "current"
    window._active_job_ids["import"] = "new-import"
    before = window.thumbnail_browser._empty_label.text()

    window._on_job_finished(JobFinished("old", "old-import", JobState.SUCCEEDED))

    assert window.thumbnail_browser._empty_label.text() == before
    assert window._active_job_ids["import"] == "new-import"


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
