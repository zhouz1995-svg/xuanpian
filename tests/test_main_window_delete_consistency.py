from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.deleted: list[tuple[str, ...]] = []

    def delete_photos(self, photo_ids: tuple[str, ...]) -> None:
        if self.fail:
            raise OSError("disk full")
        self.deleted.append(photo_ids)

    def close(self) -> None:
        pass


def _window_with_models() -> tuple[MainWindow, list[ImageFileModel]]:
    window = MainWindow()
    models = [
        ImageFileModel(f"/missing/{name}.jpg", photo_id=name)
        for name in ("first", "target", "last")
    ]
    window.thumbnail_browser.set_models(models)
    window.enter_preview_photo("target")
    return window, models


def test_delete_is_db_first_and_preview_selects_neighbor(
    qapp, monkeypatch
) -> None:
    window, _models = _window_with_models()
    store = _Store()
    window._project_store = store
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.delete_photo("target")

    assert store.deleted == [("target",)]
    assert window.thumbnail_browser.current_photo_ids() == ["first", "last"]
    assert window.model_for_photo_id("target") is None
    assert window._preview_widget.current_photo_id() == "last"
    assert window._preview_widget._filmstrip_model.photo_ids() == [
        "first",
        "last",
    ]
    window.close()


def test_delete_db_failure_keeps_browser_and_preview_unchanged(
    qapp, monkeypatch
) -> None:
    window, models = _window_with_models()
    window._project_store = _Store(fail=True)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    monkeypatch.setattr(QMessageBox, "warning", lambda *_args, **_kwargs: None)

    window.delete_photo("target")

    assert window.thumbnail_browser.all_models() == models
    assert window.thumbnail_browser.current_photo_ids() == [
        "first",
        "target",
        "last",
    ]
    assert window._preview_widget.current_photo_id() == "target"
    window.close()


def test_preview_delete_key_runs_real_main_window_command(
    qapp, monkeypatch
) -> None:
    window, _models = _window_with_models()
    store = _Store()
    window._project_store = store
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    preview = window._preview_widget
    window.show()
    qapp.processEvents()
    preview.setFocus()

    QTest.keyClick(preview, Qt.Key.Key_Delete)
    qapp.processEvents()

    assert store.deleted == [("target",)]
    assert preview.current_photo_id() == "last"
    window.close()


def test_delete_confirmation_explains_project_only_removal(
    qapp, monkeypatch
) -> None:
    window, _models = _window_with_models()
    prompts: list[tuple[str, str]] = []

    def capture_question(_parent, title, message, *_args):
        prompts.append((title, message))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", capture_question)

    window.delete_photo("target")

    assert len(prompts) == 1
    title, message = prompts[0]
    assert "从项目移除" in title
    assert "项目内的标记和分析记录" in message
    assert "不会删除磁盘上的 JPEG/RAW 源文件" in message
    assert "淘汰" not in title
    window.close()
