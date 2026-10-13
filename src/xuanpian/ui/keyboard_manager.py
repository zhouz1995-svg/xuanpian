from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QWidget,
)

from src.xuanpian.domain.culling import PhotoMark


class KeyboardManager:
    """Centralized keyboard shortcut manager.

    Installs shortcuts on MainWindow, scoped to thumbnail grid and preview.
    """

    def install_on(self, main_window: object, thumbnail_browser: QWidget) -> None:
        self._mw = main_window
        self._browser = thumbnail_browser

        self._shortcuts: list[QShortcut] = []

        def _add(key, slot):
            sc = QShortcut(QKeySequence(key), main_window)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
            self._shortcuts.append(sc)

        _add(Qt.Key.Key_Right, self._on_right)
        _add(Qt.Key.Key_Left, self._on_left)
        _add(Qt.Key.Key_Space, self._on_space)
        _add(Qt.Key.Key_1, self._on_mark_1)
        _add(Qt.Key.Key_2, self._on_mark_2)
        _add(Qt.Key.Key_3, self._on_mark_3)
        _add(Qt.Key.Key_G, self._on_grid)
        _add(Qt.Key.Key_L, self._on_list)
        _add(Qt.Key.Key_Return, self._on_enter)
        _add(Qt.Key.Key_Enter, self._on_enter)
        _add(Qt.Key.Key_Delete, self._on_delete)
        _add(QKeySequence.StandardKey.SelectAll, self._on_select_all)
        _add(QKeySequence.StandardKey.Undo, self._on_undo)
        _add(Qt.Key.Key_Escape, self._on_escape)

    # ---- helpers ----

    def _in_preview(self) -> bool:
        return hasattr(self._mw, "_preview_widget") and self._mw._stack.currentWidget() is self._mw._preview_widget

    def _in_culling(self) -> bool:
        return (
            hasattr(self._mw, "_culling_workspace")
            and self._mw._stack.currentWidget()
            is self._mw._culling_workspace
        )

    @staticmethod
    def _text_control_has_focus() -> bool:
        return isinstance(
            QApplication.focusWidget(),
            (
                QAbstractSpinBox,
                QComboBox,
                QLineEdit,
                QPlainTextEdit,
                QTextEdit,
            ),
        )

    def _selected_index(self) -> int:
        indexes = self._browser.selected_indexes()
        return indexes[0] if indexes else -1

    def _selected_photo_id(self) -> str | None:
        photo_ids = self._browser.selected_photo_ids()
        return photo_ids[0] if photo_ids else None

    def _select_and_scroll(self, index: int) -> None:
        self._browser.select_index(index)

    def _move_selection(self, delta: int) -> None:
        cur = self._selected_index()
        model_count = len(self._browser.current_photo_ids())
        if model_count == 0:
            return
        new_idx = max(0, min(cur + delta, model_count - 1))
        if new_idx != cur:
            self._select_and_scroll(new_idx)

    # ---- slots ----

    def _on_right(self) -> None:
        if self._text_control_has_focus():
            return
        if self._in_culling():
            if not self._mw.culling_move_pending:
                self._mw.navigate_culling(1)
            return
        if self._in_preview():
            self._mw._preview_widget.navigate(1)
        else:
            self._move_selection(1)

    def _on_left(self) -> None:
        if self._text_control_has_focus():
            return
        if self._in_culling():
            if not self._mw.culling_move_pending:
                self._mw.navigate_culling(-1)
            return
        if self._in_preview():
            self._mw._preview_widget.navigate(-1)
        else:
            self._move_selection(-1)

    def _on_space(self) -> None:
        if (
            self._text_control_has_focus()
            or self._in_preview()
            or self._in_culling()
        ):
            return
        photo_id = self._selected_photo_id()
        if photo_id is not None:
            self._mw.enter_preview_photo(photo_id)

    def _on_mark_1(self) -> None:
        self._mark_culling(PhotoMark.KEEP)

    def _on_mark_2(self) -> None:
        self._mark_culling(PhotoMark.HOLD)

    def _on_mark_3(self) -> None:
        self._mark_culling(PhotoMark.DISCARD)

    def _mark_culling(self, mark: PhotoMark) -> None:
        if (
            self._text_control_has_focus()
            or not self._in_culling()
            or self._mw.culling_move_pending
        ):
            return
        session = self._mw._culling_session
        if session is None or session.current_id is None:
            return
        self._mw.request_culling_mark(session.current_id, mark)

    def _mark_selected(self, mark: str) -> None:
        """Compatibility adapter for explicit non-shortcut business calls."""
        if self._in_preview():
            photo_id = self._mw._preview_widget.current_photo_id()
            if photo_id is not None:
                self._mw.mark_photo(photo_id, mark)
            return
        photo_ids = self._browser.selected_photo_ids()
        if not photo_ids:
            return
        mark_photos = getattr(self._mw, "mark_photos", None)
        if mark_photos is not None:
            mark_photos(tuple(photo_ids), mark)
            return
        for photo_id in photo_ids:
            self._mw.mark_photo(photo_id, mark)

    def _on_delete(self) -> None:
        if self._text_control_has_focus() or self._in_culling():
            return
        if self._in_preview():
            photo_id = self._mw._preview_widget.current_photo_id()
            if photo_id is not None:
                self._mw.delete_photo(photo_id)
        else:
            for photo_id in self._browser.selected_photo_ids():
                self._mw.delete_photo(photo_id)
                break  # one at a time

    def _on_select_all(self) -> None:
        if (
            not self._text_control_has_focus()
            and not self._in_preview()
            and not self._in_culling()
        ):
            self._mw.select_all()

    def _on_undo(self) -> None:
        if (
            self._text_control_has_focus()
            or not self._in_culling()
            or self._mw.culling_move_pending
        ):
            return
        self._mw.request_culling_undo()

    def _on_escape(self) -> None:
        if self._text_control_has_focus():
            return
        if self._in_culling():
            if not self._mw.culling_move_pending:
                self._mw.exit_culling()
            return
        if self._in_preview():
            self._mw.exit_preview()

    def _on_grid(self) -> None:
        if self._text_control_has_focus() or self._in_culling():
            return
        self._mw.set_view_mode("grid")

    def _on_list(self) -> None:
        if self._text_control_has_focus() or self._in_culling():
            return
        self._mw.set_view_mode("list")

    def _on_enter(self) -> None:
        if (
            self._text_control_has_focus()
            or self._in_preview()
            or self._in_culling()
        ):
            return
        photo_id = self._selected_photo_id()
        if photo_id is not None:
            self._mw.enter_preview_photo(photo_id)
