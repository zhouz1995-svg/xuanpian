from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QPushButton, QWidget

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.preview_widget import PreviewWidget
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser


def test_preview_has_analysis_and_exif_tabs(qapp) -> None:
    preview = PreviewWidget()

    assert [
        button.text() for button in preview._tab_header.buttons
    ] == ["技术结论", "拍摄信息"]
    assert preview._stack.count() == 2


def test_preview_failed_state_hides_stale_score(qapp) -> None:
    model = ImageFileModel(
        "/fake/stale.jpg",
        technical_status=AnalysisStatus.FAILED,
        focus_conclusion="清晰",
    )
    preview = PreviewWidget()

    preview._show_analysis(model)

    assert preview._blur_value.text() == "分析失败"
    assert "999" not in preview._blur_value.text()


def test_preview_exif_missing_values_use_em_dash(qapp) -> None:
    preview = PreviewWidget()

    preview._show_exif(ImageFileModel("/fake/plain.jpg", camera_model="D850"))

    assert preview._exif_values["camera_model"].text() == "D850"
    assert preview._exif_values["lens_model"].text() == "—"


def test_preview_exif_error_is_explicit(qapp) -> None:
    preview = PreviewWidget()

    preview._show_exif(
        ImageFileModel("/fake/broken.jpg", exif_error="broken.jpg: decode failed")
    )

    assert preview._exif_status.text() == "EXIF 读取失败"


def test_preview_exposes_current_business_photo_id(qapp) -> None:
    preview = PreviewWidget()
    models = {
        "first": ImageFileModel("/fake/first.jpg", photo_id="first"),
        "second": ImageFileModel("/fake/second.jpg", photo_id="second"),
    }
    photo_ids = ["first", "second"]

    preview.load_photo("second", lambda: list(photo_ids), models.get)

    assert preview.current_photo_id() == "second"


def test_preview_mark_passes_business_photo_id_to_main_window(
    qapp, monkeypatch
) -> None:
    preview = PreviewWidget()
    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    calls: list[tuple[str, str]] = []

    class MainWindow:
        def mark_photo(self, photo_id: str, mark: str) -> None:
            calls.append((photo_id, mark))

    monkeypatch.setattr(preview, "_find_main_window", lambda: MainWindow())

    preview._mark_current("入选")

    assert calls == [("photo", "入选")]


def test_preview_identity_survives_external_current_list_reorder(
    qapp, monkeypatch
) -> None:
    models = {
        photo_id: ImageFileModel(
            f"/fake/{photo_id}.jpg",
            photo_id=photo_id,
        )
        for photo_id in ("first", "target", "last")
    }
    photo_ids = ["first", "target", "last"]
    preview = PreviewWidget()
    calls: list[tuple[str, str]] = []

    class MainWindow:
        def mark_photo(self, photo_id: str, mark: str) -> None:
            calls.append((photo_id, mark))

    monkeypatch.setattr(preview, "_find_main_window", lambda: MainWindow())
    preview.load_photo("target", lambda: list(photo_ids), models.get)

    photo_ids[:] = ["last", "first", "target"]
    preview._mark_current("入选")

    assert preview.current_photo_id() == "target"
    assert calls == [("target", "入选")]


def test_preview_removed_photo_selects_neighbor_without_identity_drift(
    qapp, monkeypatch
) -> None:
    models = {
        photo_id: ImageFileModel(
            f"/fake/{photo_id}.jpg",
            photo_id=photo_id,
        )
        for photo_id in ("first", "target", "last")
    }
    photo_ids = ["first", "target", "last"]
    preview = PreviewWidget()
    calls: list[tuple[str, str]] = []

    class MainWindow:
        def mark_photo(self, photo_id: str, mark: str) -> None:
            calls.append((photo_id, mark))

    monkeypatch.setattr(preview, "_find_main_window", lambda: MainWindow())
    preview.load_photo("target", lambda: list(photo_ids), models.get)

    photo_ids.remove("target")
    preview._filmstrip_model.set_photo_ids(photo_ids)
    preview._mark_current("待定")

    assert preview.current_photo_id() == "last"
    assert calls == [("last", "待定")]


def test_preview_exits_when_current_sequence_becomes_empty(
    qapp, monkeypatch
) -> None:
    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    photo_ids = ["photo"]
    preview = PreviewWidget()
    exits: list[bool] = []

    class MainWindow:
        def exit_preview(self) -> None:
            exits.append(True)

    monkeypatch.setattr(preview, "_find_main_window", lambda: MainWindow())
    preview.load_photo("photo", lambda: list(photo_ids), {"photo": model}.get)

    photo_ids.clear()
    preview._filmstrip_model.set_photo_ids(photo_ids)

    assert preview.current_photo_id() is None
    assert exits == [True]


def test_photo_tools_live_in_compact_bottom_overlay(qapp) -> None:
    preview = PreviewWidget()
    preview.resize(1200, 800)
    preview.show()
    qapp.processEvents()

    top_bar = preview.findChild(QWidget, "previewTopBar")
    assert [button.text() for button in top_bar.findChildren(QPushButton)] == [
        "← 返回",
        "对比本组",
    ]
    assert preview._compare_btn.isHidden() is True  # only shown for burst photos
    assert set(preview._tool_buttons) == {
        "zoom_out",
        "fit",
        "actual",
        "zoom_in",
        "rotate_left",
        "rotate_right",
    }
    assert max(button.width() for button in preview._tool_buttons.values()) <= 52

    preview._tool_overlay.show()
    preview._position_tool_overlay()
    viewport = preview._image_view.viewport()
    assert abs(preview._tool_overlay.geometry().center().x() - viewport.rect().center().x()) <= 1
    assert preview._tool_overlay.geometry().bottom() <= viewport.rect().bottom() - 12


def test_information_tabs_use_scrollable_full_width_blocks(qapp) -> None:
    preview = PreviewWidget()

    assert preview._analysis_scroll.widgetResizable() is True
    assert preview._exif_scroll.widgetResizable() is True
    for label in (
            preview._filename_value,
            preview._blur_value,
            preview._exposure_value,
            preview._portrait_value,
            *preview._exif_values.values(),
    ):
        assert label.wordWrap() is True
        assert label.property("infoValue") is True


def test_preview_information_remains_truthful_after_culling_ui_completion(qapp) -> None:
    preview = PreviewWidget()
    model = ImageFileModel(
        "/fake/unavailable.jpg",
        technical_status=AnalysisStatus.FAILED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=2,
        portrait_assessed=True,
    )

    preview._show_analysis(model)

    assert preview._blur_value.text() == "分析失败"
    assert preview._exposure_value.text() == "分析失败"
    assert preview._portrait_value.text() == "不判断"


def test_virtualized_grid_double_click_enters_preview_by_photo_id(
    qapp, monkeypatch
) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(
        [ImageFileModel("/fake/photo.jpg", photo_id="stable-photo")]
    )
    calls: list[str] = []

    class MainWindow:
        def enter_preview_photo(self, photo_id: str) -> None:
            calls.append(photo_id)

    monkeypatch.setattr(browser, "_find_main_window", lambda: MainWindow())

    browser._view.doubleClicked.emit(browser._photo_list_model.index(0, 0))

    assert calls == ["stable-photo"]


def _drag_panel_handle(panel_handle, start_global_x: int, end_global_x: int) -> None:
    def synth(kind: QEvent.Type, global_x: int) -> QMouseEvent:
        return QMouseEvent(
            kind,
            QPointF(2, 10),
            QPointF(global_x, 300),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    panel_handle.mousePressEvent(
        synth(QEvent.Type.MouseButtonPress, start_global_x)
    )
    panel_handle.mouseMoveEvent(
        synth(QEvent.Type.MouseMove, end_global_x)
    )
    panel_handle.mouseReleaseEvent(
        synth(QEvent.Type.MouseButtonRelease, end_global_x)
    )


def test_preview_info_panel_collapses_and_expands_via_edge_tab(qapp) -> None:
    preview = PreviewWidget()
    panel = preview._panel

    assert panel.is_collapsed is False
    assert panel.width() == 300
    assert panel._tab.isHidden() is False
    assert panel._tab.x() == 1
    assert preview._stack.isHidden() is False
    assert preview._tab_header.isHidden() is False

    panel._tab.click()
    QTest.qWait(260)
    assert panel.is_collapsed is True
    assert panel.width() == 24
    assert panel._tab.isHidden() is False
    assert panel._tab.x() == 1
    assert preview._stack.isHidden() is True
    assert preview._tab_header.isHidden() is True
    assert preview._mark_label.parentWidget().isHidden() is True

    panel._tab.click()
    QTest.qWait(260)
    assert panel.is_collapsed is False
    assert panel.width() == 300
    assert panel._tab.isHidden() is False
    assert panel._tab.x() == 1
    assert preview._stack.isHidden() is False
    assert preview._tab_header.isHidden() is False


def test_preview_info_panel_handle_drag_resizes_panel(qapp) -> None:
    preview = PreviewWidget()
    panel = preview._panel

    _drag_panel_handle(preview._panel_handle, 700, 620)
    assert panel.width() == 360  # 300 + 80, 上限 360
    assert panel.is_collapsed is False

    _drag_panel_handle(preview._panel_handle, 700, 760)
    assert panel.width() == 300  # 360 - 60


def test_preview_info_panel_handle_drag_past_rail_collapses(qapp) -> None:
    preview = PreviewWidget()
    panel = preview._panel

    _drag_panel_handle(preview._panel_handle, 700, 2200)
    assert panel.is_collapsed is True
    QTest.qWait(260)
    assert panel.width() == 24
    assert preview._stack.isHidden() is True
