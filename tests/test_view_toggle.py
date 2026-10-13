"""Segmented grid/list view toggle used by the regrouped toolbar."""

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from src.xuanpian.ui.view_toggle import ViewToggle


def test_view_toggle_defaults_to_grid(qapp) -> None:
    toggle = ViewToggle()
    assert toggle.current_view() == "grid"
    assert toggle._buttons["grid"].isChecked() is True
    assert toggle._buttons["list"].isChecked() is False


def test_view_toggle_emits_on_change(qapp) -> None:
    toggle = ViewToggle()
    seen: list[str] = []
    toggle.view_changed.connect(seen.append)

    QTest.mouseClick(toggle._buttons["list"], Qt.MouseButton.LeftButton)

    assert seen == ["list"]
    assert toggle.current_view() == "list"
    assert toggle._buttons["list"].isChecked() is True


def test_view_toggle_set_view_and_idempotent_click(qapp) -> None:
    toggle = ViewToggle()
    toggle.set_view("list")
    assert toggle.current_view() == "list"

    seen: list[str] = []
    toggle.view_changed.connect(seen.append)
    QTest.mouseClick(toggle._buttons["list"], Qt.MouseButton.LeftButton)
    # 点击已选中项不重复发信号
    assert seen == []


def test_view_toggle_rejects_unknown_key(qapp) -> None:
    toggle = ViewToggle()
    try:
        toggle.set_view("nope")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown view key")
