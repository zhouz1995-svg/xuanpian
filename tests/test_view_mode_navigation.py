"""Browse (grid/list) vs preview state machine and keyboard routing."""

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.keyboard_manager import KeyboardManager
from src.xuanpian.ui.main_window import MainWindow


def _window(qapp) -> tuple[MainWindow, list[ImageFileModel]]:
    window = MainWindow(shutdown_timeout=0.2)
    models = [
        ImageFileModel(
            f"/fake/{photo_id}.jpg",
            photo_id=photo_id,
            filename=f"{photo_id}.JPG",
        )
        for photo_id in ("a", "b", "c")
    ]
    window.thumbnail_browser.set_models(models)
    window.install_keyboard_manager(KeyboardManager())
    window.resize(900, 650)
    window.show()
    qapp.processEvents()
    return window, models


def _press(qapp, target, key) -> None:
    target.setFocus()
    QTest.keyClick(target, key)
    qapp.processEvents()


def test_set_view_mode_syncs_toggle_browser_and_exits_preview(qapp) -> None:
    window, _ = _window(qapp)
    browser = window.thumbnail_browser

    window.enter_preview_photo("a")
    assert window._in_preview() is True

    window.set_view_mode("list")

    assert window.view_mode() == "list"
    assert window._view_toggle.current_view() == "list"
    assert browser.view_mode() == "list"
    assert window._in_preview() is False
    assert window._stack.currentWidget() is browser
    window.close()
    qapp.processEvents()


def test_keyboard_g_and_l_switch_browse_modes(qapp) -> None:
    window, _ = _window(qapp)
    browser = window.thumbnail_browser
    browser.select_photo_id("a")

    _press(qapp, browser._view, Qt.Key.Key_L)
    assert window.view_mode() == "list"
    assert browser.view_mode() == "list"

    _press(qapp, browser._list_view, Qt.Key.Key_G)
    assert window.view_mode() == "grid"
    assert browser.view_mode() == "grid"
    window.close()
    qapp.processEvents()


def test_enter_opens_preview_and_escape_returns_to_last_browse_mode(qapp) -> None:
    window, _ = _window(qapp)
    browser = window.thumbnail_browser

    window.set_view_mode("list")
    browser.select_photo_id("b")

    _press(qapp, browser._list_view, Qt.Key.Key_Return)

    preview = window._preview_widget
    assert preview is not None
    assert preview.current_photo_id() == "b"

    _press(qapp, preview, Qt.Key.Key_Escape)

    assert window._in_preview() is False
    assert window.last_browse_mode() == "list"
    assert browser.view_mode() == "list"
    window.close()
    qapp.processEvents()


def test_toolbar_toggle_while_in_preview_returns_to_browse(qapp) -> None:
    window, _ = _window(qapp)
    browser = window.thumbnail_browser

    window.enter_preview_photo("a")
    assert window._in_preview() is True

    QTest.mouseClick(window._view_toggle._buttons["list"], Qt.MouseButton.LeftButton)

    assert window._in_preview() is False
    assert browser.view_mode() == "list"
    window.close()
    qapp.processEvents()
