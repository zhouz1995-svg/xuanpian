from __future__ import annotations

import sys

from PyQt6.QtCore import Qt, qInstallMessageHandler
from PyQt6.QtGui import QImage, QKeySequence, QShortcut
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLineEdit

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.keyboard_manager import KeyboardManager
from src.xuanpian.ui.main_window import MainWindow


def _window(qapp, tmp_path):
    window = MainWindow(shutdown_timeout=0.2)
    window._project_store = ProjectStore.open(
        tmp_path / "project.sqlite3",
        root_path=tmp_path,
    )
    models = []
    records = []
    for index in range(4):
        path = tmp_path / f"photo-{index}.jpg"
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkCyan)
        assert image.save(str(path), "JPEG")
        model = ImageFileModel(str(path), photo_id=f"photo-{index}")
        model.technical_status = AnalysisStatus.SUCCEEDED
        models.append(model)
        stat = path.stat()
        records.append(
            PhotoRecord(
                id=model.photo_id,
                filename=model.filename,
                mark="",
                jpeg=PhotoAssetRecord(
                    AssetKind.JPEG,
                        FileFingerprint(
                            str(path),
                            stat.st_size,
                            stat.st_mtime_ns,
                        ),
                        str(path),
                    ),
                raw=None,
            )
        )
    window._project_store.upsert_photos(
        records
    )
    window._project_store.set_culling_notice_confirmed(True)
    window.thumbnail_browser.set_models(models)
    window.install_keyboard_manager(KeyboardManager())
    window.resize(900, 650)
    window.show()
    qapp.processEvents()
    return window, models


def _press(
    qapp,
    target,
    key,
    modifier: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    target.setFocus()
    QTest.keyClick(target, key, modifier)
    qapp.processEvents()


def test_production_keyboard_has_one_unambiguous_shortcut_per_key_and_routes_navigation(
    qapp,
    tmp_path,
) -> None:
    window, _models = _window(qapp, tmp_path)
    browser = window.thumbnail_browser
    browser.select_photo_id("photo-1")
    messages: list[str] = []
    previous = qInstallMessageHandler(
        lambda _kind, _context, message: messages.append(message)
    )
    try:
        _press(qapp, browser._view, Qt.Key.Key_Right)
        assert browser.selected_photo_ids() == ["photo-2"]
        _press(qapp, browser._view, Qt.Key.Key_Left)
        assert browser.selected_photo_ids() == ["photo-1"]

        _press(qapp, browser._view, Qt.Key.Key_Space)
        preview = window._preview_widget
        assert preview.current_photo_id() == "photo-1"
        _press(qapp, preview, Qt.Key.Key_Right)
        assert preview.current_photo_id() == "photo-2"
        _press(qapp, preview, Qt.Key.Key_Left)
        assert preview.current_photo_id() == "photo-1"
        _press(qapp, preview, Qt.Key.Key_Escape)
        assert window._stack.currentWidget() is browser
    finally:
        qInstallMessageHandler(previous)
        window.close()
        qapp.processEvents()

    key_sequences = [
        shortcut.key().toString()
        for shortcut in window.findChildren(QShortcut)
    ]
    assert key_sequences.count("Right") == 1
    assert key_sequences.count("Left") == 1
    assert key_sequences.count("Esc") == 1
    assert not any("ambiguous" in message.lower() for message in messages)


def test_number_keys_mark_preview_and_culling_identity_and_native_undo_reverses(
    qapp,
    qtbot,
    tmp_path,
) -> None:
    window, models = _window(qapp, tmp_path)
    browser = window.thumbnail_browser
    browser.select_photo_id("photo-0")
    window.enter_preview_photo("photo-3")
    preview = window._preview_widget
    qtbot.waitUntil(
        lambda: preview.loaded_photo_id == "photo-3",
        timeout=2000,
    )
    assert preview.preview_state == "loaded"
    assert all(
        button.isEnabled()
        for button in preview._mark_buttons.values()
    )
    next(
        shortcut
        for shortcut in window.findChildren(QShortcut)
        if shortcut.key().toString() == "1"
    ).activated.emit()
    qapp.processEvents()
    assert models[3].mark == PhotoMark.KEEP.value
    assert (
        window._project_store.get_photo("photo-3").mark
        == PhotoMark.KEEP.value
    )
    assert (tmp_path / "photo-3.jpg").is_file()
    _press(qapp, preview, Qt.Key.Key_Escape)
    _press(qapp, browser._view, Qt.Key.Key_2)
    assert models[0].mark == ""

    window.enter_culling()
    workspace = window._culling_workspace
    _press(qapp, workspace, Qt.Key.Key_Right)
    assert window._culling_session.current_id == "photo-1"
    _press(qapp, workspace, Qt.Key.Key_Left)
    assert window._culling_session.current_id == "photo-0"
    for key, index, mark in (
        (Qt.Key.Key_1, 0, PhotoMark.KEEP),
        (Qt.Key.Key_2, 1, PhotoMark.HOLD),
        (Qt.Key.Key_3, 2, PhotoMark.DISCARD),
    ):
        _press(qapp, workspace, key)
        qtbot.waitUntil(
            lambda expected=index, value=mark: (
                models[expected].mark == value.value
                and not window.culling_move_pending
            ),
            timeout=2000,
        )

    assert [
        window._project_store.get_photo(f"photo-{index}").mark
        for index in range(3)
    ] == [PhotoMark.KEEP.value, PhotoMark.HOLD.value, PhotoMark.DISCARD.value]

    window.request_culling_undo()
    qtbot.waitUntil(
        lambda: (
            models[2].mark == PhotoMark.UNPROCESSED.value
            and not window.culling_move_pending
        ),
        timeout=2000,
    )
    assert window._culling_session.current_id == "photo-2"

    shortcut_sequences = {
        shortcut.key().toString()
        for shortcut in window.findChildren(QShortcut)
    }
    assert QKeySequence(
        QKeySequence.StandardKey.Undo
    ).toString() in shortcut_sequences
    _press(qapp, workspace, Qt.Key.Key_Escape)
    assert window._stack.currentWidget() is browser
    window.close()
    qapp.processEvents()


def test_text_editor_focus_keeps_number_and_navigation_shortcuts_local(
    qapp,
    tmp_path,
) -> None:
    window, models = _window(qapp, tmp_path)
    browser = window.thumbnail_browser
    browser.select_photo_id("photo-1")
    editor = QLineEdit(window)
    editor.show()
    editor.setFocus()
    qapp.processEvents()

    QTest.keyClick(editor, Qt.Key.Key_1)
    QTest.keyClick(editor, Qt.Key.Key_Right)
    qapp.processEvents()

    assert editor.text() == "1"
    assert browser.selected_photo_ids() == ["photo-1"]
    assert all(model.mark == "" for model in models)
    window.close()
    qapp.processEvents()


def test_pending_culling_move_suppresses_keyboard_mark_navigation_undo_and_exit(
    qapp,
    tmp_path,
) -> None:
    window, models = _window(qapp, tmp_path)
    window.enter_culling()
    workspace = window._culling_workspace
    current = window._culling_session.current_id
    window._active_culling_move = object()
    workspace.set_move_pending(True)
    try:
        for key in (
            Qt.Key.Key_Right,
            Qt.Key.Key_1,
            Qt.Key.Key_Escape,
        ):
            _press(qapp, workspace, key)
        modifier = (
            Qt.KeyboardModifier.MetaModifier
            if sys.platform == "darwin"
            else Qt.KeyboardModifier.ControlModifier
        )
        _press(qapp, workspace, Qt.Key.Key_Z, modifier)

        assert window._culling_session.current_id == current
        assert models[0].mark == ""
        assert window._stack.currentWidget() is workspace
    finally:
        window._active_culling_move = None
        workspace.set_move_pending(False)
        window.close()
        qapp.processEvents()


def test_keyboard_handles_empty_grid_and_empty_preview_without_triggering_business_actions(
    qapp,
) -> None:
    window = MainWindow(shutdown_timeout=0.2)
    window.install_keyboard_manager(KeyboardManager())
    window.show()
    qapp.processEvents()

    for key in (
        Qt.Key.Key_Left,
        Qt.Key.Key_Right,
        Qt.Key.Key_1,
        Qt.Key.Key_2,
        Qt.Key.Key_3,
        Qt.Key.Key_Escape,
    ):
        _press(qapp, window.thumbnail_browser._view, key)

    assert window.thumbnail_browser.selected_photo_ids() == []
    window.close()
    qapp.processEvents()
