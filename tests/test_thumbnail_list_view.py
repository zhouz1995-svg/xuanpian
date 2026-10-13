"""Grid/list browse-mode toggle and list-view row behavior."""

from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtTest import QTest

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser


def _models() -> list[ImageFileModel]:
    return [
        ImageFileModel(
            f"/fake/{photo_id}.jpg",
            photo_id=photo_id,
            filename=f"{photo_id}.JPG",
            date_taken=f"2026-07-18 14:2{i}",
        )
        for i, photo_id in enumerate(("a", "b", "c"))
    ]


def test_browser_defaults_to_grid(qapp) -> None:
    browser = ThumbnailBrowser()
    assert browser.view_mode() == "grid"
    assert browser._view.isVisibleTo(browser) is False  # empty state
    assert browser._list_view.isVisibleTo(browser) is False


def test_set_view_mode_shows_list_and_hides_grid(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models())

    browser.set_view_mode("list")

    assert browser.view_mode() == "list"
    assert browser._view.isVisibleTo(browser) is False
    assert browser._list_view.isVisibleTo(browser) is True
    assert browser._list_view.model().rowCount() == 3


def test_set_view_mode_rejects_unknown(qapp) -> None:
    browser = ThumbnailBrowser()
    try:
        browser.set_view_mode("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown mode")


def test_list_view_shared_selection_model(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models())

    assert browser._list_view.selectionModel() is browser._view.selectionModel()

    browser._view.selectionModel().select(
        browser._photo_list_model.index(1, 0),
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    browser.set_view_mode("list")
    assert browser._list_view.selectionModel().selectedIndexes()


def test_list_row_click_emits_selection_changed(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models())
    browser.set_view_mode("list")

    emitted: list[bool] = []
    browser.selection_changed.connect(lambda: emitted.append(True))

    index = browser._photo_list_model.index(0, 0)
    browser._list_view.setCurrentIndex(index)
    browser._list_view.selectionModel().select(
        index,
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    QTest.qWait(10)

    assert emitted
    assert browser.selected_photo_ids() == ["a"]


def test_capture_time_role_served(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models())

    index = browser._photo_list_model.index(0, 0)
    value = browser._photo_list_model.data(index, int(PhotoRoles.CAPTURE_TIME))
    assert value == "2026-07-18 14:20"


def test_main_window_view_toggle_drives_browser(qapp) -> None:
    window = MainWindow()
    window.thumbnail_browser.set_models(_models())

    assert window.thumbnail_browser.view_mode() == "grid"
    QTest.mouseClick(window._view_toggle._buttons["list"], Qt.MouseButton.LeftButton)
    assert window.thumbnail_browser.view_mode() == "list"
    assert window._browse_mode == "list"
