from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QThread, Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QLabel, QListView, QToolButton

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.ui.image_model import ImageFileModel


def _models(count: int = 3) -> dict[str, ImageFileModel]:
    return {
        f"photo-{row}": ImageFileModel(
            f"/not-read/photo-{row}.jpg",
            photo_id=f"photo-{row}",
        )
        for row in range(count)
    }


def _image(color: str) -> QImage:
    image = QImage(24, 16, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def _wait(qapp, predicate, timeout_ms: int = 3_000) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
    assert predicate()


def _image_view_text(widget) -> str:
    return "\n".join(
        item.toPlainText()
        for item in widget._image_view.scene().items()
        if hasattr(item, "toPlainText")
    )


def test_workspace_matches_confirmed_spatial_structure(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()

    assert widget._body_layout.indexOf(widget._filmstrip) == 0
    assert widget._body_layout.indexOf(widget._image_view) == 1
    assert widget._filmstrip._view.flow() is QListView.Flow.TopToBottom
    assert [button.text() for button in widget._mark_buttons] == [
        "保留",
        "待定",
        "舍弃",
    ]
    assert widget.layout().indexOf(widget._action_bar) == widget.layout().count() - 1


def test_filter_menu_actions_bind_enums_and_refresh_capture_order(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(3)
    models["photo-0"].mark = PhotoMark.KEEP.value
    models["photo-0"].technical_status = AnalysisStatus.SUCCEEDED
    models["photo-0"].focus_conclusion = AnalysisFilter.CLEAR.value
    models["photo-1"].technical_status = AnalysisStatus.SUCCEEDED
    models["photo-1"].focus_conclusion = AnalysisFilter.CLEAR.value
    models["photo-2"].technical_status = AnalysisStatus.SUCCEEDED
    models["photo-2"].focus_conclusion = AnalysisFilter.OUT_OF_FOCUS.value
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)

    assert isinstance(widget._status_filter, QToolButton)
    assert isinstance(widget._analysis_filter, QToolButton)
    assert widget._status_filter.focusPolicy() is Qt.FocusPolicy.StrongFocus
    assert widget._analysis_filter.focusPolicy() is Qt.FocusPolicy.StrongFocus
    assert widget._status_filter.popupMode() is (
        QToolButton.ToolButtonPopupMode.InstantPopup
    )
    assert widget._analysis_filter.popupMode() is (
        QToolButton.ToolButtonPopupMode.InstantPopup
    )
    assert widget._status_filter.menu().accessibleName() == "状态筛选选项"
    assert widget._analysis_filter.menu().accessibleName() == "分析筛选选项"
    status_actions = widget._status_filter.menu().actions()
    analysis_actions = widget._analysis_filter.menu().actions()
    assert [action.data() for action in status_actions] == list(StatusFilter)
    assert [action.data() for action in analysis_actions] == [
        value
        for value in AnalysisFilter
        if value is not AnalysisFilter.ANALYSIS_UNFINISHED
    ]
    status_changes = QSignalSpy(widget._status_filter.currentIndexChanged)
    next(
        action
        for action in status_actions
        if action.data() is StatusFilter.UNPROCESSED
    ).trigger()
    assert len(status_changes) == 0

    next(
        action
        for action in status_actions
        if action.data() is StatusFilter.ALL
    ).trigger()
    assert len(status_changes) == 1
    next(
        action
        for action in analysis_actions
        if action.data() is AnalysisFilter.CLEAR
    ).trigger()

    assert session.status_filter is StatusFilter.ALL
    assert session.analysis_filter is AnalysisFilter.CLEAR
    assert widget._filmstrip._model.photo_ids() == ["photo-0", "photo-1"]
    assert widget._status_filter.text() == StatusFilter.ALL.value
    assert widget._analysis_filter.text() == AnalysisFilter.CLEAR.value
    assert next(
        action
        for action in status_actions
        if action.data() is StatusFilter.ALL
    ).isChecked()
    assert sum(action.isChecked() for action in status_actions) == 1
    widget.close()


def test_unfinished_filter_menu_action_is_dynamic_and_checked(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(2)
    models["photo-0"].technical_status = AnalysisStatus.FAILED
    models["photo-1"].technical_status = AnalysisStatus.SUCCEEDED
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)

    unfinished_action = next(
        action
        for action in widget._analysis_filter.menu().actions()
        if action.data() is AnalysisFilter.ANALYSIS_UNFINISHED
    )
    unfinished_action.trigger()

    assert session.analysis_filter is AnalysisFilter.ANALYSIS_UNFINISHED
    assert unfinished_action.isChecked()
    assert widget._filmstrip._model.photo_ids() == ["photo-0"]

    models["photo-0"].technical_status = AnalysisStatus.SUCCEEDED
    widget.refresh_current()

    assert session.analysis_filter is AnalysisFilter.ALL
    assert widget._analysis_filter.currentData() is AnalysisFilter.ALL
    assert all(
        action.data() is not AnalysisFilter.ANALYSIS_UNFINISHED
        for action in widget._analysis_filter.menu().actions()
    )
    widget.close()


def test_pending_move_refuses_filter_menu_action(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(1)
    models["photo-0"].technical_status = AnalysisStatus.SUCCEEDED
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)
    widget.set_move_pending(True)

    next(
        action
        for action in widget._status_filter.menu().actions()
        if action.data() is StatusFilter.ALL
    ).trigger()

    assert session.status_filter is StatusFilter.UNPROCESSED
    assert widget._status_filter.currentData() is StatusFilter.UNPROCESSED
    assert widget._status_filter.isEnabled() is False
    widget.close()


def test_pending_move_disables_new_filters_and_summary_review_actions(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()
    widget.set_move_pending(True)

    assert widget._status_filter.isEnabled() is False
    assert widget._analysis_filter.isEnabled() is False
    assert widget._review_hold_btn.isEnabled() is False
    assert widget._review_discard_btn.isEnabled() is False


def test_information_is_opt_in_and_shows_only_trustworthy_conclusions(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    model = ImageFileModel(
        "/not-read/portrait.jpg",
        filename="portrait.jpg",
        photo_id="portrait",
        camera_model="D850",
        aperture="f/2.8",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="过曝",
        face_count=1,
        portrait_assessed=True,
    )
    session = CullingSession([model.photo_id], lambda _photo_id: model)
    widget = CullingWorkspace(
        provider=lambda _photo_id: model,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)

    assert widget._information_panel.isHidden()
    widget._information_btn.click()

    assert widget._information_panel.isHidden() is False
    visible_text = "\n".join(
        label.text() for label in widget._information_panel.findChildren(QLabel)
    )
    for expected in ("portrait.jpg", "D850", "f/2.8", "清晰", "过曝", "检测到人像"):
        assert expected in visible_text
    assert "裁切" not in visible_text
    assert "置信度" not in visible_text
    widget.close()


def _assert_open_information_has_no_stale_current(
    widget, *, progress: str
) -> None:
    assert widget.current_photo_id is None
    assert widget._information_btn.isChecked()
    assert widget._information_panel.isHidden() is False
    assert widget._filename_label.text() == ""
    assert widget._filename_label.toolTip() == ""
    assert widget._progress_label.text() == progress
    assert {
        label.text() for label in widget._information_values.values()
    } == {"—"}


def test_open_information_clears_when_filter_scope_becomes_empty(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    model = ImageFileModel(
        "/not-read/portrait.jpg",
        filename="portrait.jpg",
        photo_id="portrait",
        camera_model="D850",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    session = CullingSession([model.photo_id], lambda _photo_id: model)
    widget = CullingWorkspace(
        provider=lambda _photo_id: model,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)
    widget._information_btn.click()
    assert widget._information_values["filename"].text() == "portrait.jpg"

    widget.set_filters(StatusFilter.KEEP, AnalysisFilter.ALL)

    _assert_open_information_has_no_stale_current(widget, progress="0 / 0")
    widget.close()


def test_open_information_clears_when_current_provider_record_disappears(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    model = ImageFileModel(
        "/not-read/portrait.jpg",
        filename="portrait.jpg",
        photo_id="portrait",
        camera_model="D850",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    available = {model.photo_id: model}
    session = CullingSession([model.photo_id], lambda _photo_id: model)
    widget = CullingWorkspace(
        provider=available.get,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.set_session(session)
    widget._information_btn.click()
    assert widget._information_values["filename"].text() == "portrait.jpg"
    available.clear()

    widget.refresh_current()

    _assert_open_information_has_no_stale_current(widget, progress="0 / 0")
    widget.close()


def test_culling_image_tools_keep_fit_zoom_and_view_rotation_available(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()

    assert set(widget._tool_buttons) == {
        "zoom_out",
        "fit",
        "actual",
        "zoom_in",
        "rotate_left",
        "rotate_right",
    }
    widget._tool_buttons["rotate_right"].click()
    assert widget._image_view.rotation == 90


def test_mark_buttons_have_text_icon_shortcut_and_accessible_name(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()

    for shortcut, button in zip(("1", "2", "3"), widget._mark_buttons, strict=True):
        assert button.text()
        assert not button.icon().isNull()
        assert shortcut in button.toolTip()
        assert button.accessibleName()


def test_floating_action_bar_fades_at_idle_and_wakes_for_interaction(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()
    widget.show()
    qapp.processEvents()

    widget._fade_action_bar()
    assert widget._action_opacity.opacity() == pytest.approx(0.55)

    widget._wake_action_bar()
    assert widget._action_opacity.opacity() == pytest.approx(1.0)
    widget.close()


def test_set_session_refreshes_visible_ids_and_current_selection(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models()
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )

    widget.set_session(session)

    assert widget._filmstrip._model.photo_ids() == list(models)
    assert widget._filmstrip.current_photo_id() == "photo-0"
    session.navigate(2)
    widget.refresh_current()
    assert widget._filmstrip.current_photo_id() == "photo-2"
    widget.close()


def test_mark_buttons_emit_current_stable_id_without_mutating_session(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(1)
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    requested: list[tuple[str, PhotoMark]] = []
    widget.mark_requested.connect(
        lambda photo_id, mark: requested.append((photo_id, mark))
    )

    widget._mark_buttons[0].click()
    widget.set_session(session)
    for button in widget._mark_buttons:
        button.click()

    assert requested == [
        ("photo-0", PhotoMark.KEEP),
        ("photo-0", PhotoMark.HOLD),
        ("photo-0", PhotoMark.DISCARD),
    ]
    assert models["photo-0"].mark == ""
    assert session.current_id == "photo-0"
    widget.close()


def test_refresh_clears_when_session_current_leaves_validated_filmstrip(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = {
        "a": ImageFileModel("/not-read/a.jpg", photo_id="a"),
        "b": ImageFileModel("/not-read/b.jpg", photo_id="b"),
    }
    session = CullingSession(["a", "b"], models.__getitem__)
    scripted: dict[str, list[ImageFileModel | None]] = {
        "a": [models["a"], models["a"], models["a"], None],
        "b": [None, None],
    }
    defaults = {"a": None, "b": models["b"]}

    def provider(photo_id: str) -> ImageFileModel | None:
        values = scripted.get(photo_id)
        if values:
            return values.pop(0)
        return defaults[photo_id]

    session._provider = provider  # type: ignore[attr-defined]
    decoded: list[str] = []
    requested: list[tuple[str, PhotoMark]] = []
    widget = CullingWorkspace(
        provider=provider,
        preview_decoder=lambda path, _edge: decoded.append(path) or _image("red"),
    )
    widget.mark_requested.connect(
        lambda photo_id, mark: requested.append((photo_id, mark))
    )

    widget.set_session(session)
    _wait(qapp, lambda: widget.preview_unfinished_count == 0)
    widget._mark_buttons[0].click()

    assert widget._filmstrip._model.photo_ids() == ["a"]
    assert widget._filmstrip.current_photo_id() is None
    assert widget.loaded_photo_id is None
    assert decoded == []
    assert "正在加载" not in _image_view_text(widget)
    assert requested == []
    widget.close()


def test_mark_uses_validated_workspace_snapshot_when_session_moves_later(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(2)
    session = CullingSession(models, models.__getitem__)
    requested: list[tuple[str, PhotoMark]] = []
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    widget.mark_requested.connect(
        lambda photo_id, mark: requested.append((photo_id, mark))
    )
    widget.set_session(session)
    session.navigate(1)

    widget._mark_buttons[0].click()

    assert widget._filmstrip.current_photo_id() == "photo-0"
    assert requested == [("photo-0", PhotoMark.KEEP)]
    widget.close()


def test_shell_controls_forward_exact_navigation_and_lifecycle_signals(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()
    back: list[bool] = []
    navigation: list[int] = []
    undo: list[bool] = []
    opened: list[bool] = []
    widget.back_requested.connect(lambda: back.append(True))
    widget.navigate_requested.connect(navigation.append)
    widget.undo_requested.connect(lambda: undo.append(True))
    widget.open_directory_requested.connect(lambda: opened.append(True))

    widget._back_btn.click()
    widget._previous_btn.click()
    widget._next_btn.click()
    widget._undo_btn.click()
    widget._open_directory_btn.click()

    assert back == [True]
    assert navigation == [-1, 1]
    assert undo == [True]
    assert opened == [True]


def test_filmstrip_activation_requests_relative_session_navigation(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(4)
    session = CullingSession(models, models.__getitem__)
    widget = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_: _image("red"),
    )
    requested: list[int] = []
    widget.navigate_requested.connect(requested.append)
    widget.set_session(session)

    widget._filmstrip.photo_activated.emit("photo-3")

    assert requested == [3]
    assert session.current_id == "photo-0"
    widget.close()


def test_large_preview_decode_is_bounded_off_owner_thread_and_drops_stale_result(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(3)
    session = CullingSession(models, models.__getitem__)
    first_started = threading.Event()
    release_first = threading.Event()
    decoder_threads: list[QThread] = []
    decoded: list[str] = []

    def decoder(path: str, _max_edge: int) -> QImage:
        decoder_threads.append(QThread.currentThread())
        name = Path(path).stem
        decoded.append(name)
        if name == "photo-0":
            first_started.set()
            assert release_first.wait(3)
            return _image("red")
        return _image("green")

    widget = CullingWorkspace(provider=models.get, preview_decoder=decoder)
    pixmap_threads: list[QThread] = []
    set_pixmap = widget._image_view.set_pixmap

    def record_pixmap_thread(*args, **kwargs) -> None:
        pixmap_threads.append(QThread.currentThread())
        set_pixmap(*args, **kwargs)

    widget._image_view.set_pixmap = record_pixmap_thread  # type: ignore[method-assign]
    widget.set_session(session)
    assert first_started.wait(2)
    session.navigate(1)
    widget.refresh_current()
    session.navigate(1)
    widget.refresh_current()

    assert widget.preview_unfinished_count <= 2
    assert widget.preview_pending_count <= 1
    release_first.set()
    _wait(qapp, lambda: widget.loaded_photo_id == "photo-2")

    assert decoded == ["photo-0", "photo-2"]
    assert decoder_threads
    assert all(thread is not QThread.currentThread() for thread in decoder_threads)
    assert pixmap_threads == [widget.thread()]
    assert widget._image_view._pixmap_item is not None
    assert (
        widget._image_view._pixmap_item.pixmap().toImage().pixelColor(0, 0)
        == QColor("green")
    )
    widget.close()
    _wait(qapp, lambda: widget.preview_unfinished_count == 0)


def test_workspace_uses_public_bounded_preview_loader(qapp) -> None:
    from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()

    assert isinstance(widget._preview_loader, BoundedPreviewLoader)
    assert not hasattr(widget, "_active_preview_thread")
    assert not hasattr(widget, "_pending_preview_request")
    widget.close()


def test_workspace_close_and_delete_with_blocked_decode_reaps_worker(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace
    from src.xuanpian.ui.preview_widget import preview_decode_reaper_count

    baseline = preview_decode_reaper_count()
    models = _models(1)
    session = CullingSession(models, models.__getitem__)
    started = threading.Event()
    release = threading.Event()
    decoder_finished = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        try:
            assert release.wait(3)
            return _image("red")
        finally:
            decoder_finished.set()

    widget = CullingWorkspace(provider=models.get, preview_decoder=decoder)
    widget.set_session(session)
    assert started.wait(2)

    try:
        widget.close()
        widget.deleteLater()
        assert widget.preview_unfinished_count == 0
        assert preview_decode_reaper_count() == baseline + 1
    finally:
        release.set()

    assert decoder_finished.wait(2)
    _wait(qapp, lambda: preview_decode_reaper_count() == baseline)
    assert widget.loaded_photo_id is None


def test_refresh_without_available_model_never_decodes_or_crashes(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(1)
    session = CullingSession(models, models.__getitem__)
    decoded: list[str] = []
    widget = CullingWorkspace(
        provider={}.__getitem__,
        preview_decoder=lambda path, _edge: decoded.append(path) or _image("red"),
    )

    widget.set_session(session)
    widget.refresh_current()

    assert decoded == []
    assert widget.loaded_photo_id is None
    widget.close()


def test_refresh_provider_exception_uses_shared_fail_closed_policy(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(1)
    session = CullingSession(models, models.__getitem__)

    def unavailable(_photo_id: str) -> ImageFileModel:
        raise RuntimeError("provider temporarily unavailable")

    widget = CullingWorkspace(
        provider=unavailable,
        preview_decoder=lambda *_: _image("red"),
    )

    widget.set_session(session)

    assert widget._filmstrip._model.photo_ids() == []
    assert widget.loaded_photo_id is None
    widget.close()


@pytest.mark.parametrize(
    "invalid_record",
    [
        object(),
        ImageFileModel("/not-read/wrong.jpg", photo_id="wrong"),
    ],
)
def test_refresh_with_malformed_provider_record_fails_closed(
    qapp,
    invalid_record: object,
) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    models = _models(1)
    session = CullingSession(models, models.__getitem__)
    decoded: list[str] = []

    widget = CullingWorkspace(
        provider=lambda _photo_id: invalid_record,  # type: ignore[arg-type]
        preview_decoder=lambda path, _edge: decoded.append(path) or _image("red"),
    )

    widget.set_session(session)

    assert widget._filmstrip._model.photo_ids() == []
    assert decoded == []
    assert widget.loaded_photo_id is None
    widget.close()


def test_show_summary_displays_canonical_counts_and_directory_action(qapp) -> None:
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    widget = CullingWorkspace()
    widget.show()

    widget.show_summary({"保留": 82, "待定": 14, "舍弃": 52})
    qapp.processEvents()

    assert widget._summary.isVisible()
    assert "保留 82" in widget._summary_label.text()
    assert "待定 14" in widget._summary_label.text()
    assert "舍弃 52" in widget._summary_label.text()
    assert widget._open_directory_btn.text() == "打开照片目录"
    widget.close()
