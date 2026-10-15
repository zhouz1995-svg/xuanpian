from __future__ import annotations

import logging
from collections.abc import Callable

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QScrollArea,
    QStyle,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.ui.async_preview_loader import (
    DEFAULT_PREVIEW_MAX_EDGE as PREVIEW_MAX_EDGE,
    BoundedPreviewLoader,
    PreviewDecoder,
    decode_preview_image,
    preview_decode_reaper_count,
)
from src.xuanpian.ui.edge_tab import EdgeTab
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.image_view import ImageView
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles
from src.xuanpian.ui.theme import (
    ACCENT,
    BORDER,
    DANGER,
    ELEVATED,
    MUTED,
    SUCCESS,
    SURFACE,
    TEXT,
    WARNING,
)

logger = logging.getLogger(__name__)
_PREVIEW_PANEL_DEFAULT = 300
_PREVIEW_PANEL_MIN = 260
_PREVIEW_PANEL_MAX = 360
_PREVIEW_PANEL_COLLAPSED = 24
_PREVIEW_TAB_INSET = _PREVIEW_PANEL_COLLAPSED + 4


class _ElidedLabel(QLabel):
    """QLabel that elides long text with an ellipsis when space is tight."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._full_text = text
        self._refresh()

    def setText(self, text: str) -> None:
        self._full_text = text
        self._refresh()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        fm = QFontMetrics(self.font())
        super().setText(
            fm.elidedText(
                self._full_text,
                Qt.TextElideMode.ElideMiddle,
                max(0, self.width()),
            )
        )

FILMSTRIP_HEIGHT = 80
FILMSTRIP_THUMB_WIDTH = 100


class FilmstripDelegate(QStyledItemDelegate):
    """Paint a filmstrip cell directly from stable-ID model roles."""

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QSize:
        return QSize(FILMSTRIP_THUMB_WIDTH + 4, FILMSTRIP_HEIGHT + 4)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        painter.save()
        bounds = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        background = ELEVATED if selected else SURFACE
        painter.fillRect(bounds, background)
        thumbnail = index.data(int(PhotoRoles.THUMBNAIL))
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            scaled = thumbnail.scaled(
                bounds.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            painter.drawPixmap(
                bounds.x() + (bounds.width() - scaled.width()) // 2,
                bounds.y() + (bounds.height() - scaled.height()) // 2,
                scaled,
            )
        mark = str(index.data(int(PhotoRoles.MARK)) or "")
        mark_color = {
            PhotoMark.KEEP: SUCCESS,
            PhotoMark.HOLD: WARNING,
            PhotoMark.DISCARD: DANGER,
        }.get(normalize_legacy_mark(mark))
        if mark_color is not None:
            painter.fillRect(
                QRect(bounds.left(), bounds.top(), 4, bounds.height()),
                mark_color,
            )
        if selected:
            painter.setPen(QPen(ACCENT, 2))
            painter.drawRect(bounds.adjusted(1, 1, -1, -1))
        painter.restore()


class _CompareDelegate(QStyledItemDelegate):
    """Paint one similar-photo card with its technical conclusions."""

    THUMB_HEIGHT = 118

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QSize:
        return QSize(200, self.THUMB_HEIGHT + 42)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = option.rect.adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setBrush(ELEVATED if selected else SURFACE)
        painter.setPen(QPen(ACCENT if selected else BORDER, 1))
        painter.drawRoundedRect(bounds, 5, 5)

        image_rect = QRect(
            bounds.left(),
            bounds.top(),
            bounds.width(),
            self.THUMB_HEIGHT,
        )
        thumbnail = index.data(int(PhotoRoles.THUMBNAIL))
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            scaled = thumbnail.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                image_rect.x()
                + (image_rect.width() - scaled.width()) // 2,
                image_rect.y()
                + (image_rect.height() - scaled.height()) // 2,
                scaled,
            )
        else:
            painter.fillRect(image_rect, QColor(20, 24, 30))

        mark = str(index.data(int(PhotoRoles.MARK)) or "")
        mark_color = {
            PhotoMark.KEEP: SUCCESS,
            PhotoMark.HOLD: WARNING,
            PhotoMark.DISCARD: DANGER,
        }.get(normalize_legacy_mark(mark))
        if mark_color is not None:
            painter.fillRect(
                QRect(image_rect.left(), image_rect.top(), 4, image_rect.height()),
                mark_color,
            )

        name_font = QFont(painter.font())
        name_font.setPointSize(9)
        painter.setFont(name_font)
        painter.setPen(TEXT)
        filename = str(index.data(int(PhotoRoles.FILENAME)) or "")
        name = QFontMetrics(name_font).elidedText(
            filename,
            Qt.TextElideMode.ElideMiddle,
            max(0, bounds.width() - 12),
        )
        painter.drawText(
            QRect(bounds.left() + 6, bounds.top() + self.THUMB_HEIGHT + 3, bounds.width() - 12, 18),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            name,
        )

        analysis = index.data(int(PhotoRoles.ANALYSIS))
        status = (
            getattr(analysis, "technical_status", AnalysisStatus.PENDING)
            if analysis is not None
            else AnalysisStatus.PENDING
        )
        if status is AnalysisStatus.SUCCEEDED:
            focus = str(getattr(analysis, "focus_conclusion", "") or "—")
            exposure = str(getattr(analysis, "exposure_conclusion", "") or "—")
            meta = f"{focus} · {exposure}"
        else:
            meta = "未分析"
        meta_font = QFont(name_font)
        meta_font.setPointSize(8)
        painter.setFont(meta_font)
        painter.setPen(MUTED)
        meta_text = QFontMetrics(meta_font).elidedText(
            meta,
            Qt.TextElideMode.ElideRight,
            max(0, bounds.width() - 12),
        )
        painter.drawText(
            QRect(
                bounds.left() + 6,
                bounds.bottom() - 16,
                bounds.width() - 12,
                18,
            ),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            meta_text,
        )

        if selected:
            painter.setPen(QPen(ACCENT, 2))
            painter.drawRect(bounds.adjusted(1, 1, -1, -1))
        painter.restore()


class _CompareStrip(QWidget):
    """Horizontal cards of the current similar-photo group."""

    photo_activated = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("compareStrip")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._list = QListView(self)
        self._list.setObjectName("compareStripList")
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setFlow(QListView.Flow.LeftToRight)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setWrapping(False)
        self._list.setUniformItemSizes(True)
        self._list.setItemDelegate(_CompareDelegate(self._list))
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self._list.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._list.setFixedHeight(170)
        self._list.clicked.connect(self._on_clicked)
        layout.addWidget(self._list)

    def set_model(self, model: PhotoListModel) -> None:
        self._list.setModel(model)

    def set_current(self, photo_id: str) -> None:
        model = self._list.model()
        if model is None:
            return
        row = model.row_for_id(photo_id)
        if row < 0:
            return
        index = model.index(row, 0)
        self._list.setCurrentIndex(index)
        self._list.scrollTo(index, QListView.ScrollHint.PositionAtCenter)

    def _on_clicked(self, index: QModelIndex) -> None:
        photo_id = index.data(int(PhotoRoles.ID))
        if isinstance(photo_id, str):
            self.photo_activated.emit(photo_id)


class _InfoPanel(QWidget):
    """Right-hand preview info panel; collapses to an edge tab.

    Width is owned by the panel itself: minimum and maximum are animated
    together so the layout always sees a pinned width. The previous
    QSplitter was deliberately dropped because it hides children it treats
    as "collapsed", which left the panel unable to re-expand.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewPanel")
        self._collapsed = False
        self._expanded_width = _PREVIEW_PANEL_DEFAULT
        self._content_widgets: list[QWidget] = []
        self._layout = QVBoxLayout(self)
        # Reserve a left rail for the edge tab so it never covers content.
        self._layout.setContentsMargins(_PREVIEW_TAB_INSET, 0, 0, 0)
        self._tab = EdgeTab("信息", self, rounded_side="right")
        self._tab.setObjectName("previewEdgeTab")
        self._tab.setToolTip("收起详细信息")
        self._tab.clicked.connect(self.toggle)
        self._min_animation = QPropertyAnimation(self, b"minimumWidth", self)
        self._max_animation = QPropertyAnimation(self, b"maximumWidth", self)
        for animation in (self._min_animation, self._max_animation):
            animation.setDuration(180)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._max_animation.finished.connect(self._on_animation_finished)
        self.set_width(_PREVIEW_PANEL_DEFAULT)
        self._position_tab()

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    def add_content(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.addWidget(widget, stretch)
        self._content_widgets.append(widget)

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._position_tab()

    def _position_tab(self) -> None:
        self._tab.move(
            1,
            max(4, (self.height() - self._tab.height()) // 2),
        )
        self._tab.raise_()

    @staticmethod
    def _clamp(width: int) -> int:
        return max(
            _PREVIEW_PANEL_COLLAPSED,
            min(width, _PREVIEW_PANEL_MAX),
        )

    def set_width(self, width: int) -> None:
        """Apply a width directly (drag handle).

        Dragging the handle past the rail width collapses the panel;
        dragging it out again while collapsed expands it.
        """
        if width <= _PREVIEW_PANEL_COLLAPSED:
            if not self._collapsed:
                self.toggle()
            return
        self._min_animation.stop()
        self._max_animation.stop()
        if self._collapsed:
            self._collapsed = False
            self._tab.setToolTip("收起详细信息")
            for widget in self._content_widgets:
                widget.show()
        self.setMinimumWidth(self._clamp(width))
        self.setMaximumWidth(self._clamp(width))

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._expanded_width = max(self.width(), _PREVIEW_PANEL_MIN)
            for widget in self._content_widgets:
                widget.hide()
            self._tab.setToolTip("展开详细信息")
            self._start_width_animation(
                self.width(), _PREVIEW_PANEL_COLLAPSED
            )
        else:
            for widget in self._content_widgets:
                widget.show()
            self._tab.setToolTip("收起详细信息")
            self._start_width_animation(
                self.width(),
                max(self._expanded_width, _PREVIEW_PANEL_MIN),
            )

    def _start_width_animation(self, start: int, end: int) -> None:
        for animation in (self._min_animation, self._max_animation):
            animation.stop()
            animation.setStartValue(start)
            animation.setEndValue(end)
            animation.start()

    def _on_animation_finished(self) -> None:
        self._position_tab()


class _PanelTabHeader(QWidget):
    """Segmented header for the preview info pages.

    Replaces the built-in QTabBar, whose labels could be compressed or
    elided by the panel geometry; plain checkable buttons always render
    their full label text.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewTabHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 6)
        layout.setSpacing(4)
        self.buttons: list[QPushButton] = []
        for key, label in (
            ("analysis", "技术结论"),
            ("exif", "拍摄信息"),
        ):
            button = QPushButton(label, self)
            button.setProperty("previewTabButton", True)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            layout.addWidget(button, 1)
            setattr(self, f"{key}_button", button)
            self.buttons.append(button)


class _PanelHandle(QFrame):
    """Draggable divider between the image view and the info panel."""

    panel_width_changed = pyqtSignal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewPanelHandle")
        self.setFixedWidth(5)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self._width_provider: Callable[[], int] = lambda: 0
        self._drag_start_x = 0.0
        self._drag_start_width = 0
        self._dragging = False

    def set_width_provider(self, provider: Callable[[], int]) -> None:
        self._width_provider = provider

    def mousePressEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start_x = event.globalPosition().x()
            self._drag_start_width = self._width_provider()
            event.accept()

    def mouseMoveEvent(self, event: object) -> None:
        if self._dragging and event.buttons() & Qt.MouseButton.LeftButton:
            delta = self._drag_start_x - event.globalPosition().x()
            self.panel_width_changed.emit(
                int(self._drag_start_width + delta)
            )
            event.accept()

    def mouseReleaseEvent(self, event: object) -> None:
        self._dragging = False
        event.accept()


def _analysis_state_text(status: AnalysisStatus) -> str:
    return {
        AnalysisStatus.PENDING: "未分析",
        AnalysisStatus.RUNNING: "分析中",
        AnalysisStatus.FAILED: "分析失败",
        AnalysisStatus.CANCELED: "已取消",
    }.get(status, "未分析")


def _analysis_value(status: AnalysisStatus, success_text: str) -> str:
    return success_text if status is AnalysisStatus.SUCCEEDED else _analysis_state_text(status)


class PreviewWidget(QWidget):
    """Darkroom preview with view-only transforms and metadata panels."""

    preview_state_changed = pyqtSignal(str, str, int)
    preview_loaded = pyqtSignal(str, int)
    preview_error = pyqtSignal(str, int, str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        preview_decoder: PreviewDecoder = decode_preview_image,
        preview_max_edge: int = PREVIEW_MAX_EDGE,
    ) -> None:
        super().__init__(parent)
        if preview_max_edge <= 0:
            raise ValueError("preview_max_edge must be positive")
        self._current_photo_id: str | None = None
        self._preview_max_edge = preview_max_edge
        self._preview_token = 0
        self._preview_state = "idle"
        self._loaded_photo_id: str | None = None
        self._loaded_token: int | None = None
        self._preview_loader = BoundedPreviewLoader(
            self,
            decoder=preview_decoder,
        )
        self._preview_loader.decoded.connect(self._on_preview_decoded)
        self._preview_loader.error.connect(self._on_preview_failed)
        self._model_provider: Callable[
            [str], ImageFileModel | None
        ] = lambda _photo_id: None
        self._burst_group_provider: Callable[
            [str], tuple[str, ...] | None
        ] | None = None
        self._filmstrip_model = PhotoListModel(self._model_provider, self)
        self._bound_filmstrip_model: PhotoListModel | None = None
        self._row_before_reset = -1
        self._removed_current_row = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_top_bar())

        self._body = QWidget(self)
        body_layout = QHBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        self._image_view = ImageView(self._body)
        self._image_view.setMinimumSize(400, 300)
        self._build_tool_overlay()
        self._image_view.viewport().installEventFilter(self)
        body_layout.addWidget(self._image_view, 1)
        self._panel_handle = _PanelHandle(self._body)
        self._panel_handle.set_width_provider(lambda: self._panel.width())
        self._panel_handle.panel_width_changed.connect(
            self._on_panel_width_changed
        )
        body_layout.addWidget(self._panel_handle)
        self._panel = self._build_panel(self._body)
        body_layout.addWidget(self._panel)
        root.addWidget(self._body, 1)

        self._compare_strip = _CompareStrip(self)
        self._compare_strip.photo_activated.connect(self._show_photo_id)
        self._compare_strip.hide()
        root.addWidget(self._compare_strip)

        self._filmstrip = QListView(self)
        self._filmstrip.setViewMode(QListView.ViewMode.IconMode)
        self._filmstrip.setFlow(QListView.Flow.LeftToRight)
        self._filmstrip.setMovement(QListView.Movement.Static)
        self._filmstrip.setWrapping(False)
        self._filmstrip.setUniformItemSizes(True)
        self._filmstrip.setItemDelegate(FilmstripDelegate(self._filmstrip))
        self._bind_filmstrip_model(self._filmstrip_model)
        self._filmstrip.setFixedHeight(FILMSTRIP_HEIGHT + 30)
        self._filmstrip.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        )
        self._filmstrip.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._filmstrip.clicked.connect(self._on_filmstrip_clicked)
        root.addWidget(self._filmstrip)

    def _build_top_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("previewTopBar")
        bar.setFixedHeight(44)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(10, 4, 10, 4)
        self._back_btn = QPushButton("← 返回", bar)
        self._back_btn.clicked.connect(self._on_back)
        layout.addWidget(self._back_btn)
        self._top_filename = _ElidedLabel("", bar)
        layout.addWidget(self._top_filename, 1)
        self._top_position = QLabel(bar)
        layout.addWidget(self._top_position)
        self._compare_btn = QPushButton("对比本组", bar)
        self._compare_btn.setCheckable(True)
        self._compare_btn.setProperty("previewCompareButton", True)
        self._compare_btn.clicked.connect(self._toggle_compare_strip)
        self._compare_btn.hide()
        layout.addWidget(self._compare_btn)
        return bar

    def _toggle_compare_strip(self, checked: bool) -> None:
        self._compare_strip.setVisible(checked)

    def _build_tool_overlay(self) -> None:
        self._tool_overlay = QWidget(self._image_view.viewport())
        self._tool_overlay.setObjectName("previewToolOverlay")
        layout = QHBoxLayout(self._tool_overlay)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)
        definitions = (
            ("zoom_out", "−", "缩小", self._image_view_zoom_out, 36),
            ("fit", "适合", "适合窗口", self._image_view_fit, 46),
            ("actual", "1:1", "原始尺寸", self._image_view_100, 42),
            ("zoom_in", "+", "放大", self._image_view_zoom_in, 36),
            ("rotate_left", "↶", "逆时针旋转 90°", lambda: self._rotate(-90), 36),
            ("rotate_right", "↷", "顺时针旋转 90°", lambda: self._rotate(90), 36),
        )
        self._tool_buttons: dict[str, QPushButton] = {}
        for key, text, tooltip, slot, width in definitions:
            button = QPushButton(text, self._tool_overlay)
            button.setProperty("previewToolButton", True)
            button.setToolTip(tooltip)
            button.setFixedSize(width, 30)
            button.clicked.connect(slot)
            layout.addWidget(button)
            self._tool_buttons[key] = button
        self._tool_overlay.adjustSize()
        self._tool_overlay.hide()

    def _position_tool_overlay(self) -> None:
        viewport = self._image_view.viewport()
        overlay = self._tool_overlay
        overlay.adjustSize()
        x = max(12, (viewport.width() - overlay.width()) // 2)
        y = max(12, viewport.height() - overlay.height() - 16)
        overlay.move(x, y)
        overlay.raise_()

    def eventFilter(self, watched: object, event: QEvent | None) -> bool:
        image_view = getattr(self, "_image_view", None)
        if (
            image_view is not None
            and watched is image_view.viewport()
            and event is not None
            and event.type() == QEvent.Type.Resize
        ):
            self._position_tool_overlay()
        return super().eventFilter(watched, event)

    def _build_panel(self, parent: QWidget) -> QWidget:
        panel = _InfoPanel(parent)
        self._tab_header = _PanelTabHeader(panel)
        self._tab_header.analysis_button.clicked.connect(
            lambda checked=False: self._switch_info_page(0)
        )
        self._tab_header.exif_button.clicked.connect(
            lambda checked=False: self._switch_info_page(1)
        )
        panel.add_content(self._tab_header)

        self._stack = QStackedWidget(panel)
        self._stack.addWidget(self._build_analysis_tab())
        self._stack.addWidget(self._build_exif_tab())
        panel.add_content(self._stack, 1)

        mark_panel = QWidget(panel)
        mark_panel.setObjectName("previewMarkPanel")
        mark_layout = QVBoxLayout(mark_panel)
        mark_layout.setContentsMargins(12, 10, 12, 12)
        mark_layout.setSpacing(8)
        self._mark_label = QLabel("标记: 未标记", mark_panel)
        self._mark_label.setProperty("class", "markStatus")
        mark_layout.addWidget(self._mark_label)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self._mark_buttons: dict[PhotoMark, QPushButton] = {}
        for mark, style_key, shortcut in (
            (PhotoMark.KEEP, "keep", "1"),
            (PhotoMark.HOLD, "hold", "2"),
            (PhotoMark.DISCARD, "discard", "3"),
        ):
            button = QPushButton(mark.value, mark_panel)
            button.setCheckable(True)
            button.setEnabled(False)
            button.setProperty("previewMark", style_key)
            button.setAccessibleName(
                f"{mark.value}照片（快捷键 {shortcut}）"
            )
            button.setToolTip(
                f"{mark.value} · 快捷键 {shortcut} · "
                "仅记录项目标记，不移动原文件"
            )
            button.clicked.connect(
                lambda checked=False, value=mark.value: self._mark_current(
                    value
                )
            )
            buttons.addWidget(button, 1)
            self._mark_buttons[mark] = button
        mark_layout.addLayout(buttons)
        panel.add_content(mark_panel)
        self._sync_info_header()
        return panel

    def _switch_info_page(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._sync_info_header()

    def _sync_info_header(self) -> None:
        current = self._stack.currentIndex()
        self._tab_header.analysis_button.setChecked(current == 0)
        self._tab_header.exif_button.setChecked(current == 1)

    def _new_info_scroll(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        content.setObjectName("previewInfoContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 16)
        layout.setSpacing(10)
        scroll.setWidget(content)
        return scroll, layout

    @staticmethod
    def _add_section_heading(layout: QVBoxLayout, text: str) -> None:
        heading = QLabel(text)
        heading.setProperty("infoSection", True)
        heading.setStyleSheet(
            "color:#7F8996;font-size:10px;font-weight:600;"
            "letter-spacing:0.8px;padding-top:4px;"
        )
        layout.addWidget(heading)

    @staticmethod
    def _add_info_block(
        layout: QVBoxLayout, title: str, initial: str = "—"
    ) -> QLabel:
        block = QWidget()
        block.setProperty("infoBlock", True)
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 2)
        block_layout.setSpacing(3)
        title_label = QLabel(title, block)
        title_label.setProperty("infoTitle", True)
        title_label.setStyleSheet("color:#7F8996;font-size:10px;")
        value_label = QLabel(initial, block)
        value_label.setProperty("infoValue", True)
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        value_label.setStyleSheet(
            "color:#D1D6DC;font-size:12px;line-height:1.35;"
        )
        block_layout.addWidget(title_label)
        block_layout.addWidget(value_label)
        layout.addWidget(block)
        return value_label

    def _build_analysis_tab(self) -> QWidget:
        self._analysis_scroll, layout = self._new_info_scroll()
        self._add_section_heading(layout, "照片")
        self._filename_value = self._add_info_block(layout, "文件名")
        self._add_section_heading(layout, "分析结果")
        self._blur_value = self._add_info_block(layout, "清晰度", "未分析")
        self._exposure_value = self._add_info_block(layout, "曝光", "未分析")
        self._portrait_value = self._add_info_block(layout, "人像", "不判断")
        layout.addStretch()
        return self._analysis_scroll

    def _build_exif_tab(self) -> QWidget:
        self._exif_scroll, layout = self._new_info_scroll()
        self._exif_status = self._add_info_block(layout, "读取状态")
        self._exif_values: dict[str, QLabel] = {}
        for section, fields in (
            (
                "设备",
                (
                    ("camera_make", "厂商"),
                    ("camera_model", "相机"),
                    ("lens_model", "镜头"),
                ),
            ),
            (
                "拍摄参数",
                (
                    ("aperture", "光圈"),
                    ("shutter_speed", "快门"),
                    ("iso", "ISO"),
                    ("focal_length", "焦距"),
                    ("flash", "闪光灯"),
                ),
            ),
            (
                "文件信息",
                (("date_taken", "拍摄时间"), ("dimensions", "尺寸")),
            ),
        ):
            self._add_section_heading(layout, section)
            for key, title in fields:
                self._exif_values[key] = self._add_info_block(layout, title)
        layout.addStretch()
        return self._exif_scroll

    def load(
        self,
        model: ImageFileModel,
        index: int,
        all_models: list[ImageFileModel],
    ) -> None:
        """Compatibility adapter for callers that still have an immediate row."""
        models_by_id = {
            item.photo_id: item for item in all_models if item.photo_id
        }
        photo_ids = list(models_by_id)
        if not 0 <= index < len(photo_ids):
            return
        self.load_photo(
            model.photo_id,
            lambda: list(photo_ids),
            models_by_id.get,
        )

    def load_photo(
        self,
        photo_id: str,
        photo_ids_provider: Callable[[], list[str]],
        model_provider: Callable[[str], ImageFileModel | None],
        *,
        photo_list_model: PhotoListModel | None = None,
        burst_group_provider: Callable[
            [str], tuple[str, ...] | None
        ] | None = None,
    ) -> None:
        if model_provider(photo_id) is None:
            return
        self._model_provider = model_provider
        self._burst_group_provider = burst_group_provider
        if photo_list_model is None:
            photo_ids = list(photo_ids_provider())
            if photo_id not in photo_ids:
                return
            self._filmstrip_model = PhotoListModel(model_provider, self)
            self._filmstrip_model.set_photo_ids(photo_ids)
        else:
            if photo_list_model.row_for_id(photo_id) < 0:
                return
            self._filmstrip_model = photo_list_model
        self._bind_filmstrip_model(self._filmstrip_model)
        self._current_photo_id = photo_id
        self._render_photo(photo_id)

    def _render_photo(self, photo_id: str) -> None:
        model = self._model_provider(photo_id)
        if model is None:
            return
        self._show_image(model)
        self._show_analysis(model)
        self._show_exif(model)
        self._sync_filmstrip_selection(photo_id)
        self._update_compare_state()
        self._update_position_label()

    def _update_compare_state(self) -> None:
        """Show the similar-photo strip only for grouped photos."""
        photo_id = self._current_photo_id
        members: tuple[str, ...] | None = None
        provider = self._burst_group_provider
        if photo_id is not None and provider is not None:
            model = self._model_provider(photo_id)
            if model is not None and model.burst_group_size > 1:
                members = provider(photo_id)
        if not members:
            self._compare_btn.setChecked(False)
            self._compare_btn.hide()
            self._compare_strip.hide()
            return
        model = PhotoListModel(self._model_provider, self)
        model.set_photo_ids(list(members))
        self._compare_strip.set_model(model)
        self._compare_strip.set_current(photo_id)
        self._compare_btn.show()
        self._compare_strip.setVisible(self._compare_btn.isChecked())

    def _update_position_label(self) -> None:
        photo_id = self._current_photo_id
        index = (
            self._filmstrip_model.row_for_id(photo_id)
            if photo_id is not None
            else -1
        )
        self._top_position.setText(
            f"{index + 1} / {self._filmstrip_model.rowCount()}"
            if index >= 0
            else "0 / 0"
        )

    def _on_panel_width_changed(self, width: int) -> None:
        self._panel.set_width(width)

    def _on_back(self) -> None:
        main_window = self._find_main_window()
        if main_window is not None:
            main_window.exit_preview()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is not None:
            if event.key() == Qt.Key.Key_Escape:
                self._on_back()
                return
            if event.key() == Qt.Key.Key_Right:
                self.navigate(1)
                return
            if event.key() == Qt.Key.Key_Left:
                self.navigate(-1)
                return
            if event.key() == Qt.Key.Key_Delete:
                photo_id = self.current_photo_id()
                main_window = self._find_main_window()
                if photo_id is not None and main_window is not None:
                    main_window.delete_photo(photo_id)
                return
        super().keyPressEvent(event)

    def _find_main_window(self) -> object | None:
        owner = self.window()
        if owner.__class__.__name__ == "MainWindow":
            return owner
        for widget in QApplication.topLevelWidgets():
            if widget.__class__.__name__ == "MainWindow":
                return widget
        return None

    def _show_image(self, model: ImageFileModel) -> None:
        if self._preview_loader.is_closed:
            return
        self._preview_token = self._preview_loader.request(
            model.photo_id,
            model.path,
            self._preview_max_edge,
        )
        self._loaded_photo_id = None
        self._loaded_token = None
        self._set_preview_state("loading", model.photo_id, self._preview_token)
        self._sync_mark_controls(model.mark, enabled=False)
        self._image_view.clear_image("正在加载…")
        self._tool_overlay.hide()

    def _set_preview_state(
        self,
        state: str,
        photo_id: str,
        token: int,
    ) -> None:
        self._preview_state = state
        self.preview_state_changed.emit(state, photo_id, token)

    def _on_preview_decoded(
        self,
        photo_id: str,
        image: QImage,
    ) -> None:
        if not self._is_current_preview_result(photo_id):
            return
        token = self._preview_token
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            self._on_preview_failed(photo_id, "无法创建预览图")
            return
        model = self._model_provider(photo_id)
        if model is None:
            return
        self._image_view.set_pixmap(pixmap, model.rotation)
        self._loaded_photo_id = photo_id
        self._loaded_token = token
        self._set_preview_state("loaded", photo_id, token)
        self._sync_mark_controls(model.mark, enabled=True)
        self._tool_overlay.show()
        self._position_tool_overlay()
        self.preview_loaded.emit(photo_id, token)

    def _on_preview_failed(
        self,
        photo_id: str,
        message: str,
    ) -> None:
        if not self._is_current_preview_result(photo_id):
            return
        token = self._preview_token
        logger.warning("Preview load failed for %s: %s", photo_id, message)
        self._image_view.clear_image("无法加载图片\n可继续浏览其他照片")
        self._loaded_photo_id = None
        self._loaded_token = None
        self._set_preview_state("error", photo_id, token)
        model = self._model_provider(photo_id)
        self._sync_mark_controls(
            model.mark if model is not None else "",
            enabled=False,
        )
        self._tool_overlay.hide()
        self.preview_error.emit(photo_id, token, message)

    def _is_current_preview_result(self, photo_id: str) -> bool:
        return (
            not self._preview_loader.is_closed
            and photo_id == self._current_photo_id
            and self._model_provider(photo_id) is not None
            and self._filmstrip_model.row_for_id(photo_id) >= 0
        )

    @property
    def preview_state(self) -> str:
        return self._preview_state

    @property
    def loaded_photo_id(self) -> str | None:
        return self._loaded_photo_id

    @property
    def loaded_token(self) -> int | None:
        return self._loaded_token

    @property
    def preview_pending_count(self) -> int:
        return self._preview_loader.pending_count

    @property
    def preview_unfinished_count(self) -> int:
        return self._preview_loader.unfinished_count

    def invalidate_preview_requests(self) -> None:
        self._preview_loader.invalidate()
        self._loaded_photo_id = None
        self._loaded_token = None
        self._preview_state = "idle"

    def shutdown_preview_loader(self) -> None:
        if self._preview_loader.is_closed:
            return
        self._preview_loader.close()
        self._loaded_photo_id = None
        self._loaded_token = None
        self._preview_state = "idle"

    def closeEvent(self, event: object) -> None:
        self.shutdown_preview_loader()
        super().closeEvent(event)

    def _show_analysis(self, model: ImageFileModel) -> None:
        self._filename_value.setText(model.filename)
        self._top_filename.setText(model.filename)
        status = model.technical_status
        self._blur_value.setText(_analysis_value(status, model.focus_conclusion or "—"))
        self._exposure_value.setText(_analysis_value(status, model.exposure_conclusion or "—"))
        portrait = "不判断"
        if (
            status is AnalysisStatus.SUCCEEDED
            and model.portrait_assessed
        ):
            portrait = (
                "检测到人像" if model.face_count > 0 else "未检测到人像"
            )
        self._portrait_value.setText(portrait)
        self._sync_mark_controls(
            model.mark,
            enabled=self._current_preview_is_loaded(model.photo_id),
        )

    def _current_preview_is_loaded(self, photo_id: str) -> bool:
        return (
            self._current_photo_id == photo_id
            and self._loaded_photo_id == photo_id
            and self._preview_state == "loaded"
        )

    def _sync_mark_controls(self, value: str, *, enabled: bool) -> None:
        normalized = normalize_legacy_mark(value)
        label = normalized.value if normalized else "历史不可用"
        self._mark_label.setText(f"标记: {label}")
        selected = (
            normalized
            if normalized
            in (PhotoMark.KEEP, PhotoMark.HOLD, PhotoMark.DISCARD)
            else None
        )
        for mark, button in self._mark_buttons.items():
            button.setEnabled(enabled)
            button.setChecked(mark is selected)

    def _show_exif(self, model: ImageFileModel) -> None:
        self._exif_status.setText(
            "EXIF 读取失败"
            if model.exif_error
            else ("已读取" if model.exif_loaded else "未读取")
        )
        values: dict[str, object] = {
            "camera_make": model.camera_make,
            "camera_model": model.camera_model,
            "lens_model": model.lens_model,
            "aperture": model.aperture,
            "shutter_speed": model.shutter_speed,
            "iso": model.iso,
            "focal_length": model.focal_length,
            "date_taken": model.date_taken,
            "dimensions": (
                f"{model.image_width} × {model.image_height}"
                if model.image_width and model.image_height
                else ""
            ),
            "flash": model.flash,
        }
        for key, label in self._exif_values.items():
            label.setText(str(values[key]) if values[key] else "—")

    def _sync_filmstrip_selection(self, photo_id: str) -> None:
        row = self._filmstrip_model.row_for_id(photo_id)
        if row < 0:
            return
        index = self._filmstrip_model.index(row, 0)
        self._filmstrip.setCurrentIndex(index)
        self._filmstrip.scrollTo(
            index, QListView.ScrollHint.PositionAtCenter
        )

    def _on_filmstrip_clicked(self, index: QModelIndex) -> None:
        photo_id = index.data(int(PhotoRoles.ID))
        if isinstance(photo_id, str):
            self._show_photo_id(photo_id)

    def _show_photo_id(
        self,
        photo_id: str,
    ) -> None:
        if (
            self._filmstrip_model.row_for_id(photo_id) < 0
            or self._model_provider(photo_id) is None
        ):
            return
        self._current_photo_id = photo_id
        self._render_photo(photo_id)

    def navigate(self, delta: int) -> None:
        photo_id = self.current_photo_id()
        if photo_id is None:
            return
        new_index = self._filmstrip_model.row_for_id(photo_id) + delta
        new_photo_id = self._filmstrip_model.photo_id_at(new_index)
        if new_photo_id is not None:
            self._show_photo_id(new_photo_id)

    def current_index(self) -> int:
        photo_id = self.current_photo_id()
        return (
            self._filmstrip_model.row_for_id(photo_id)
            if photo_id is not None
            else -1
        )

    def current_photo_id(self) -> str | None:
        photo_id = self._current_photo_id
        if photo_id is None:
            return None
        if (
            self._filmstrip_model.row_for_id(photo_id) >= 0
        ):
            return photo_id
        return self._select_neighbor(0)

    def _select_neighbor(self, preferred_row: int) -> str | None:
        count = self._filmstrip_model.rowCount()
        if count == 0:
            self._current_photo_id = None
            self.invalidate_preview_requests()
            self._image_view.clear_image()
            self._tool_overlay.hide()
            self._sync_mark_controls("", enabled=False)
            main_window = self._find_main_window()
            if main_window is not None:
                main_window.exit_preview()
            return None
        row = max(0, min(preferred_row, count - 1))
        replacement = self._filmstrip_model.photo_id_at(row)
        if replacement is None:
            return None
        self._current_photo_id = replacement
        self._render_photo(replacement)
        return replacement

    def _bind_filmstrip_model(self, model: PhotoListModel) -> None:
        old_model = self._bound_filmstrip_model
        if old_model is model:
            self._filmstrip.setModel(model)
            return
        if old_model is not None:
            for signal, slot in (
                (
                    old_model.rowsAboutToBeRemoved,
                    self._on_rows_about_to_be_removed,
                ),
                (old_model.rowsRemoved, self._on_rows_removed),
                (old_model.modelAboutToBeReset, self._on_model_about_to_reset),
                (old_model.modelReset, self._on_model_reset),
                (old_model.rowsInserted, self._on_rows_inserted),
                (old_model.dataChanged, self._on_model_data_changed),
            ):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
        self._bound_filmstrip_model = model
        self._filmstrip.setModel(model)
        model.rowsAboutToBeRemoved.connect(
            self._on_rows_about_to_be_removed
        )
        model.rowsRemoved.connect(self._on_rows_removed)
        model.modelAboutToBeReset.connect(self._on_model_about_to_reset)
        model.modelReset.connect(self._on_model_reset)
        model.rowsInserted.connect(self._on_rows_inserted)
        model.dataChanged.connect(self._on_model_data_changed)

    def _on_rows_about_to_be_removed(
        self,
        _parent: QModelIndex,
        first: int,
        last: int,
    ) -> None:
        photo_id = self._current_photo_id
        current_row = (
            self._filmstrip_model.row_for_id(photo_id)
            if photo_id is not None
            else -1
        )
        self._removed_current_row = (
            current_row if first <= current_row <= last else -1
        )

    def _on_rows_removed(
        self,
        _parent: QModelIndex,
        _first: int,
        _last: int,
    ) -> None:
        removed_row = self._removed_current_row
        self._removed_current_row = -1
        if removed_row >= 0:
            self._select_neighbor(removed_row)
            return
        photo_id = self._current_photo_id
        if (
            photo_id is not None
            and self._filmstrip_model.row_for_id(photo_id) >= 0
        ):
            self._sync_filmstrip_selection(photo_id)
            self._update_position_label()

    def _on_model_about_to_reset(self) -> None:
        photo_id = self._current_photo_id
        self._row_before_reset = (
            self._filmstrip_model.row_for_id(photo_id)
            if photo_id is not None
            else 0
        )
        self.invalidate_preview_requests()

    def _on_model_reset(self) -> None:
        photo_id = self._current_photo_id
        if (
            photo_id is not None
            and self._filmstrip_model.row_for_id(photo_id) >= 0
        ):
            self._render_photo(photo_id)
        else:
            self._select_neighbor(self._row_before_reset)
        self._row_before_reset = -1

    def _on_rows_inserted(
        self,
        _parent: QModelIndex,
        _first: int,
        _last: int,
    ) -> None:
        photo_id = self._current_photo_id
        if (
            photo_id is not None
            and self._filmstrip_model.row_for_id(photo_id) >= 0
        ):
            self._sync_filmstrip_selection(photo_id)
            self._update_position_label()

    def _on_model_data_changed(
        self,
        top_left: QModelIndex,
        bottom_right: QModelIndex,
        roles: list[int],
    ) -> None:
        photo_id = self._current_photo_id
        current_row = (
            self._filmstrip_model.row_for_id(photo_id)
            if photo_id is not None
            else -1
        )
        if not top_left.row() <= current_row <= bottom_right.row():
            return
        model = self._model_provider(photo_id)
        if model is None:
            return
        changed = set(roles)
        if not changed:
            self._render_photo(photo_id)
            return
        if int(PhotoRoles.MARK) in changed:
            self._sync_mark_controls(
                model.mark,
                enabled=self._current_preview_is_loaded(photo_id),
            )
        if int(PhotoRoles.ANALYSIS) in changed:
            self._show_analysis(model)
            self._show_exif(model)
        if changed & {
            int(PhotoRoles.PATH),
            int(PhotoRoles.FILENAME),
            int(PhotoRoles.ASSET_KIND),
        }:
            self._render_photo(photo_id)

    def _mark_current(self, mark: str) -> None:
        photo_id = self.current_photo_id()
        if photo_id is None or not self._current_preview_is_loaded(photo_id):
            return
        model = self._model_provider(photo_id)
        if model is None:
            return
        main_window = self._find_main_window()
        result: object = True
        if main_window is None:
            model.mark = mark
        else:
            result = main_window.mark_photo(photo_id, mark)
        if self.current_photo_id() != photo_id:
            return
        current_model = self._model_provider(photo_id)
        if current_model is None:
            self._sync_mark_controls("", enabled=False)
            return
        value = current_model.mark if result is False else mark
        self._sync_mark_controls(
            value,
            enabled=self._current_preview_is_loaded(photo_id),
        )

    def _rotate(self, delta: int) -> None:
        photo_id = self.current_photo_id()
        if photo_id is None:
            return
        model = self._model_provider(photo_id)
        if model is None:
            return
        rotation = self._image_view.rotate(delta)
        model.rotation = rotation

    def _image_view_zoom_in(self) -> None:
        self._image_view.zoom_in()

    def _image_view_zoom_out(self) -> None:
        self._image_view.zoom_out()

    def _image_view_fit(self) -> None:
        self._image_view.zoom_to_fit()

    def _image_view_100(self) -> None:
        self._image_view.zoom_to_100()
