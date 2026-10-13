"""Similar-photo (burst) grouping UI tests."""

from __future__ import annotations

from PyQt6.QtCore import QItemSelectionModel
from PyQt6.QtTest import QTest

from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.preview_widget import PreviewWidget
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser
from src.xuanpian.tasks.io_coordinator import MetadataResult


def _model(photo_id: str, date_taken: str = "") -> ImageFileModel:
    return ImageFileModel(
        f"/fake/{photo_id}.jpg",
        photo_id=photo_id,
        filename=f"{photo_id}.JPG",
        date_taken=date_taken,
    )


def _burst_models() -> list[ImageFileModel]:
    return [
        _model("a", "2026:05:02 16:40:31"),
        _model("b", "2026:05:02 16:40:32"),
        _model("c", "2026:05:02 16:41:00"),
        _model("d", "2026:05:02 16:41:01"),
        _model("e", "2026:05:02 16:50:00"),
    ]


def test_browser_derives_burst_groups_from_exif(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_burst_models())

    assert browser._burst_group_photo_ids["a"] == ("a", "b")
    assert browser._burst_group_photo_ids["c"] == ("c", "d")
    assert "e" not in browser._burst_group_photo_ids
    assert browser._model_by_photo_id["a"].burst_group_id == "a"
    assert browser._model_by_photo_id["b"].burst_group_size == 2
    assert browser._model_by_photo_id["e"].burst_group_size == 0


def test_browser_sidebar_lists_burst_groups(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_burst_models())

    sidebar = browser._sidebar
    items = [
        sidebar._burst_list.item(row).text()
        for row in range(sidebar._burst_list.count())
    ]
    assert items == ["a.JPG · 连拍 2张", "c.JPG · 连拍 2张"]


def test_burst_groups_appear_after_exif_arrives(qapp) -> None:
    """Similar groups appear once EXIF capture time is applied to models."""
    models = [_model("a"), _model("b"), _model("c"), _model("d")]
    browser = ThumbnailBrowser()
    browser.set_models(models)

    # EXIF not loaded yet: no groups, sidebar section hidden.
    assert browser._burst_group_photo_ids == {}
    assert browser._sidebar._burst_header.isHidden()

    for model, taken in (
        ("a", "2026:05:02 16:40:31"),
        ("b", "2026:05:02 16:40:32"),
        ("c", "2026:05:02 16:41:00"),
        ("d", "2026:05:02 16:41:01"),
    ):
        next(m for m in models if m.photo_id == model).date_taken = taken

    browser.refresh_burst_groups()

    assert browser._burst_group_photo_ids["a"] == ("a", "b")
    assert browser._burst_group_photo_ids["c"] == ("c", "d")
    assert not browser._sidebar._burst_header.isHidden()
    assert browser._sidebar._burst_list.count() == 2


def test_metadata_result_schedules_burst_recompute(qapp) -> None:
    class _FakeHandle:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id

        def cancel(self) -> None:
            pass

    window = MainWindow()
    browser = window.thumbnail_browser
    models = [_model("a"), _model("b"), _model("c"), _model("d")]
    browser.set_models(models)

    job_id = "meta-job-burst"
    window._io_handles[job_id] = _FakeHandle(job_id)
    taken_by_id = {
        "a": "2026:05:02 16:40:31",
        "b": "2026:05:02 16:40:32",
        "c": "2026:05:02 16:41:00",
        "d": "2026:05:02 16:41:01",
    }
    for model in models:
        event = MetadataResult(
            project_id=window._active_project_id,
            job_id=job_id,
            photo_id=model.photo_id,
            path=model.analysis_path,
            qimage=None,
            exif=ExifData(
                path=model.analysis_path,
                date_taken=taken_by_id[model.photo_id],
            ),
        )
        window._job_bridge.metadata.emit(event)

    QTest.qWait(600)

    assert browser._burst_group_photo_ids["a"] == ("a", "b")
    assert browser._sidebar._burst_list.count() == 2
    window.close()


def test_burst_group_filters_the_grid(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_burst_models())

    browser.set_burst_group("a")
    assert browser.current_photo_ids() == ["a", "b"]

    # Clicking the same group again clears the filter.
    browser.set_burst_group("a")
    assert browser.current_photo_ids() == ["a", "b", "c", "d", "e"]

    browser.set_burst_group("c")
    assert browser.current_photo_ids() == ["c", "d"]

    # Standard filters reset the burst filter.
    browser.set_burst_group("a")
    browser._set_group("all")
    assert browser.current_photo_ids() == ["a", "b", "c", "d", "e"]


def test_burst_member_ids_provider_for_preview(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_burst_models())

    assert browser.burst_member_ids("b") == ("a", "b")
    assert browser.burst_member_ids("e") is None


def test_preview_compare_strip_shows_group_members(qapp) -> None:
    models = {model.photo_id: model for model in _burst_models()}
    browser = ThumbnailBrowser()
    browser.set_models(list(models.values()))
    preview = PreviewWidget()

    preview.load_photo(
        "b",
        lambda: ["a", "b", "c", "d", "e"],
        models.get,
        burst_group_provider=browser.burst_member_ids,
    )

    assert preview._compare_btn.isHidden() is False
    assert preview._compare_btn.text() == "对比本组"
    strip_model = preview._compare_strip._list.model()
    assert strip_model.rowCount() == 2
    assert strip_model.photo_id_at(0) == "a"
    assert preview._compare_strip.isHidden() is True

    preview._toggle_compare_strip(True)
    assert preview._compare_strip.isHidden() is False


def test_preview_compare_strip_navigates_and_hides_outside_group(qapp) -> None:
    models = {model.photo_id: model for model in _burst_models()}
    browser = ThumbnailBrowser()
    browser.set_models(list(models.values()))
    preview = PreviewWidget()
    preview.load_photo(
        "b",
        lambda: ["a", "b", "c", "d", "e"],
        models.get,
        burst_group_provider=browser.burst_member_ids,
    )

    preview._compare_strip.photo_activated.emit("a")
    assert preview.current_photo_id() == "a"

    preview._show_photo_id("e")
    assert preview._compare_btn.isHidden() is True
    assert preview._compare_strip.isHidden() is True


def test_existing_similar_group_scope_and_preview_comparison_remain_available(
    qapp,
) -> None:
    window = MainWindow()
    models = [
        _model("member-a", "2026:05:02 16:40:31"),
        _model("member-b", "2026:05:02 16:40:32"),
    ]
    window.thumbnail_browser.set_models(models)
    try:
        assert window._thumbnail_browser.burst_member_ids("member-b") == (
            "member-a",
            "member-b",
        )
        window.enter_preview_photo("member-b")
        assert not window._preview_widget._compare_btn.isHidden()
    finally:
        window.close()


def test_batch_mark_button_stays_disabled_for_selected_similar_photos(qapp) -> None:
    window = MainWindow()
    browser = window.thumbnail_browser
    models = _burst_models()
    browser.set_models(models)
    assert window._batch_mark_btn.isEnabled() is False

    selection_model = browser._view.selectionModel()
    for row in (0, 2):
        selection_model.select(
            browser._photo_list_model.index(row, 0),
            QItemSelectionModel.SelectionFlag.Select,
        )
    window._update_batch_mark_state()
    assert window._batch_mark_btn.isEnabled() is False

    for action in window._batch_mark_menu.actions():
        action.trigger()
    assert models[0].mark == ""
    assert models[2].mark == ""
    assert models[1].mark == ""
    window.close()
