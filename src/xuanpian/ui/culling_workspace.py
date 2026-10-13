from __future__ import annotations

from collections.abc import Callable, Mapping
import logging

from PyQt6.QtCore import QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.ui.async_preview_loader import (
    DEFAULT_PREVIEW_MAX_EDGE,
    BoundedPreviewLoader,
    PreviewDecoder,
    decode_preview_image,
)
from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.image_view import ImageView
from src.xuanpian.ui.icons import ui_icon

logger = logging.getLogger(__name__)


class _FilterMenuButton(QToolButton):
    """A checked-action menu exposing the small selection API used here."""

    currentIndexChanged = pyqtSignal(int)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._current_index = -1
        menu = QMenu(self)
        self.setMenu(menu)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("cullingFilterButton", True)

    def addItem(self, text: str, data: object) -> None:
        action = self.menu().addAction(text)
        action.setCheckable(True)
        action.setData(data)
        action.triggered.connect(
            lambda _checked=False, selected=action: self._select_action(
                selected
            )
        )
        if self.currentIndex() < 0:
            self.setCurrentIndex(0)

    def removeItem(self, index: int) -> None:
        actions = self.menu().actions()
        if not 0 <= index < len(actions):
            return
        action = actions[index]
        previous = self._current_index
        self.menu().removeAction(action)
        action.deleteLater()
        if not self.count():
            self.setCurrentIndex(-1)
        elif previous == index:
            self._current_index = -1
            self.setCurrentIndex(min(index, self.count() - 1))
        elif previous > index:
            self._current_index -= 1
            self._sync_action_state()

    def count(self) -> int:
        return len(self.menu().actions())

    def itemData(self, index: int) -> object | None:
        actions = self.menu().actions()
        return actions[index].data() if 0 <= index < len(actions) else None

    def findData(self, data: object) -> int:
        for index, action in enumerate(self.menu().actions()):
            if action.data() == data:
                return index
        return -1

    def currentData(self) -> object | None:
        index = self.currentIndex()
        return self.itemData(index)

    def currentIndex(self) -> int:
        return self._current_index

    def setCurrentIndex(self, index: int) -> None:
        actions = self.menu().actions()
        normalized = index if 0 <= index < len(actions) else -1
        previous = self._current_index
        self._current_index = normalized
        self._sync_action_state()
        if previous != normalized:
            self.currentIndexChanged.emit(normalized)

    def _sync_action_state(self) -> None:
        actions = self.menu().actions()
        selected = (
            actions[self._current_index]
            if 0 <= self._current_index < len(actions)
            else None
        )
        for action in actions:
            action.setChecked(action is selected)
        self.setText("" if selected is None else selected.text())

    def _select_action(self, action: object) -> None:
        actions = self.menu().actions()
        try:
            index = actions.index(action)
        except ValueError:
            return
        self.setCurrentIndex(index)


class CullingWorkspace(QWidget):
    """Virtualized culling shell with bounded, stale-safe preview loading."""

    back_requested = pyqtSignal()
    mark_requested = pyqtSignal(str, object)
    navigate_requested = pyqtSignal(int)
    undo_requested = pyqtSignal()
    open_directory_requested = pyqtSignal()
    review_hold_requested = pyqtSignal()
    review_discard_requested = pyqtSignal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        provider: Callable[[str], ImageFileModel | None] | None = None,
        preview_decoder: PreviewDecoder = decode_preview_image,
        preview_max_edge: int = DEFAULT_PREVIEW_MAX_EDGE,
    ) -> None:
        super().__init__(parent)
        if preview_max_edge <= 0:
            raise ValueError("preview_max_edge must be positive")
        self._provider = provider or (lambda _photo_id: None)
        self._session: CullingSession | None = None
        self._current_photo_id: str | None = None
        self._move_pending = False
        # Standalone workspaces retain the Task 2 signal-only control contract.
        # The owning MainWindow applies real command availability explicitly.
        self._undo_available = True
        self._preview_max_edge = preview_max_edge
        self._loaded_photo_id: str | None = None
        self._preview_loader = BoundedPreviewLoader(
            self,
            decoder=preview_decoder,
        )
        self._preview_loader.decoded.connect(self._on_preview_decoded)
        self._preview_loader.error.connect(self._on_preview_failed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._body = QWidget(self)
        self._body_layout = QHBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._filmstrip = CullingFilmstrip(self._provider, self._body)
        self._filmstrip.setObjectName("cullingFilmstrip")
        self._filmstrip.setFixedWidth(132)
        self._filmstrip.photo_activated.connect(
            self._request_activated_photo
        )
        self._body_layout.addWidget(self._filmstrip)
        self._image_view = ImageView(self._body)
        self._image_view.setObjectName("cullingCanvas")
        self._image_view.setBackgroundBrush(QColor("#202124"))
        self._image_view.setMinimumSize(400, 300)
        self._body_layout.addWidget(self._image_view, 1)
        self._information_panel = self._build_information_panel(self._body)
        self._information_panel.hide()
        self._body_layout.addWidget(self._information_panel)
        root.addWidget(self._build_tool_bar())
        root.addWidget(self._body, 1)

        self._summary = self._build_summary(self)
        self._summary.hide()
        root.addWidget(self._summary, 1)

        self._action_bar = self._build_action_bar(self)
        self._action_bar.setMinimumWidth(560)
        self._action_bar.setMaximumWidth(780)
        root.addWidget(
            self._action_bar,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        self._action_opacity = QGraphicsOpacityEffect(self._action_bar)
        self._action_opacity.setOpacity(1.0)
        self._action_bar.setGraphicsEffect(self._action_opacity)
        self._action_idle_timer = QTimer(self)
        self._action_idle_timer.setSingleShot(True)
        self._action_idle_timer.setInterval(2500)
        self._action_idle_timer.timeout.connect(self._fade_action_bar)
        for control in (
            self,
            self._image_view.viewport(),
            self._action_bar,
            *self._mark_buttons,
            self._previous_btn,
            self._next_btn,
        ):
            control.installEventFilter(self)
        self._wake_action_bar()
        self._apply_control_state()

    def _build_tool_bar(self) -> QFrame:
        bar = QFrame(self)
        bar.setObjectName("cullingToolBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        self._back_btn = QPushButton("← 返回", bar)
        self._back_btn.setAccessibleName("返回整体预览")
        self._back_btn.clicked.connect(self.back_requested)
        layout.addWidget(self._back_btn)
        self._filename_label = QLabel("", bar)
        self._filename_label.setObjectName("cullingFilename")
        self._filename_label.setMinimumWidth(120)
        layout.addWidget(self._filename_label, 1)

        self._status_filter = _FilterMenuButton(bar)
        self._status_filter.setAccessibleName("状态筛选")
        self._status_filter.menu().setAccessibleName("状态筛选选项")
        for value in StatusFilter:
            self._status_filter.addItem(value.value, value)
        self._status_filter.currentIndexChanged.connect(
            self._on_filter_changed
        )
        layout.addWidget(self._status_filter)

        self._analysis_filter = _FilterMenuButton(bar)
        self._analysis_filter.setAccessibleName("分析筛选")
        self._analysis_filter.menu().setAccessibleName("分析筛选选项")
        for value in AnalysisFilter:
            self._analysis_filter.addItem(
                "全部分析结果"
                if value is AnalysisFilter.ALL
                else value.value,
                value,
            )
        self._analysis_filter.currentIndexChanged.connect(
            self._on_filter_changed
        )
        layout.addWidget(self._analysis_filter)

        self._tool_buttons: dict[str, QPushButton] = {}
        for key, icon_name, tooltip, slot in (
            ("zoom_out", "zoom_out", "缩小", self._image_view.zoom_out),
            ("fit", "fit", "适合窗口", self._image_view.zoom_to_fit),
            ("actual", "actual", "原始尺寸", self._image_view.zoom_to_100),
            ("zoom_in", "zoom_in", "放大", self._image_view.zoom_in),
            (
                "rotate_left",
                "rotate_left",
                "逆时针旋转 90°",
                lambda: self._image_view.rotate(-90),
            ),
            (
                "rotate_right",
                "rotate_right",
                "顺时针旋转 90°",
                lambda: self._image_view.rotate(90),
            ),
        ):
            button = QPushButton(ui_icon(icon_name), "", bar)
            button.setProperty("cullingToolButton", True)
            button.setAccessibleName(tooltip)
            button.setToolTip(tooltip)
            button.setFixedSize(30, 30)
            button.clicked.connect(slot)
            layout.addWidget(button)
            self._tool_buttons[key] = button

        self._information_btn = QPushButton(
            ui_icon("info"), "信息", bar
        )
        self._information_btn.setCheckable(True)
        self._information_btn.setAccessibleName("显示照片信息")
        self._information_btn.setToolTip("显示照片信息")
        self._information_btn.toggled.connect(
            self._information_panel_visibility_changed
        )
        layout.addWidget(self._information_btn)

        self._progress_label = QLabel("0 / 0", bar)
        self._progress_label.setObjectName("cullingProgress")
        layout.addWidget(self._progress_label)

        self._undo_btn = QPushButton(ui_icon("undo"), "撤销", bar)
        self._undo_btn.setAccessibleName("撤销上一次标记")
        self._undo_btn.setToolTip("撤销 · Command/Ctrl + Z")
        self._undo_btn.clicked.connect(self.undo_requested)
        layout.addWidget(self._undo_btn)
        return bar

    def _build_action_bar(self, parent: QWidget) -> QFrame:
        bar = QFrame(parent)
        bar.setObjectName("cullingActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(8)
        self._previous_btn = QPushButton("上一张", bar)
        self._previous_btn.setAccessibleName("上一张照片")
        self._previous_btn.setToolTip("上一张 · ←")
        self._previous_btn.clicked.connect(
            lambda checked=False: self.navigate_requested.emit(-1)
        )
        layout.addWidget(self._previous_btn)
        layout.addStretch(1)
        self._mark_buttons: list[QPushButton] = []
        for text, mark, icon_name, icon_color, shortcut, style_name in (
            ("保留", PhotoMark.KEEP, "pick", "#79B992", "1", "keep"),
            ("待定", PhotoMark.HOLD, "pending", "#D7AA62", "2", "hold"),
            ("舍弃", PhotoMark.DISCARD, "reject", "#D27880", "3", "discard"),
        ):
            button = QPushButton(
                ui_icon(icon_name, color=icon_color), text, bar
            )
            button.setProperty("cullingMark", style_name)
            button.setAccessibleName(f"{text}照片（快捷键 {shortcut}）")
            button.setToolTip(f"{text} · 快捷键 {shortcut}")
            button.clicked.connect(
                lambda checked=False, value=mark: self._request_mark(value)
            )
            layout.addWidget(button)
            self._mark_buttons.append(button)
        layout.addStretch(1)
        self._next_btn = QPushButton("下一张", bar)
        self._next_btn.setAccessibleName("下一张照片")
        self._next_btn.setToolTip("下一张 · →")
        self._next_btn.clicked.connect(
            lambda checked=False: self.navigate_requested.emit(1)
        )
        layout.addWidget(self._next_btn)
        return bar

    def _build_information_panel(self, parent: QWidget) -> QFrame:
        panel = QFrame(parent)
        panel.setObjectName("cullingInformationPanel")
        panel.setFixedWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = QLabel("照片信息", panel)
        heading.setObjectName("cullingInformationHeading")
        layout.addWidget(heading)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(9)
        self._information_values: dict[str, QLabel] = {}
        for row, (key, title) in enumerate(
            (
                ("filename", "文件名"),
                ("exif", "EXIF"),
                ("focus", "清晰度"),
                ("exposure", "曝光"),
                ("portrait", "人像"),
            )
        ):
            title_label = QLabel(title, panel)
            title_label.setProperty("informationTitle", True)
            value_label = QLabel("—", panel)
            value_label.setProperty("informationValue", True)
            value_label.setWordWrap(True)
            grid.addWidget(title_label, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value_label, row, 1)
            self._information_values[key] = value_label
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        layout.addStretch(1)
        return panel

    def _build_summary(self, parent: QWidget) -> QFrame:
        summary = QFrame(parent)
        summary.setObjectName("cullingSummary")
        layout = QVBoxLayout(summary)
        layout.addStretch(1)
        heading = QLabel("筛选完成", summary)
        heading.setObjectName("cullingSummaryHeading")
        layout.addWidget(heading)
        self._summary_label = QLabel(summary)
        self._summary_label.setObjectName("cullingSummaryCounts")
        layout.addWidget(self._summary_label)
        actions = QHBoxLayout()
        self._review_hold_btn = QPushButton("复查待定", summary)
        self._review_hold_btn.clicked.connect(self.review_hold_requested)
        actions.addWidget(self._review_hold_btn)
        self._review_discard_btn = QPushButton("复查舍弃", summary)
        self._review_discard_btn.clicked.connect(
            self.review_discard_requested
        )
        actions.addWidget(self._review_discard_btn)
        layout.addLayout(actions)
        self._open_directory_btn = QPushButton(
            ui_icon("folder"), "打开照片目录", summary
        )
        self._open_directory_btn.clicked.connect(
            self.open_directory_requested
        )
        layout.addWidget(self._open_directory_btn)
        self._summary_back_btn = QPushButton("返回整体预览", summary)
        self._summary_back_btn.clicked.connect(self.back_requested)
        layout.addWidget(self._summary_back_btn)
        layout.addStretch(1)
        return summary

    def set_session(self, session: CullingSession | None) -> None:
        if session is not None and not isinstance(session, CullingSession):
            raise TypeError("session must be a CullingSession or None")
        self._session = session
        self._information_btn.setChecked(False)
        self.refresh_current()

    def set_filters(
        self,
        status_filter: StatusFilter,
        analysis_filter: AnalysisFilter,
    ) -> None:
        if self._move_pending or self._session is None:
            return
        self._session.set_filters(status_filter, analysis_filter)
        self._sync_filter_controls()
        self.refresh_current()

    def _sync_filter_controls(self) -> None:
        session = self._session
        unfinished = AnalysisFilter.ANALYSIS_UNFINISHED
        available = session is not None and session.has_unfinished_analysis()
        index = self._analysis_filter.findData(unfinished)
        self._analysis_filter.blockSignals(True)
        if available and index < 0:
            self._analysis_filter.addItem(unfinished.value, unfinished)
        elif not available and index >= 0:
            self._analysis_filter.removeItem(index)
            if session is not None and session.analysis_filter is unfinished:
                session.set_filters(session.status_filter, AnalysisFilter.ALL)
        self._analysis_filter.blockSignals(False)
        status = (
            StatusFilter.UNPROCESSED
            if session is None
            else session.status_filter
        )
        analysis = (
            AnalysisFilter.ALL
            if session is None
            else session.analysis_filter
        )
        for control, value in (
            (self._status_filter, status),
            (self._analysis_filter, analysis),
        ):
            control.blockSignals(True)
            control.setCurrentIndex(control.findData(value))
            control.blockSignals(False)

    def _on_filter_changed(self, _index: int) -> None:
        if self._move_pending or self._session is None:
            self._sync_filter_controls()
            return
        status = self._status_filter.currentData()
        analysis = self._analysis_filter.currentData()
        if not isinstance(status, StatusFilter) or not isinstance(
            analysis, AnalysisFilter
        ):
            return
        self._session.set_filters(status, analysis)
        self.refresh_current()

    def _information_panel_visibility_changed(self, visible: bool) -> None:
        self._information_panel.setVisible(visible)
        self._information_btn.setAccessibleName(
            "隐藏照片信息" if visible else "显示照片信息"
        )

    def refresh_current(self) -> None:
        self._sync_filter_controls()
        self._summary.hide()
        self._body.show()
        self._action_bar.show()
        self._wake_action_bar()
        session = self._session
        if session is None:
            self._filmstrip.set_photo_ids([])
            self._clear_current("没有可筛选的照片")
            return
        visible_ids = session.visible_ids()
        self._filmstrip.set_photo_ids(visible_ids)
        photo_id = session.current_id
        if photo_id is None:
            self._filename_label.clear()
            self._progress_label.setText(f"0 / {len(visible_ids)}")
            self._clear_current("没有符合条件的照片")
            return
        model = self._validated_current_model(photo_id)
        if model is None:
            self._clear_current("无法加载图片")
            return
        self._current_photo_id = photo_id
        self._filmstrip.select_photo_id(photo_id)
        self._filename_label.setText(model.filename)
        self._filename_label.setToolTip(model.filename)
        self._progress_label.setText(
            f"{visible_ids.index(photo_id) + 1} / {len(visible_ids)}"
        )
        self._show_information(model)
        self._request_preview(model)

    def show_summary(self, counts: Mapping[str, int]) -> None:
        self.invalidate_preview_requests()
        self._filename_label.setText("筛选完成")
        self._filename_label.setToolTip("")
        self._progress_label.clear()
        self._current_photo_id = None
        self._filmstrip.select_photo_id(None)
        self._body.hide()
        self._action_bar.hide()
        self._action_idle_timer.stop()
        self._information_btn.setChecked(False)
        self._summary_label.setText(
            "  ·  ".join(
                f"{label} {int(counts.get(label, 0))}"
                for label in ("保留", "待定", "舍弃")
            )
        )
        self._summary.show()

    @staticmethod
    def _analysis_value(model: ImageFileModel, conclusion: str) -> str:
        status = model.technical_status
        if status is AnalysisStatus.SUCCEEDED:
            return conclusion or "—"
        return {
            AnalysisStatus.PENDING: "未分析",
            AnalysisStatus.RUNNING: "分析中",
            AnalysisStatus.FAILED: "分析未完成",
            AnalysisStatus.CANCELED: "分析未完成",
        }.get(status, "未分析")

    def _show_information(self, model: ImageFileModel) -> None:
        exif_parts = tuple(
            str(value)
            for value in (
                model.camera_model,
                model.lens_model,
                model.aperture,
                model.shutter_speed,
                model.iso,
                model.focal_length,
                model.date_taken,
            )
            if value
        )
        if model.exif_error:
            exif = "EXIF 读取失败"
        elif exif_parts:
            exif = " · ".join(exif_parts)
        else:
            exif = "未读取" if not model.exif_loaded else "—"
        portrait = "不判断"
        if (
            model.technical_status is AnalysisStatus.SUCCEEDED
            and model.portrait_assessed is True
        ):
            portrait = (
                "检测到人像" if model.face_count > 0 else "未检测到人像"
            )
        values = {
            "filename": model.filename,
            "exif": exif,
            "focus": self._analysis_value(model, model.focus_conclusion),
            "exposure": self._analysis_value(
                model, model.exposure_conclusion
            ),
            "portrait": portrait,
        }
        for key, value in values.items():
            self._information_values[key].setText(value or "—")

    def _request_mark(self, mark: PhotoMark) -> None:
        if self._move_pending:
            return
        photo_id = self._current_photo_id
        if photo_id is None:
            return
        if self._validated_current_model(photo_id) is None:
            self._clear_current("无法加载图片")
            return
        self.mark_requested.emit(photo_id, mark)

    def _request_activated_photo(self, photo_id: str) -> None:
        if self._move_pending:
            return
        session = self._session
        if session is None:
            return
        visible_ids = session.visible_ids()
        current_id = session.current_id
        if current_id not in visible_ids or photo_id not in visible_ids:
            return
        offset = visible_ids.index(photo_id) - visible_ids.index(current_id)
        if offset:
            self.navigate_requested.emit(offset)

    def _request_preview(self, model: ImageFileModel) -> None:
        self._preview_loader.request(
            model.photo_id,
            model.path,
            self._preview_max_edge,
        )
        self._loaded_photo_id = None
        self._image_view.clear_image("正在加载…")

    def _on_preview_decoded(
        self,
        photo_id: str,
        image: QImage,
    ) -> None:
        if not self._is_current_preview_result(photo_id):
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._on_preview_failed(photo_id, "无法创建预览图")
            return
        model = self._model_for(photo_id)
        if model is None:
            return
        self._image_view.set_pixmap(pixmap, model.rotation)
        self._loaded_photo_id = photo_id

    def _on_preview_failed(
        self,
        photo_id: str,
        message: str,
    ) -> None:
        if not self._is_current_preview_result(photo_id):
            return
        logger.warning("Culling preview failed for %s: %s", photo_id, message)
        self._loaded_photo_id = None
        self._image_view.clear_image("无法加载图片\n可继续筛选其他照片")

    def _is_current_preview_result(self, photo_id: str) -> bool:
        return (
            photo_id == self._current_photo_id
            and self._filmstrip.row_for_id(photo_id) >= 0
            and self._model_for(photo_id) is not None
        )

    def _model_for(self, photo_id: str) -> ImageFileModel | None:
        return self._filmstrip.model_for_id(photo_id)

    def _validated_current_model(
        self,
        photo_id: str,
    ) -> ImageFileModel | None:
        if self._filmstrip.row_for_id(photo_id) < 0:
            return None
        return self._model_for(photo_id)

    def _clear_current(self, message: str) -> None:
        self._current_photo_id = None
        self._filmstrip.select_photo_id(None)
        self._filename_label.clear()
        self._filename_label.setToolTip("")
        self._progress_label.setText("0 / 0")
        for label in self._information_values.values():
            label.setText("—")
        self.invalidate_preview_requests()
        self._image_view.clear_image(message)

    def _wake_action_bar(self) -> None:
        self._action_opacity.setOpacity(1.0)
        if self._action_bar.isVisible():
            self._action_idle_timer.start()

    def _fade_action_bar(self) -> None:
        if self._action_bar.isVisible():
            self._action_opacity.setOpacity(0.55)

    def eventFilter(self, watched: object, event: QEvent | None) -> bool:
        if event is not None and event.type() in {
            QEvent.Type.KeyPress,
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.Wheel,
        }:
            self._wake_action_bar()
        return super().eventFilter(watched, event)

    @property
    def loaded_photo_id(self) -> str | None:
        return self._loaded_photo_id

    @property
    def current_photo_id(self) -> str | None:
        return self._current_photo_id

    @property
    def move_pending(self) -> bool:
        return self._move_pending

    def set_move_pending(self, pending: bool) -> None:
        if not isinstance(pending, bool):
            raise TypeError("pending must be a bool")
        self._move_pending = pending
        self._apply_control_state()

    def set_undo_available(self, available: bool) -> None:
        if not isinstance(available, bool):
            raise TypeError("available must be a bool")
        self._undo_available = available
        self._apply_control_state()

    def _apply_control_state(self) -> None:
        safe = not self._move_pending
        self._back_btn.setEnabled(safe)
        self._previous_btn.setEnabled(safe)
        self._next_btn.setEnabled(safe)
        self._filmstrip.setEnabled(safe)
        self._status_filter.setEnabled(safe)
        self._analysis_filter.setEnabled(safe)
        for button in self._mark_buttons:
            button.setEnabled(safe)
        self._review_hold_btn.setEnabled(safe)
        self._review_discard_btn.setEnabled(safe)
        self._open_directory_btn.setEnabled(safe)
        self._summary_back_btn.setEnabled(safe)
        self._undo_btn.setEnabled(safe and self._undo_available)

    @property
    def preview_pending_count(self) -> int:
        return self._preview_loader.pending_count

    @property
    def preview_unfinished_count(self) -> int:
        return self._preview_loader.unfinished_count

    def invalidate_preview_requests(self) -> None:
        self._preview_loader.invalidate()
        self._loaded_photo_id = None

    def shutdown_preview_loader(self) -> None:
        self._preview_loader.close()
        self._current_photo_id = None
        self._loaded_photo_id = None

    def closeEvent(self, event: object) -> None:
        self.shutdown_preview_loader()
        super().closeEvent(event)
