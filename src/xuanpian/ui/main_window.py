from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from pathlib import Path
from time import monotonic
from typing import Callable
from uuid import uuid4

from PyQt6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QWidget,
)

from src.xuanpian.domain.analysis import AnalysisFailure, AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.domain.technical_analysis import TechnicalAnalysisResult
from src.xuanpian.cache.thumbnail_cache import ThumbnailCache
from src.xuanpian.domain.photo import PhotoAssetRecord, PhotoRecord
from src.xuanpian.domain.photo_move import PhotoMoveTransaction
from src.xuanpian.engine.exif_reader import read_exif
from src.xuanpian.services.export_service import (
    ExportItem,
    ExportMode,
    ExportRequest,
    ExportService,
    ExportUnit,
)
from src.xuanpian.services.analysis_service import PhotoAnalysisRequest
from src.xuanpian.services.import_service import (
    MANAGED_STATUS_DIRECTORIES,
    PhotoCandidate,
    candidate_to_photo_record,
)
from src.xuanpian.services.fingerprints import fingerprint
from src.xuanpian.services.photo_move_service import (
    PhotoMoveService,
    RecoveryDecision,
    RecoveryResult,
)
from src.xuanpian.store.database_lock import ProjectDatabaseLockedError
from src.xuanpian.store.project_store import (
    PhotoChange,
    PhotoReconciliation,
    ProjectStore,
)
from src.xuanpian.tasks.analysis_coordinator import (
    AnalysisCoordinator,
    AnalysisResult,
)
from src.xuanpian.tasks.contracts import (
    JobFinished,
    JobProgress,
    JobState,
)
from src.xuanpian.tasks.import_coordinator import (
    ImportBatch,
    ImportCoordinator,
)
from src.xuanpian.tasks.io_coordinator import (
    ExportFinished,
    ExportProgress,
    ExportStarted,
    IoCoordinator,
    MetadataRequest,
    MetadataResult,
)
from src.xuanpian.tasks.move_coordinator import (
    MoveCoordinator,
    MoveFinished,
    MoveItemCompleted,
    MovePreflightRejected,
    MoveStartRejected,
    MoveStarted,
)
from src.xuanpian.ui.export_dialog import ExportDialog
from src.xuanpian.ui.culling_workspace import CullingWorkspace
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.job_bridge import QtJobBridge
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles
from src.xuanpian.ui.view_toggle import ViewToggle
from src.xuanpian.ui.thumbnail_browser import GROUPS, ThumbnailBrowser
from src.xuanpian.ui.icons import application_icon, ui_icon

_THUMB_MIN = 110
_THUMB_MAX = 220
_THUMB_DEFAULT = 150

logger = logging.getLogger(__name__)


class TransientAssetScanError(RuntimeError):
    """A source exists conceptually but cannot be inspected right now."""


@dataclass(frozen=True, slots=True)
class UndoCommand:
    """One exact retryable action, including paths and file fingerprints."""

    photo_id: str
    before_mark: PhotoMark
    after_mark: PhotoMark
    before_record: PhotoRecord
    after_record: PhotoRecord
    plan: PhotoMoveTransaction


@dataclass(frozen=True, slots=True)
class _MarkUndoCommand:
    """One database-only culling mark that can be restored once."""

    photo_id: str
    before_mark: PhotoMark
    after_mark: PhotoMark


@dataclass(slots=True)
class _ActiveCullingMove:
    project_id: object
    store: ProjectStore
    plan: PhotoMoveTransaction
    before_record: PhotoRecord
    model: ImageFileModel
    is_undo: bool
    undo_command: UndoCommand | None
    job_id: object | None = None
    handle: object | None = None
    completed_ordinals: set[int] = field(default_factory=set)
    blocked: bool = False
    rollback_of: PhotoMoveTransaction | None = None


def default_project_database_path(
    root: Path,
    *,
    app_data_root: Path | None = None,
) -> Path:
    """Resolve one import root to a stable app-data project database."""
    normalized_root = str(Path(root).expanduser().resolve())
    digest = hashlib.sha256(normalized_root.encode("utf-8")).hexdigest()
    data_root = app_data_root or Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
    )
    return Path(data_root) / "projects" / f"{digest}.sqlite3"


def open_project_root(path: str) -> bool:
    return QDesktopServices.openUrl(QUrl.fromLocalFile(path))


class MainWindow(QMainWindow):
    """Main application window with three-section layout."""

    def __init__(
        self,
        export_service: ExportService | None = None,
        *,
        import_coordinator: ImportCoordinator | None = None,
        analysis_coordinator: AnalysisCoordinator | None = None,
        io_coordinator: IoCoordinator | None = None,
        project_path_resolver: Callable[[Path], Path] | None = None,
        project_store_factory: Callable[
            [Path, Path], ProjectStore
        ] | None = None,
        thumbnail_cache: ThumbnailCache | None = None,
        metadata_loader: Callable[[MetadataRequest], MetadataResult]
        | None = None,
        photo_move_service: PhotoMoveService | None = None,
        move_coordinator: MoveCoordinator | None = None,
        shutdown_timeout: float = 2.0,
    ) -> None:
        super().__init__()
        self.setWindowTitle("选片工作台")
        self.resize(1200, 800)
        self.setMinimumSize(940, 560)

        self._active_project_id = uuid4()
        self._active_job_ids = {
            "import": None,
            "analysis": None,
            "export": None,
        }
        self._import_handle = None
        self._analysis_handle = None
        self._export_handle = None
        self._export_context = None
        self._io_handles = {}
        self._metadata_path_by_photo_id: dict[str, str] = {}
        self._project_store: ProjectStore | None = None
        if (project_path_resolver is None) != (
            project_store_factory is None
        ):
            raise ValueError(
                "project path resolver and store factory must be "
                "provided together"
            )
        self._project_path_resolver = project_path_resolver
        self._project_store_factory = project_store_factory
        self._thumbnail_cache = thumbnail_cache
        self._photo_move_service = photo_move_service or PhotoMoveService()
        self._move_coordinator = move_coordinator or MoveCoordinator(
            self._photo_move_service
        )
        self._active_culling_move: _ActiveCullingMove | None = None
        self._undo_command: UndoCommand | None = None
        self._move_event_processing_enabled = True
        self._pending_thumbnail_invalidations = set()
        self._thumbnail_cleanup_error: str | None = None
        if shutdown_timeout < 0:
            raise ValueError("shutdown_timeout must be non-negative")
        self._shutdown_timeout = shutdown_timeout
        self._transient_photo_ids: dict[str, str] = {}
        self._restored_photo_ids: set[str] = set()
        self._project_recovery_blocked = False
        self._browse_mode = "grid"
        self._uses_direct_export_service = (
            export_service is not None and io_coordinator is None
        )
        self._export_service = (
            export_service if export_service is not None else ExportService()
        )
        self._import_coordinator = import_coordinator or ImportCoordinator()
        self._analysis_coordinator = (
            analysis_coordinator or AnalysisCoordinator()
        )
        if io_coordinator is not None:
            self._io_coordinator = io_coordinator
        else:
            self._io_coordinator = IoCoordinator(
                export_service=self._export_service,
                thumbnail_cache=thumbnail_cache,
                metadata_loader=metadata_loader,
            )
        self._setup_central_stack()
        self._setup_toolbar()
        self._setup_statusbar()
        self._burst_recompute_timer = QTimer(self)
        self._burst_recompute_timer.setSingleShot(True)
        self._burst_recompute_timer.setInterval(400)
        self._burst_recompute_timer.timeout.connect(
            self._thumbnail_browser.refresh_burst_groups
        )
        self._connect_signals()
        self._job_bridge = QtJobBridge(self)
        self._job_bridge.batch.connect(self._on_import_batch)
        self._job_bridge.result.connect(self._on_analysis_result)
        self._job_bridge.metadata.connect(self._on_metadata_result)
        self._job_bridge.export_started.connect(self._on_export_started)
        self._job_bridge.export_progress.connect(self._on_export_progress)
        self._job_bridge.export_finished.connect(self._on_export_finished)
        self._job_bridge.move_started.connect(self._on_move_started)
        self._job_bridge.move_item_completed.connect(
            self._on_move_item_completed
        )
        self._job_bridge.move_finished.connect(self._on_move_finished)
        self._job_bridge.move_preflight_rejected.connect(self._on_move_preflight_rejected)
        self._job_bridge.progress.connect(self._on_job_progress)
        self._job_bridge.finished.connect(self._on_job_finished)

    # ---- central stack ----

    def _setup_central_stack(self) -> None:
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._thumbnail_browser = ThumbnailBrowser(self)
        self._stack.addWidget(self._thumbnail_browser)  # index 0

        self._preview_placeholder = QLabel("预览模式（按 Esc 返回）")
        self._preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._preview_placeholder)  # index 1 (placeholder)

        self._culling_session: CullingSession | None = None
        self._culling_workspace = CullingWorkspace(
            self,
            provider=self._thumbnail_browser.model_for_photo_id,
        )
        self._culling_workspace.back_requested.connect(self.exit_culling)
        self._culling_workspace.mark_requested.connect(
            self.request_culling_mark
        )
        self._culling_workspace.navigate_requested.connect(
            self.navigate_culling
        )
        self._culling_workspace.undo_requested.connect(
            self.request_culling_undo
        )
        self._culling_workspace.review_hold_requested.connect(
            lambda: self._review_culling_status(StatusFilter.HOLD)
        )
        self._culling_workspace.review_discard_requested.connect(
            lambda: self._review_culling_status(StatusFilter.DISCARD)
        )
        self._culling_workspace.open_directory_requested.connect(
            self._open_culling_project_root
        )
        self._culling_workspace.set_undo_available(False)
        self._stack.addWidget(self._culling_workspace)

    # ---- toolbar ----

    def _setup_toolbar(self) -> None:
        self._toolbar = QToolBar("工具栏", self)
        self._toolbar.setObjectName("mainToolBar")
        self._toolbar.setMovable(False)
        self.addToolBar(self._toolbar)

        self._brand_icon = QLabel(self._toolbar)
        self._brand_icon.setObjectName("brandIcon")
        self._brand_icon.setPixmap(
            application_icon().pixmap(22, 22)
        )
        self._brand_icon.setFixedSize(22, 22)
        self._toolbar.addWidget(self._brand_icon)
        self._brand_label = QLabel("选片工作台", self._toolbar)
        self._brand_label.setObjectName("brandLabel")
        self._toolbar.addWidget(self._brand_label)
        self._toolbar.addSeparator()

        self._import_btn = QPushButton(
            ui_icon("folder"), "导入照片", self._toolbar
        )
        self._import_btn.clicked.connect(self._on_import)
        self._toolbar.addWidget(self._import_btn)

        self._analyze_btn = QPushButton(
            ui_icon("analyze"), "技术分析", self._toolbar
        )
        self._analyze_btn.clicked.connect(self._on_analyze_all)
        self._analyze_btn.setEnabled(False)
        self._toolbar.addWidget(self._analyze_btn)

        self._cull_btn = QPushButton(
            ui_icon("tag"), "开始筛选", self._toolbar
        )
        self._cull_btn.clicked.connect(self.enter_culling)
        self._cull_btn.setEnabled(False)
        self._toolbar.addWidget(self._cull_btn)

        self._toolbar.addSeparator()

        self._view_toggle = ViewToggle(self._toolbar)
        self._view_toggle.view_changed.connect(self._on_view_toggle)
        self._view_toggle.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self._toolbar.addWidget(self._view_toggle)

        spacer = QWidget(self._toolbar)
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self._toolbar.addWidget(spacer)

        self._batch_mark_btn = QPushButton(
            ui_icon("tag"), "批量标记", self._toolbar
        )
        self._batch_mark_menu = QMenu(self._batch_mark_btn)
        for mark in ("保留", "待定", "舍弃"):
            action = self._batch_mark_menu.addAction(mark)
            action.triggered.connect(
                lambda checked=False, value=mark: self._on_batch_mark(value)
            )
        self._batch_mark_btn.setMenu(self._batch_mark_menu)
        self._batch_mark_btn.setEnabled(False)
        self._toolbar.addWidget(self._batch_mark_btn)

        self._export_btn = QPushButton(
            ui_icon("export", color="#08131F"), "导出照片", self._toolbar
        )
        self._export_btn.setProperty("primaryAction", True)
        self._export_btn.clicked.connect(self._on_export)
        self._toolbar.addWidget(self._export_btn)

        self._toolbar.addSeparator()

        self._size_label = QLabel(self._size_text(_THUMB_DEFAULT))
        self._size_label.setMinimumWidth(16)
        self._toolbar.addWidget(self._size_label)

        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(_THUMB_MIN, _THUMB_MAX)
        self._size_slider.setValue(_THUMB_DEFAULT)
        self._size_slider.setFixedWidth(90)
        self._size_slider.valueChanged.connect(self._on_thumbnail_size_changed)
        self._toolbar.addWidget(self._size_slider)

    def _on_view_toggle(self, key: str) -> None:
        self.set_view_mode(key)

    def set_view_mode(self, mode: str) -> None:
        """Switch browse mode (grid/list); leaving preview when active."""
        self._browse_mode = mode
        self._view_toggle.set_view(mode)
        self._thumbnail_browser.set_view_mode(mode)
        if self._in_preview():
            self.exit_preview()

    def view_mode(self) -> str:
        return self._browse_mode

    def last_browse_mode(self) -> str:
        return self._browse_mode

    def _in_preview(self) -> bool:
        return (
            hasattr(self, "_preview_widget")
            and self._preview_widget is not None
            and self._stack.currentWidget() is self._preview_widget
        )

    # ---- status bar ----

    def _setup_statusbar(self) -> None:
        self._status_bar = QStatusBar()
        self._status_bar.setObjectName("mainStatusBar")
        self.setStatusBar(self._status_bar)

        self._status_filter_label = QLabel("筛选：全部", self._status_bar)
        self._status_filter_label.setObjectName("statusFilter")
        self._status_bar.addPermanentWidget(self._status_filter_label)

        self._status_counts_label = QLabel("共 0 张", self._status_bar)
        self._status_counts_label.setObjectName("statusCounts")
        self._status_bar.addPermanentWidget(self._status_counts_label)

        self._analysis_progress = QProgressBar(self._status_bar)
        self._analysis_progress.setObjectName("analysisProgress")
        self._analysis_progress.setRange(0, 100)
        self._analysis_progress.setFixedWidth(160)
        self._analysis_progress.setTextVisible(True)
        self._analysis_progress.setVisible(False)
        self._status_bar.addPermanentWidget(self._analysis_progress)

        self._update_status_bar()

    def _update_status_bar(self) -> None:
        all_models = self._thumbnail_browser.all_models()
        total = len(all_models)
        normalized_marks = [normalize_legacy_mark(m.mark) for m in all_models]
        picks = normalized_marks.count(PhotoMark.KEEP)
        pending = normalized_marks.count(PhotoMark.HOLD)
        rejects = normalized_marks.count(PhotoMark.DISCARD)

        selected = len(self._thumbnail_browser.selected_photo_ids())

        self._status_filter_label.setText(
            f"筛选：{self._thumbnail_browser.current_group_label()}"
        )
        self._status_counts_label.setText(
            f"共 {total} 张  ·  已选 {selected}"
            f"  ·  保留 {picks}  待定 {pending}  舍弃 {rejects}"
        )

    # ---- signals ----

    def _connect_signals(self) -> None:
        self._thumbnail_browser.selection_changed.connect(self._update_status_bar)
        self._thumbnail_browser.model_set_changed.connect(self._update_status_bar)
        self._thumbnail_browser.model_set_changed.connect(
            self._refresh_culling_eligibility
        )
        self._thumbnail_browser.analysis_status_changed.connect(
            self._refresh_culling_eligibility
        )
        self._thumbnail_browser.selection_changed.connect(
            self._update_batch_mark_state
        )
        self._thumbnail_browser.thumbnail_size_changed.connect(self._on_browser_zoom)

    def _update_batch_mark_state(self) -> None:
        enabled = bool(self._thumbnail_browser.selected_photo_ids()) and (
            self._project_store is not None
            and self._active_project_id is not None
            and not self._project_recovery_blocked
            and self._active_culling_move is None
            and all(job_id is None for job_id in self._active_job_ids.values())
        )
        self._batch_mark_btn.setEnabled(enabled)
        for action in self._batch_mark_menu.actions():
            action.setEnabled(enabled)
        self._batch_mark_btn.setToolTip(
            "批量设置选中照片的标记；不会移动原文件"
            if enabled
            else "请先选择照片"
        )

    def _on_batch_mark(self, mark: str) -> None:
        photo_ids = self._thumbnail_browser.selected_photo_ids()
        if not photo_ids:
            return
        self.mark_photos(photo_ids, mark)

    # ---- import ----

    def _on_import(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if not folder:
            return
        self._start_import(folder)

    def _start_import(self, folder: str) -> None:
        if self._active_culling_move is not None:
            self._status_bar.showMessage(
                "照片移动完成前无法切换项目",
                8000,
            )
            return
        if self._stack.currentWidget() is self._culling_workspace:
            self.exit_culling()
        self._flush_thumbnail_invalidations()
        root = Path(folder).expanduser().resolve()
        old_import = self._import_handle
        old_analysis = self._analysis_handle
        old_export = self._export_handle
        old_io = tuple(self._io_handles.values())
        old_store = self._project_store
        self._active_project_id = uuid4()
        self._active_job_ids = {
            "import": None,
            "analysis": None,
            "export": None,
        }
        self._import_handle = None
        self._analysis_handle = None
        self._export_handle = None
        self._export_context = None
        self._io_handles = {}
        self._metadata_path_by_photo_id = {}
        self._transient_photo_ids = {}
        self._restored_photo_ids = set()
        self._project_recovery_blocked = False
        self._undo_command = None
        self._culling_workspace.set_undo_available(False)
        self._project_store = None
        self._export_btn.setEnabled(True)
        self._cancel_and_wait_handles(
            (old_import, old_analysis, old_export, *old_io)
        )
        if old_store is not None:
            old_store.close()

        self._thumbnail_browser.set_models([])
        self._thumbnail_browser.show_loading()
        self._analyze_btn.setEnabled(False)
        self._export_btn.setEnabled(True)
        self._status_bar.showMessage("正在扫描照片…")

        if (
            self._project_path_resolver is not None
            and self._project_store_factory is not None
        ):
            database_path = self._project_path_resolver(root)
            try:
                self._project_store = self._project_store_factory(
                    database_path,
                    root,
                )
            except ProjectDatabaseLockedError:
                self._thumbnail_browser.hide_loading()
                self._status_bar.showMessage(
                    "项目已在另一个窗口打开；关闭另一个窗口后重试",
                    8000,
                )
                QMessageBox.warning(
                    self,
                    "项目已打开",
                    "该照片项目已在另一个窗口打开。"
                    "请关闭另一个窗口后重试。",
                )
                self._refresh_culling_eligibility()
                return
            try:
                unresolved = self._recover_pending_photo_moves()
            except Exception:
                logger.exception("Failed to recover pending photo moves")
                self._block_for_move_recovery(())
                return
            if unresolved:
                self._block_for_move_recovery(unresolved)
                return
            self._project_store.recover_interrupted()
            self._restore_project()

        handle = self._import_coordinator.start(
            self._active_project_id,
            root,
            self._job_bridge.publish,
        )
        self._import_handle = handle
        self._active_job_ids["import"] = handle.job_id
        self._refresh_culling_eligibility()

    def _recover_pending_photo_moves(self) -> tuple[RecoveryResult, ...]:
        store = self._project_store
        if store is None:
            return ()
        mover = PhotoMoveService()
        unresolved: list[RecoveryResult] = []
        for pending in store.list_pending_photo_moves():
            transaction = pending.transaction
            result = mover.recover_pending_move(
                transaction,
                completed_ordinals=pending.completed_ordinals,
            )
            if result.decision in (
                RecoveryDecision.ABORTED,
                RecoveryDecision.ROLLED_BACK,
            ):
                store.abort_photo_move(transaction.id)
                continue
            if result.decision is RecoveryDecision.READY_TO_COMMIT:
                for item in transaction.items:
                    if item.ordinal not in pending.completed_ordinals:
                        store.mark_move_item_completed(
                            transaction.id,
                            item.ordinal,
                        )
                store.commit_photo_move(transaction.id, result.moved_assets)
                continue
            unresolved.append(result)
        return tuple(unresolved)

    def _block_for_move_recovery(
        self,
        results: tuple[RecoveryResult, ...],
    ) -> None:
        self._project_recovery_blocked = True
        self._analyze_btn.setEnabled(False)
        self._thumbnail_browser.hide_loading()
        paths = tuple(
            dict.fromkeys(
                path
                for result in results
                for path in result.recovery_paths
            )
        )
        path_details = ""
        if paths:
            path_details = "\n\n需要检查的文件：\n" + "\n".join(
                str(path) for path in paths
            )
        self._status_bar.showMessage(
            "上次照片移动状态需要人工确认；扫描和分析已暂停"
        )
        QMessageBox.warning(
            self,
            "照片移动需要处理",
            "检测到上次照片移动未能安全完成。为避免覆盖文件，"
            "本项目的扫描和分析已暂停；移动记录已保留。"
            "请先备份"
            "列出的文件，再处理冲突后重新打开项目。"
            f"{path_details}",
        )

    def _cancel_and_wait_handles(self, handles: tuple[object, ...]) -> None:
        active = tuple(handle for handle in handles if handle is not None)
        for handle in active:
            handle.cancel()
        deadline = monotonic() + self._shutdown_timeout
        for handle in active:
            wait = getattr(handle, "wait", None)
            if wait is None:
                continue
            remaining = max(0.0, deadline - monotonic())
            wait(remaining)

    def _restore_project(self) -> None:
        store = self._project_store
        if store is None:
            return
        models: list[ImageFileModel] = []
        managed_models: list[ImageFileModel] = []
        for record in store.list_photos():
            self._restored_photo_ids.add(record.id)
            model = self._model_from_record(record)
            if model is None:
                continue
            try:
                analysis = store.get_current_technical_analysis(record.id)
                if analysis is not None:
                    if analysis.status is AnalysisStatus.SUCCEEDED:
                        result = TechnicalAnalysisResult.from_dict(
                            analysis.result
                        )
                        self._apply_technical_result(model, result)
                    model.technical_status = analysis.status
                    if analysis.failure is not None:
                        model.analysis_failures["technical"] = analysis.failure
            except (TypeError, ValueError):
                # A malformed historic result is treated as stale and requeued.
                pass
            models.append(model)
            if any(
                Path(asset.fingerprint.path).parent.name
                in MANAGED_STATUS_DIRECTORIES
                for asset in record.source_assets
            ):
                managed_models.append(model)
        self._thumbnail_browser.set_models(models)
        self._analyze_btn.setEnabled(bool(models))
        self._update_status_bar()
        self._submit_metadata(managed_models)

    @staticmethod
    def _model_from_record(
        record: PhotoRecord,
    ) -> ImageFileModel | None:
        jpeg_path = (
            ""
            if record.jpeg is None
            else record.jpeg.fingerprint.path
        )
        raw_path = (
            ""
            if record.raw is None
            else record.raw.fingerprint.path
        )
        analysis_path = jpeg_path or raw_path
        if not analysis_path:
            return None
        return ImageFileModel(
            path=analysis_path,
            filename=record.filename,
            photo_id=record.id,
            jpeg_path=jpeg_path,
            raw_path=raw_path,
            mark=record.mark,
        )

    def _persistent_candidate(
        self,
        candidate: PhotoCandidate,
    ) -> PhotoCandidate:
        store = self._project_store
        if store is None:
            return candidate
        persistent_id = self._transient_photo_ids.get(
            candidate.photo_id
        )
        if persistent_id is None:
            by_path = store.photo_ids_by_asset_paths(
                candidate.source_paths
            )
            persistent_id = next(
                (
                    by_path[path]
                    for path in candidate.source_paths
                    if path in by_path
                ),
                candidate.photo_id,
            )
            self._transient_photo_ids[
                candidate.photo_id
            ] = persistent_id

        mapped_superseded = [
            self._transient_photo_ids.get(
                transient_id,
                transient_id,
            )
            for transient_id in candidate.superseded_photo_ids
        ]
        if (
            persistent_id not in self._restored_photo_ids
            and (
                restored_survivor := next(
                    (
                        photo_id
                        for photo_id in mapped_superseded
                        if photo_id in self._restored_photo_ids
                    ),
                    None,
                )
            )
            is not None
        ):
            mapped_superseded.insert(0, persistent_id)
            persistent_id = restored_survivor
            self._transient_photo_ids[
                candidate.photo_id
            ] = persistent_id

        superseded = []
        for mapped_id in mapped_superseded:
            if mapped_id != persistent_id and mapped_id not in superseded:
                superseded.append(mapped_id)
        return replace(
            candidate,
            photo_id=persistent_id,
            superseded_photo_ids=tuple(superseded),
        )

    def _persist_candidates(
        self,
        candidates: tuple[PhotoCandidate, ...],
        models_by_id: dict[str, ImageFileModel],
    ) -> tuple[
        tuple[PhotoCandidate, ...],
        tuple[PhotoChange | None, ...],
        dict[str, str],
    ]:
        store = self._project_store
        if store is None:
            return candidates, (None,) * len(candidates), {}
        working_records: dict[str, PhotoRecord | None] = {}

        def current_record(photo_id: str) -> PhotoRecord | None:
            if photo_id not in working_records:
                working_records[photo_id] = store.get_photo(photo_id)
            return working_records[photo_id]

        reconciliations: list[PhotoReconciliation] = []
        effective_candidates: list[PhotoCandidate] = []
        stale_fingerprints = set()
        planned_photo_ids: set[str] = set()
        for candidate in candidates:
            current_model = models_by_id.get(candidate.photo_id)
            previous_records = tuple(
                record
                for photo_id in dict.fromkeys(
                    (
                        candidate.photo_id,
                        *candidate.superseded_photo_ids,
                    )
                )
                if (record := current_record(photo_id)) is not None
            )
            previous_survivor = next(
                (
                    record
                    for record in previous_records
                    if record.id == candidate.photo_id
                ),
                None,
            )
            mark = (
                current_model.mark
                if current_model is not None
                else (
                    previous_survivor.mark
                    if previous_survivor is not None
                    else PhotoMark.UNPROCESSED.value
                )
            )
            # Fingerprinting happens here, before ProjectStore opens its
            # transaction. This keeps filesystem I/O outside SQLite locks.
            updated = candidate_to_photo_record(candidate, mark=mark)
            if updated.analysis_asset is None:
                # The coordinator may enumerate a path just before it is
                # removed. A zero-asset event is not a photo and must not
                # create or merge identities; the completion reconcile owns
                # transitions for already-known missing paths.
                continue
            if (
                previous_survivor is not None
                and (
                    current_model is not None
                    or candidate.photo_id in planned_photo_ids
                )
                and not candidate.superseded_photo_ids
                and set(candidate.source_paths)
                < {
                    asset.fingerprint.path
                    for asset in previous_survivor.source_assets
                }
            ):
                updated = replace(
                    updated,
                    jpeg=updated.jpeg or previous_survivor.jpeg,
                    raw=updated.raw or previous_survivor.raw,
                )
            effective_candidates.append(
                replace(
                    candidate,
                    jpeg_path=(
                        ""
                        if updated.jpeg is None
                        else updated.jpeg.fingerprint.path
                    ),
                    raw_path=(
                        ""
                        if updated.raw is None
                        else updated.raw.fingerprint.path
                    ),
                )
            )
            reconciliations.append(
                PhotoReconciliation(
                    updated,
                    candidate.superseded_photo_ids,
                )
            )
            current_fingerprints = {
                asset.fingerprint for asset in updated.source_assets
            }
            for record in previous_records:
                for asset in record.source_assets:
                    if asset.fingerprint not in current_fingerprints:
                        stale_fingerprints.add(asset.fingerprint)
            persisted_mark = (
                previous_survivor.mark
                if previous_survivor is not None
                else updated.mark
            )
            working_records[candidate.photo_id] = replace(
                updated,
                mark=persisted_mark,
            )
            planned_photo_ids.add(candidate.photo_id)
            for photo_id in candidate.superseded_photo_ids:
                if photo_id != candidate.photo_id:
                    working_records[photo_id] = None

        results = store.reconcile_photos_with_marks(reconciliations)
        changes = tuple(result.change for result in results)
        resolved_marks = {
            candidate.photo_id: result.resolved_mark
            for candidate, result in zip(
                effective_candidates,
                results,
                strict=True,
            )
        }
        self._pending_thumbnail_invalidations.update(stale_fingerprints)
        return tuple(effective_candidates), tuple(changes), resolved_marks

    def _flush_thumbnail_invalidations(self, *, attempts: int = 1) -> bool:
        if not self._pending_thumbnail_invalidations:
            return True
        pending = tuple(self._pending_thumbnail_invalidations)
        self._pending_thumbnail_invalidations.clear()
        if self._thumbnail_cache is None:
            return True
        last_error: Exception | None = None
        for _attempt in range(max(1, attempts)):
            try:
                self._thumbnail_cache.invalidate_many(pending)
            except Exception as error:
                last_error = error
                logger.warning(
                    "Failed to queue %d stale thumbnail fingerprints",
                    len(pending),
                    exc_info=True,
                )
                continue
            self._thumbnail_cleanup_error = None
            return True
        self._pending_thumbnail_invalidations.update(pending)
        self._thumbnail_cleanup_error = (
            None if last_error is None else str(last_error)
        )
        return False

    def _reconcile_missing_assets(self) -> None:
        store = self._project_store
        if store is None:
            return
        removed: list[ImageFileModel] = []
        updated_records: list[PhotoRecord] = []
        model_updates: list[
            tuple[ImageFileModel, object, object]
        ] = []
        stale_fingerprints = set()
        for record in store.list_photos():
            jpeg = self._refresh_asset(record.jpeg)
            raw = self._refresh_asset(record.raw)
            if jpeg == record.jpeg and raw == record.raw:
                continue
            updated = replace(record, jpeg=jpeg, raw=raw)
            updated_records.append(updated)
            current_fingerprints = {
                asset.fingerprint for asset in updated.source_assets
            }
            for asset in record.source_assets:
                if asset.fingerprint not in current_fingerprints:
                    stale_fingerprints.add(asset.fingerprint)
            model = self.model_for_photo_id(record.id)
            if model is None:
                continue
            if updated.analysis_asset is None:
                removed.append(model)
                continue
            model_updates.append((model, jpeg, raw))
        if updated_records:
            store.upsert_photos(updated_records)
            self._pending_thumbnail_invalidations.update(stale_fingerprints)
        for model, jpeg, raw in model_updates:
            model.jpeg_path = (
                "" if jpeg is None else jpeg.fingerprint.path
            )
            model.raw_path = "" if raw is None else raw.fingerprint.path
            model.path = model.analysis_path
            model.is_raw = bool(model.raw_path and not model.jpeg_path)
            self._clear_technical_result(model)
            model.technical_status = AnalysisStatus.PENDING
            model.analysis_failures.clear()
            self._thumbnail_browser.refresh_photo(
                model.photo_id,
                [
                    PhotoRoles.PATH,
                    PhotoRoles.ASSET_KIND,
                    PhotoRoles.ANALYSIS,
                ],
            )
        self._thumbnail_browser.remove_models(removed)

    @staticmethod
    def _refresh_asset(asset):
        if asset is None:
            return None
        try:
            current = fingerprint(Path(asset.fingerprint.path))
        except FileNotFoundError:
            return None
        except OSError as error:
            raise TransientAssetScanError(
                "暂时无法读取照片文件 "
                f"{asset.fingerprint.path}: "
                f"{type(error).__name__}: {error}"
            ) from error
        return replace(asset, fingerprint=current)

    def _is_active_event(self, project_id, job_id, kind: str) -> bool:
        return (
            project_id == self._active_project_id
            and job_id == self._active_job_ids[kind]
        )

    def _on_import_batch(self, event: ImportBatch) -> None:
        if not self._is_active_event(
            event.project_id,
            event.job_id,
            "import",
        ):
            return
        try:
            self._apply_import_batch(event)
        except Exception as error:
            logger.exception("Failed to persist import batch")
            self._status_bar.showMessage(
                f"导入数据保存失败：{type(error).__name__}: {error}",
                8000,
            )
            QMessageBox.warning(
                self,
                "导入数据保存失败",
                "这批照片未写入项目，也未更新界面。"
                f"\n\n{type(error).__name__}: {error}",
            )
        finally:
            self._flush_thumbnail_invalidations()

    def _apply_import_batch(self, event: ImportBatch) -> None:
        removed: list[ImageFileModel] = []
        touched: list[ImageFileModel] = []
        mark_changed_ids: set[str] = set()
        analysis_changed_ids: set[str] = set()
        role_values_before: dict[
            str,
            tuple[str, str, str],
        ] = {}
        models_by_id = {
            model.photo_id: model
            for model in self._thumbnail_browser.all_models()
        }
        import_items = event.items
        if self._project_store is not None:
            removed_paths = self._project_store.removed_asset_paths(
                tuple(
                    path
                    for candidate in import_items
                    for path in candidate.source_paths
                )
            )
            import_items = tuple(
                candidate
                for candidate in import_items
                if removed_paths.isdisjoint(candidate.source_paths)
            )
        candidates = tuple(
            self._persistent_candidate(candidate)
            for candidate in import_items
        )
        candidates, changes, resolved_marks = self._persist_candidates(
            candidates,
            models_by_id,
        )
        for candidate, change in zip(candidates, changes, strict=True):
            current = models_by_id.get(candidate.photo_id)
            analysis_input_changed = False
            partial_pair_update = (
                current is not None
                and not candidate.superseded_photo_ids
                and set(candidate.source_paths) < set(current.source_paths)
            )
            for photo_id in candidate.superseded_photo_ids:
                superseded = models_by_id.pop(photo_id, None)
                if superseded is not None:
                    removed.append(superseded)
                self._metadata_path_by_photo_id.pop(photo_id, None)

            model = models_by_id.get(candidate.photo_id)
            if model is None:
                model = ImageFileModel.from_candidate(candidate)
                if candidate.photo_id in resolved_marks:
                    model.mark = resolved_marks[candidate.photo_id]
                models_by_id[candidate.photo_id] = model
            else:
                resolved_mark = resolved_marks.get(candidate.photo_id)
                if (
                    resolved_mark is not None
                    and model.mark != resolved_mark
                ):
                    model.mark = resolved_mark
                    mark_changed_ids.add(candidate.photo_id)
                if partial_pair_update:
                    if change in (
                        PhotoChange.CHANGED,
                        PhotoChange.MISSING,
                    ):
                        self._clear_technical_result(model)
                        model.technical_status = AnalysisStatus.PENDING
                        model.analysis_failures.clear()
                        analysis_changed_ids.add(model.photo_id)
                    touched.append(model)
                    continue
                role_values_before.setdefault(
                    candidate.photo_id,
                    self._photo_role_values(model),
                )
                old_analysis_path = model.analysis_path
                model.upsert_candidate(candidate)
                analysis_input_changed = (
                    model.analysis_path != old_analysis_path
                )
            if (
                change in (PhotoChange.CHANGED, PhotoChange.MISSING)
                or analysis_input_changed
            ):
                self._clear_technical_result(model)
                model.technical_status = AnalysisStatus.PENDING
                model.analysis_failures.clear()
                analysis_changed_ids.add(model.photo_id)
            touched.append(model)

        removed_ids = {model.photo_id for model in removed}
        active_touched = [
            model
            for model in touched
            if model.photo_id not in removed_ids
        ]
        self._thumbnail_browser.remove_models(removed)
        self._thumbnail_browser.upsert_models(active_touched)
        for photo_id in analysis_changed_ids:
            self._thumbnail_browser.refresh_photo(
                photo_id,
                [PhotoRoles.ANALYSIS],
            )
        for photo_id in mark_changed_ids:
            if self.model_for_photo_id(photo_id) is None:
                continue
            self._thumbnail_browser._photo_list_model.update_photo(
                photo_id,
                [PhotoRoles.MARK],
            )
        for photo_id, before in role_values_before.items():
            model = self.model_for_photo_id(photo_id)
            if model is None:
                continue
            after = self._photo_role_values(model)
            changed_roles = [
                role
                for role, old_value, new_value in zip(
                    (
                        PhotoRoles.PATH,
                        PhotoRoles.FILENAME,
                        PhotoRoles.ASSET_KIND,
                    ),
                    before,
                    after,
                    strict=True,
                )
                if old_value != new_value
            ]
            self._thumbnail_browser._photo_list_model.update_photo(
                photo_id,
                changed_roles,
            )
        self._submit_metadata(active_touched)
        self._update_status_bar()

    @staticmethod
    def _photo_role_values(model: ImageFileModel) -> tuple[str, str, str]:
        return (
            model.path,
            model.filename,
            PhotoListModel.asset_kind_for(model),
        )

    def _submit_metadata(self, models: list[ImageFileModel]) -> None:
        latest_models = {
            model.photo_id: model
            for model in models
            if model.photo_id and model.analysis_path
        }
        if not latest_models:
            return
        job_id = uuid4()
        size = self._size_slider.value()
        thumbnail_size = (size, int(size * 0.75))
        requests = []
        for model in latest_models.values():
            self._metadata_path_by_photo_id[model.photo_id] = model.analysis_path
            requests.append(
                MetadataRequest(
                    project_id=self._active_project_id,
                    job_id=job_id,
                    photo_id=model.photo_id,
                    path=model.analysis_path,
                    thumbnail_size=thumbnail_size,
                )
            )
        handle = self._io_coordinator.submit_metadata(
            requests,
            self._job_bridge.publish,
        )
        self._io_handles[handle.job_id] = handle

    def _on_metadata_result(self, event: MetadataResult) -> None:
        if (
            event.project_id != self._active_project_id
            or event.job_id not in self._io_handles
        ):
            return
        model = self.model_for_photo_id(event.photo_id)
        if model is None:
            return
        if event.path != model.analysis_path:
            if (
                self._metadata_path_by_photo_id.get(model.photo_id)
                == event.path
            ):
                self._submit_metadata([model])
            return

        model.apply_exif(event.exif)
        if event.qimage is not None and not event.qimage.isNull():
            self._thumbnail_browser.update_model_thumbnail(
                model,
                event.qimage,
            )
        self._burst_recompute_timer.start()
        if event.error:
            self._status_bar.showMessage(
                f"照片信息加载失败 [{model.filename}]：{event.error}",
                5000,
            )

    def _on_job_progress(self, event: JobProgress) -> None:
        if self._is_active_event(event.project_id, event.job_id, "import"):
            self._status_bar.showMessage(
                event.message or f"正在导入… {event.completed}"
            )
        elif self._is_active_event(
            event.project_id,
            event.job_id,
            "analysis",
        ):
            total = event.total or len(self.all_models)
            safe_total = max(1, int(total))
            percent = int(
                round(min(int(event.completed), safe_total) / safe_total * 100)
            )
            self._analysis_progress.setRange(0, 100)
            self._analysis_progress.setValue(percent)
            self._analysis_progress.setFormat(
                f"分析中… {event.completed}/{total}"
            )
            self._analysis_progress.setVisible(True)
            self._status_bar.showMessage(
                event.message
                or f"分析中… {event.completed}/{total}"
            )

    def _on_job_finished(self, event: JobFinished) -> None:
        if self._is_active_event(event.project_id, event.job_id, "import"):
            reconcile_error: Exception | None = None
            transient_scan_error = False
            try:
                if event.state is JobState.SUCCEEDED:
                    self._reconcile_missing_assets()
            except TransientAssetScanError as error:
                reconcile_error = error
                transient_scan_error = True
                logger.warning(
                    "Import completion scan was temporarily unavailable",
                    exc_info=True,
                )
                detail = f"{type(error).__name__}: {error}"
                self._status_bar.showMessage(
                    f"照片扫描暂时无法完成：{detail}",
                    8000,
                )
                QMessageBox.warning(
                    self,
                    "照片扫描暂时无法完成",
                    "文件系统暂时无法读取这批照片；项目数据库和界面"
                    "保持原状态，可稍后重新导入重试。"
                    f"\n\n{detail}",
                )
            except Exception as error:
                reconcile_error = error
                self._report_persistence_error("导入收尾", error)
            finally:
                self._flush_thumbnail_invalidations()
            self._active_job_ids["import"] = None
            self._import_handle = None
            if not self._io_handles:
                self._thumbnail_browser.hide_loading()
            all_models = self.all_models
            self._analyze_btn.setEnabled(
                event.state is JobState.SUCCEEDED
                and reconcile_error is None
                and bool(all_models)
            )
            self._refresh_culling_eligibility()
            if transient_scan_error:
                self._status_bar.showMessage(
                    "导入扫描暂时无法完成；项目和界面保持原状态，"
                    "请稍后重新导入"
                )
            elif reconcile_error is not None:
                self._status_bar.showMessage(
                    "导入扫描完成，但项目数据库收尾失败；"
                    "界面保留上次已保存状态"
                )
            elif event.state is JobState.SUCCEEDED:
                self._status_bar.showMessage(
                    f"已导入 {len(all_models)} 张照片"
                )
            elif event.state is JobState.CANCELED:
                self._status_bar.showMessage("导入已取消")
            else:
                self._status_bar.showMessage(
                    f"导入失败：{event.message or '未知错误'}"
                )
            return

        if self._is_active_event(event.project_id, event.job_id, "analysis"):
            self._analysis_progress.setVisible(False)
            self._active_job_ids["analysis"] = None
            self._analysis_handle = None
            all_models = self.all_models
            self._analyze_btn.setEnabled(bool(all_models))
            persistence_error_reported = False
            analysis_changed_ids: list[str] = []
            for model in all_models:
                for name in ("technical",):
                    if (
                        getattr(model, f"{name}_status")
                        is not AnalysisStatus.RUNNING
                    ):
                        continue
                    if event.state is JobState.CANCELED:
                        status = AnalysisStatus.CANCELED
                        failure = None
                    else:
                        status = AnalysisStatus.FAILED
                        failure = AnalysisFailure(
                            analyzer=name,
                            message=(
                                event.message
                                or "分析任务未返回该项目的结果"
                            ),
                            exception_type=(
                                "AnalysisJobError"
                                if event.state is JobState.FAILED
                                else "MissingAnalysisResult"
                            ),
                        )
                    if not self._save_analysis_safely(
                        model,
                        name,
                        status,
                        None,
                        failure,
                        show_dialog=not persistence_error_reported,
                    ):
                        persistence_error_reported = True
                        continue
                    self._clear_technical_result(model)
                    setattr(model, f"{name}_status", status)
                    if failure is None:
                        model.analysis_failures.pop(name, None)
                    else:
                        model.analysis_failures[name] = failure
                    analysis_changed_ids.append(model.photo_id)
            for photo_id in analysis_changed_ids:
                self._thumbnail_browser.refresh_photo(
                    photo_id,
                    [PhotoRoles.ANALYSIS],
                )
            if event.state is JobState.SUCCEEDED:
                self._status_bar.showMessage(
                    f"分析完成 — 共 {len(all_models)} 张图片"
                )
            elif event.state is JobState.CANCELED:
                self._status_bar.showMessage("分析已取消")
            else:
                self._status_bar.showMessage(
                    f"分析失败：{event.message or '未知错误'}"
                )
            return

        if (
            event.project_id == self._active_project_id
            and event.job_id in self._io_handles
        ):
            self._io_handles.pop(event.job_id, None)
            if (
                self._active_job_ids["import"] is None
                and not self._io_handles
            ):
                self._thumbnail_browser.hide_loading()

    def _load_exif_for_models(self) -> None:
        failures = 0
        for model in self.all_models:
            data = read_exif(model.path)
            model.apply_exif(data)
            failures += bool(data.error)
        if failures:
            logger.warning("EXIF metadata unavailable for %d photos", failures)

    # ---- analyze ----

    @staticmethod
    def _apply_technical_result(
        model: ImageFileModel, result: TechnicalAnalysisResult
    ) -> None:
        """Apply one versioned technical result without exposing raw scores."""
        model.focus_conclusion = result.focus.conclusion.value
        model.exposure_conclusion = result.exposure.conclusion.value
        model.face_count = result.face_count
        model.portrait_assessed = result.portrait_assessed

    @staticmethod
    def _clear_technical_result(model: ImageFileModel) -> None:
        """Clear conclusions whenever the underlying result is no longer current."""
        model.focus_conclusion = ""
        model.exposure_conclusion = ""
        model.face_count = 0
        model.portrait_assessed = False

    @staticmethod
    def _serializable_result(result: object | None) -> object | None:
        if result is not None and is_dataclass(result):
            return asdict(result)
        return result

    def _report_persistence_error(
        self,
        action: str,
        error: Exception,
        *,
        show_dialog: bool = True,
    ) -> None:
        logger.exception("%s persistence failed", action)
        detail = f"{type(error).__name__}: {error}"
        self._status_bar.showMessage(
            f"{action}保存失败：{detail}",
            8000,
        )
        if show_dialog:
            QMessageBox.warning(
                self,
                f"{action}保存失败",
                f"项目数据库未能保存本次{action}，界面保持原状态。"
                f"\n\n{detail}",
            )

    def _save_analysis_safely(
        self,
        model: ImageFileModel,
        analyzer: str,
        status: AnalysisStatus,
        result: object | None,
        failure: AnalysisFailure | None,
        *,
        show_dialog: bool = True,
    ) -> bool:
        store = self._project_store
        if store is None:
            return True
        try:
            store.save_analysis(
                model.photo_id,
                analyzer,
                status,
                self._serializable_result(result),
                failure,
            )
        except Exception as error:
            self._report_persistence_error(
                "分析结果",
                error,
                show_dialog=show_dialog,
            )
            return False
        return True

    def _on_analyze_all(self) -> None:
        if self._project_recovery_blocked:
            return
        models = self.all_models
        if not models:
            return

        old_handle = self._analysis_handle
        self._active_job_ids["analysis"] = None
        self._analysis_handle = None
        if old_handle is not None:
            old_handle.cancel()

        eligible = {
            AnalysisStatus.PENDING,
            # A terminal status write can fail after the worker has stopped,
            # leaving the last durable/UI state RUNNING. A new user command
            # must be able to retry that analyzer without reopening.
            AnalysisStatus.RUNNING,
            AnalysisStatus.FAILED,
            AnalysisStatus.CANCELED,
        }
        pending: list[tuple[ImageFileModel, tuple[str, ...]]] = []
        for model in models:
            analyzers = tuple(
                name
                for name in ("technical",)
                if getattr(model, f"{name}_status") in eligible
            )
            if not analyzers:
                continue
            pending.append((model, analyzers))

        store = self._project_store
        if store is not None and pending:
            try:
                store.save_analyses(
                    tuple(
                        (
                            model.photo_id,
                            name,
                            AnalysisStatus.RUNNING,
                            None,
                            None,
                        )
                        for model, analyzers in pending
                        for name in analyzers
                    )
                )
            except Exception as error:
                self._report_persistence_error("分析结果", error)
                self._status_bar.showMessage("分析状态保存失败")
                self._analyze_btn.setEnabled(True)
                return

        requests = []
        for model, analyzers in pending:
            for name in analyzers:
                self._clear_technical_result(model)
                setattr(model, f"{name}_status", AnalysisStatus.RUNNING)
                model.analysis_failures.pop(name, None)
            self._thumbnail_browser.refresh_photo(
                model.photo_id,
                [PhotoRoles.ANALYSIS],
            )
            requests.append(
                PhotoAnalysisRequest(
                    photo_id=model.photo_id,
                    path=model.analysis_path,
                    analyzers=tuple(analyzers),
                )
            )

        if not requests:
            self._status_bar.showMessage("所有照片已完成分析")
            self._analyze_btn.setEnabled(True)
            return
        self._analyze_btn.setEnabled(False)
        handle = self._analysis_coordinator.start(
            self._active_project_id,
            requests,
            self._job_bridge.publish,
        )
        self._analysis_handle = handle
        self._active_job_ids["analysis"] = handle.job_id

    def _on_analysis_result(self, event: AnalysisResult) -> None:
        if not self._is_active_event(
            event.project_id,
            event.job_id,
            "analysis",
        ):
            return
        model = self.model_for_photo_id(event.response.photo_id)
        if model is None:
            return
        apply_result = {"technical": self._apply_technical_result}
        persistence_error_reported = False
        for outcome in event.response.outcomes:
            if not self._save_analysis_safely(
                model,
                outcome.name,
                outcome.status,
                outcome.result,
                outcome.failure,
                show_dialog=not persistence_error_reported,
            ):
                persistence_error_reported = True
                continue
            if outcome.status is AnalysisStatus.SUCCEEDED:
                if outcome.result is not None and outcome.name in apply_result:
                    apply_result[outcome.name](model, outcome.result)
                setattr(
                    model,
                    f"{outcome.name}_status",
                    AnalysisStatus.SUCCEEDED,
                )
                model.analysis_failures.pop(outcome.name, None)
            else:
                if outcome.name == "technical":
                    self._clear_technical_result(model)
                setattr(
                    model,
                    f"{outcome.name}_status",
                    outcome.status,
                )
                if outcome.failure is not None:
                    model.analysis_failures[outcome.name] = outcome.failure
        self._thumbnail_browser.refresh_photo(
            model.photo_id,
            [PhotoRoles.ANALYSIS],
        )

    def _culling_move_can_close(self) -> bool:
        """A stopped, recovery-blocked journal is safe; running work is not."""
        context = self._active_culling_move
        if context is None:
            return True
        wait = getattr(context.handle, "wait", None)
        stopped = wait is not None and wait(0)
        return self._active_culling_move is None or (stopped and context.blocked)

    def closeEvent(self, event: object) -> None:
        if not self._culling_move_can_close():
            event.ignore()
            cancel = getattr(self._active_culling_move.handle, "cancel", None)
            if cancel is not None and self._active_culling_move.rollback_of is None:
                cancel()
            self._status_bar.showMessage("正在安全完成照片移动，完成后关闭", 0)
            QTimer.singleShot(25, self.close)
            return
        preview = getattr(self, "_preview_widget", None)
        if preview is not None:
            preview.shutdown_preview_loader()
        self._culling_workspace.shutdown_preview_loader()
        # Every running move has settled; stopped blocked journals are retained
        # for startup recovery. The owner thread processed commit/rollback.
        self._move_event_processing_enabled = False
        old_import = self._import_handle
        old_analysis = self._analysis_handle
        old_export = self._export_handle
        old_io = tuple(self._io_handles.values())
        old_store = self._project_store
        self._active_project_id = uuid4()
        self._active_job_ids = {
            "import": None,
            "analysis": None,
            "export": None,
        }
        self._import_handle = None
        self._analysis_handle = None
        self._export_handle = None
        self._export_context = None
        self._io_handles = {}
        self._project_store = None
        self._cancel_and_wait_handles(
            (old_import, old_analysis, old_export, *old_io)
        )
        invalidations_submitted = self._flush_thumbnail_invalidations(
            attempts=3
        )
        if old_store is not None:
            old_store.close()
        self._io_coordinator.shutdown(wait=False)
        if self._thumbnail_cache is not None:
            if invalidations_submitted:
                drain_cache = getattr(
                    self._thumbnail_cache,
                    "drain",
                    None,
                )
                if drain_cache is not None:
                    try:
                        drain_cache()
                    except Exception as error:
                        self._thumbnail_cleanup_error = str(error)
                        logger.error(
                            "Thumbnail invalidation cleanup failed during close",
                            exc_info=True,
                        )
            else:
                logger.error(
                    "Thumbnail invalidations remain pending during close: %d",
                    len(self._pending_thumbnail_invalidations),
                )
            close_cache = getattr(self._thumbnail_cache, "close", None)
            if close_cache is not None:
                try:
                    close_cache()
                except Exception as error:
                    self._thumbnail_cleanup_error = str(error)
                    logger.error(
                        "Thumbnail cache close failed",
                        exc_info=True,
                    )
                else:
                    if not self._pending_thumbnail_invalidations:
                        self._thumbnail_cleanup_error = None
        super().closeEvent(event)

    # ---- export ----

    def _on_export(self) -> None:
        all_models = self._thumbnail_browser.all_models()
        if not all_models:
            return

        # Build group info for dialog
        counts = self._thumbnail_browser._compute_group_counts()
        group_info = {k: (GROUPS[k], counts.get(k, 0)) for k in GROUPS}

        dlg = ExportDialog(group_info, self)
        if dlg.exec() != ExportDialog.DialogCode.Accepted:
            return

        dest = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not dest:
            return

        group_key = dlg.selected_group
        prefix = dlg.prefix
        start_num = dlg.start_number

        # Filter models by selected group
        models = [
            model
            for model in all_models
            if ThumbnailBrowser._classify_group(model, group_key)
        ]

        if not models:
            self._status_bar.showMessage("所选分组无照片可导出")
            return

        mode = ExportMode.COPY if dlg.copy_mode else ExportMode.MOVE
        if not self._uses_direct_export_service:
            units = []
            for index, model in enumerate(models):
                items = []
                for source_path in model.source_paths:
                    source = Path(source_path)
                    target_name = (
                        f"{prefix}_{start_num + index:03d}{source.suffix}"
                        if prefix
                        else source.name
                    )
                    items.append(ExportItem(source, target_name))
                units.append(ExportUnit(model.photo_id, tuple(items)))
            request = ExportRequest(
                destination=Path(dest),
                items=(),
                units=tuple(units),
                mode=mode,
                conflict=dlg.conflict_policy,
            )
            old_handle = self._export_handle
            self._active_job_ids["export"] = None
            self._export_handle = None
            self._export_context = {
                "mode": mode,
                "photo_ids": tuple(model.photo_id for model in models),
                "destination": Path(dest),
            }
            if old_handle is not None:
                old_handle.cancel()
            self._export_btn.setEnabled(False)
            try:
                handle = self._io_coordinator.submit_export(
                    self._active_project_id,
                    request,
                    self._job_bridge.publish,
                )
            except Exception as error:
                self._active_job_ids["export"] = None
                self._export_handle = None
                self._export_context = None
                self._export_btn.setEnabled(True)
                self._status_bar.showMessage(
                    f"无法启动导出：{type(error).__name__}: {error}"
                )
                return
            self._export_handle = handle
            self._active_job_ids["export"] = handle.job_id
            return

        self._run_legacy_export(
            models,
            Path(dest),
            prefix,
            start_num,
            mode,
            dlg.conflict_policy,
        )

    def _run_legacy_export(
        self,
        models: list[ImageFileModel],
        destination: Path,
        prefix: str,
        start_num: int,
        mode: ExportMode,
        conflict: object,
    ) -> None:
        progress = QProgressDialog("导出中...", "取消", 0, len(models), self)
        progress.setWindowTitle("导出进度")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)

        items: list[ExportItem] = []
        for index, model in enumerate(models):
            source = Path(model.path)
            if prefix:
                target_name = (
                    f"{prefix}_{start_num + index:03d}{source.suffix}"
                )
            else:
                target_name = source.name
            items.append(ExportItem(source, target_name))
        request = ExportRequest(
            destination=destination,
            items=tuple(items),
            mode=mode,
            conflict=conflict,
        )

        def is_canceled() -> bool:
            QApplication.processEvents()
            return progress.wasCanceled()

        report = self._export_service.export(request, is_canceled)

        progress.setValue(min(len(report.results), len(models)))
        progress.close()
        self._consume_export_report(
            report,
            mode,
            {model.photo_id: model for model in models},
            destination,
        )

    def _on_export_started(self, event: ExportStarted) -> None:
        if self._is_active_event(
            event.project_id,
            event.job_id,
            "export",
        ):
            self._status_bar.showMessage("导出中…")

    def _on_export_progress(self, event: ExportProgress) -> None:
        if self._is_active_event(
            event.project_id,
            event.job_id,
            "export",
        ):
            self._status_bar.showMessage(
                f"导出中… {event.completed}/{event.total}"
            )

    def _on_export_finished(self, event: ExportFinished) -> None:
        if not self._is_active_event(
            event.project_id,
            event.job_id,
            "export",
        ):
            return
        context = self._export_context
        self._active_job_ids["export"] = None
        self._export_handle = None
        self._export_context = None
        self._export_btn.setEnabled(True)
        if event.report is None or context is None:
            self._status_bar.showMessage(
                f"导出失败：{event.error or '未知错误'}"
            )
            if event.recovery_paths or event.notes:
                details = [event.error or "导出发生严重异常"]
                details.extend(
                    f"恢复位置: {path}"
                    for path in event.recovery_paths
                )
                details.extend(event.notes)
                QMessageBox.critical(
                    self,
                    "导出需要人工处理",
                    "\n".join(details),
                )
            return
        models_by_id = {
            photo_id: model
            for photo_id in context["photo_ids"]
            if (model := self.model_for_photo_id(photo_id)) is not None
        }
        self._consume_export_report(
            event.report,
            context["mode"],
            models_by_id,
            context["destination"],
        )

    def _consume_export_report(
        self,
        report: object,
        mode: ExportMode,
        models_by_id: dict[str, ImageFileModel],
        destination: Path,
    ) -> None:
        unit_results = report.unit_results
        moved_models: dict[str, ImageFileModel] = {}
        if mode is ExportMode.MOVE and unit_results:
            for unit_result in unit_results:
                model = models_by_id.get(unit_result.unit_id)
                if model is not None and unit_result.success:
                    moved_models[model.photo_id] = model
        elif mode is ExportMode.MOVE:
            models_by_source = {
                Path(model.path): model
                for model in models_by_id.values()
            }
            for result in report.results:
                if result.success and result.source in models_by_source:
                    model = models_by_source[result.source]
                    moved_models[model.photo_id] = model

        mark_persistence_error = False
        if moved_models and self._project_store is not None:
            try:
                self._project_store.save_marks(
                    tuple(
                        (photo_id, "已移出")
                        for photo_id in moved_models
                    )
                )
            except Exception as error:
                mark_persistence_error = True
                self._report_persistence_error("导出标记", error)
        if not mark_persistence_error:
            for model in moved_models.values():
                model.mark = "已移出"

        self._thumbnail_browser.refresh_badges()
        self._update_status_bar()
        if unit_results:
            succeeded = sum(result.success for result in unit_results)
            failed = len(unit_results) - succeeded
        else:
            succeeded = sum(result.success for result in report.results)
            failed = len(report.results) - succeeded
        action = "复制" if mode is ExportMode.COPY else "移动"
        message = f"已{action} {succeeded} 张照片到 {destination}"
        if failed:
            message += f"；{failed} 张失败"
        if report.canceled:
            message += "；已取消"
        if report.stopped_reason:
            message += f"；{report.stopped_reason}"
        if mark_persistence_error:
            message += "；导出完成但标记保存失败"
        self._status_bar.showMessage(message)

        recovery_results = [
            result
            for result in report.results
            if result.recovery_path is not None
        ]
        unit_recovery = [
            (unit_result, path)
            for unit_result in unit_results
            for path in unit_result.recovery_paths
        ]
        if recovery_results or unit_recovery:
            details = []
            for result in recovery_results:
                details.append(
                    "\n".join(
                        (
                            f"源文件: {result.source}",
                            f"目标文件: {result.target or '—'}",
                            f"恢复位置: {result.recovery_path}",
                            f"错误: {result.error or '未知错误'}",
                        )
                    )
                )
            for unit_result, path in unit_recovery:
                details.append(
                    "\n".join(
                        (
                            f"照片单元: {unit_result.unit_id}",
                            f"恢复位置: {path}",
                            f"错误: {unit_result.error or '清理失败'}",
                        )
                    )
                )
            QMessageBox.critical(
                self,
                "导出需要人工处理",
                "以下文件导出异常；照片数据或待清理文件位于恢复位置，"
                "请核对后再处理：\n\n"
                + "\n\n".join(details),
            )

    # ---- thumbnail size ----

    @staticmethod
    def _size_text(value: int) -> str:
        if value < 140:
            return "小"
        if value < 180:
            return "中"
        return "大"

    def _on_thumbnail_size_changed(self, value: int) -> None:
        self._size_label.setText(self._size_text(value))
        self._thumbnail_browser.set_thumbnail_size(value)

    def _on_browser_zoom(self, new_size: int) -> None:
        self._size_slider.blockSignals(True)
        self._size_slider.setValue(new_size)
        self._size_slider.blockSignals(False)
        self._size_label.setText(self._size_text(new_size))

    # ---- keyboard manager (set externally) ----

    def install_keyboard_manager(self, km: object) -> None:
        self._keyboard_manager = km
        km.install_on(self, self._thumbnail_browser)

    # ---- preview access ----

    def _refresh_culling_eligibility(self) -> None:
        """Enable culling only for an available, fully terminal project."""
        models = self._thumbnail_browser.all_models()
        project_available = (
            self._project_store is not None
            and self._active_project_id is not None
            and self._active_job_ids["import"] is None
            and not self._project_recovery_blocked
        )
        self._cull_btn.setEnabled(
            project_available
            and bool(models)
            and all(model.has_terminal_technical_status for model in models)
        )

    @property
    def culling_move_pending(self) -> bool:
        return self._active_culling_move is not None

    def _set_culling_move_pending(self, pending: bool) -> None:
        self._culling_workspace.set_move_pending(pending)
        self._toolbar.setEnabled(not pending)
        if not pending:
            self._culling_workspace.set_undo_available(
                self._undo_command is not None
            )

    def _culling_model_matches_record(
        self,
        model: ImageFileModel,
        record: PhotoRecord,
    ) -> bool:
        return (
            model.photo_id == record.id
            and normalize_legacy_mark(model.mark)
            is normalize_legacy_mark(record.mark)
            and model.source_paths
            == tuple(
                asset.fingerprint.path for asset in record.source_assets
            )
        )

    def _normal_culling_target(
        self,
        photo_id: str,
        mark: PhotoMark,
    ) -> tuple[ProjectStore, PhotoRecord, ImageFileModel] | None:
        if (
            not isinstance(photo_id, str)
            or not photo_id
            or not isinstance(mark, PhotoMark)
            or self._active_culling_move is not None
            or self._project_recovery_blocked
            or not self._move_event_processing_enabled
            or self._stack.currentWidget() is not self._culling_workspace
            or self._active_project_id is None
            or self._project_store is None
            or self._culling_session is None
            or self._culling_session.current_id != photo_id
            or self._culling_workspace.current_photo_id != photo_id
            or photo_id not in self._culling_session.visible_ids()
        ):
            return None
        model = self.model_for_photo_id(photo_id)
        if model is None or model.photo_id != photo_id:
            return None
        try:
            record = self._project_store.get_photo(photo_id)
        except Exception:
            logger.exception("Failed to load culling move source record")
            self._status_bar.showMessage("无法读取照片移动状态", 8000)
            return None
        if (
            record is None
            or not self._culling_model_matches_record(model, record)
            or normalize_legacy_mark(record.mark) is mark
        ):
            return None
        return self._project_store, record, model

    def request_culling_mark(
        self,
        photo_id: str,
        mark: PhotoMark,
    ) -> None:
        """Persist a culling decision without changing source files."""
        target = self._normal_culling_target(photo_id, mark)
        if target is None:
            return
        store, record, model = target
        before_mark = normalize_legacy_mark(record.mark)
        if before_mark is None:
            return
        try:
            store.save_marks(((photo_id, mark.value),))
        except Exception as error:
            self._report_persistence_error("照片标记", error)
            return
        model.mark = mark.value
        self._thumbnail_browser.refresh_photo(
            photo_id,
            [PhotoRoles.MARK],
        )
        self._undo_command = _MarkUndoCommand(
            photo_id,
            before_mark,
            mark,
        )
        if self._culling_session is not None:
            self._culling_session.advance_after_mark(photo_id)
        self._refresh_or_complete_culling()
        self._culling_workspace.set_undo_available(True)
        self._update_status_bar()
        self._status_bar.showMessage(
            "照片标记已保存；原文件未移动",
            5000,
        )

    def request_culling_undo(self) -> None:
        """Restore the previous database-only culling mark once."""
        command = self._undo_command
        store = self._project_store
        if (
            not isinstance(command, _MarkUndoCommand)
            or self._active_culling_move is not None
            or self._project_recovery_blocked
            or not self._move_event_processing_enabled
            or self._stack.currentWidget() is not self._culling_workspace
            or self._active_project_id is None
            or store is None
            or self._culling_session is None
        ):
            return
        model = self.model_for_photo_id(command.photo_id)
        if model is None or model.photo_id != command.photo_id:
            return
        try:
            record = store.get_photo(command.photo_id)
        except Exception:
            logger.exception("Failed to load culling undo source record")
            self._status_bar.showMessage("无法读取撤销状态", 8000)
            return
        if (
            record is None
            or normalize_legacy_mark(record.mark) is not command.after_mark
            or not self._culling_model_matches_record(model, record)
        ):
            self._status_bar.showMessage("当前照片状态已变化，无法撤销", 8000)
            return
        try:
            store.save_marks(
                ((command.photo_id, command.before_mark.value),)
            )
        except Exception as error:
            self._report_persistence_error("撤销标记", error)
            return
        model.mark = command.before_mark.value
        self._thumbnail_browser.refresh_photo(
            command.photo_id,
            [PhotoRoles.MARK],
        )
        self._undo_command = None
        session = self._culling_session
        if session is not None:
            if command.photo_id not in session.visible_ids():
                session.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)
            session.select_photo_id(command.photo_id)
        self._refresh_or_complete_culling()
        self._culling_workspace.set_undo_available(False)
        self._update_status_bar()
        self._status_bar.showMessage(
            "已撤销照片标记；原文件未移动",
            5000,
        )

    def _submit_culling_move(
        self,
        store: ProjectStore,
        record: PhotoRecord,
        model: ImageFileModel,
        target_mark: PhotoMark,
        *,
        is_undo: bool,
        undo_command: UndoCommand | None,
    ) -> None:
        try:
            plan = (
                self._photo_move_service.prepare_inverse(undo_command.plan)
                if is_undo and undo_command is not None
                else self._photo_move_service.prepare_plan(record, target_mark)
            )
        except Exception:
            logger.info("Culling move planning was rejected", exc_info=True)
            self._status_bar.showMessage(
                "无法移动照片；请检查目标位置是否可用",
                8000,
            )
            return
        if (
            self._active_culling_move is not None
            or store is not self._project_store
            or self._active_project_id is None
            or plan.photo_id != record.id
        ):
            return

        context = _ActiveCullingMove(
            project_id=self._active_project_id,
            store=store,
            plan=plan,
            before_record=record,
            model=model,
            is_undo=is_undo,
            undo_command=undo_command,
        )
        self._active_culling_move = context
        self._set_culling_move_pending(True)
        try:
            store.begin_photo_move(plan)
        except Exception:
            logger.exception("Failed to persist culling move journal")
            self._active_culling_move = None
            self._set_culling_move_pending(False)
            self._status_bar.showMessage("无法开始照片移动", 8000)
            return

        try:
            handle = self._move_coordinator.start(
                self._active_project_id,
                plan,
                self._job_bridge.publish,
            )
        except MoveStartRejected as error:
            logger.exception("Failed to start culling move worker")
            context.handle = error.handle
            context.job_id = error.handle.job_id
            try:
                store.abort_photo_move(plan.id)
            except Exception:
                logger.exception("Failed to abort unstarted culling move")
                self._block_active_culling_move("照片移动需要恢复后才能继续")
                return
            self._active_culling_move = None
            self._set_culling_move_pending(False)
            self._status_bar.showMessage("无法启动照片移动", 8000)
            return
        except Exception:
            # An arbitrary coordinator error is not proof that no worker ran.
            logger.exception("Unknown culling move worker launch outcome")
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return

        job_id = getattr(handle, "job_id", None)
        if job_id is None:
            context.handle = handle
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        context.handle = handle
        context.job_id = job_id
        self._status_bar.showMessage("正在安全移动照片…")

    def navigate_culling(self, offset: int) -> None:
        if (
            self._active_culling_move is not None
            or self._project_recovery_blocked
            or self._stack.currentWidget() is not self._culling_workspace
            or self._culling_session is None
        ):
            return
        try:
            self._culling_session.navigate(offset)
        except (TypeError, ValueError):
            return
        self._culling_workspace.refresh_current()

    def _matching_culling_move(
        self,
        event: MoveStarted | MoveItemCompleted | MoveFinished | MovePreflightRejected,
    ) -> _ActiveCullingMove | None:
        context = self._active_culling_move
        if (
            not self._move_event_processing_enabled
            or context is None
            or context.job_id is None
            or event.project_id != context.project_id
            or event.job_id != context.job_id
            or self._active_project_id != context.project_id
            or self._project_store is not context.store
        ):
            return None
        return context

    def _on_move_started(self, event: MoveStarted) -> None:
        if self._matching_culling_move(event) is not None:
            self._status_bar.showMessage("正在安全移动照片…")

    def _on_move_preflight_rejected(self, event: MovePreflightRejected) -> None:
        context = self._matching_culling_move(event)
        if context is None or context.blocked:
            return
        if context.rollback_of is not None:
            self._block_active_culling_move("文件回滚受阻，请重新打开项目恢复")
            return
        if context.completed_ordinals or not isinstance(event.error, str) or not event.error:
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        try:
            context.store.abort_photo_move(context.plan.id)
        except Exception:
            logger.exception("Failed to abort a rejected preflight")
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        self._active_culling_move = None
        self._set_culling_move_pending(False)
        self._status_bar.showMessage("照片未移动；请检查文件和目标位置后重试", 8000)

    def _on_move_item_completed(self, event: MoveItemCompleted) -> None:
        context = self._matching_culling_move(event)
        if context is None or context.blocked:
            return
        expected = {item.ordinal for item in context.plan.items}
        if (
            type(event.ordinal) is not int
            or event.ordinal not in expected
        ):
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        if context.rollback_of is not None:
            # Original forward journal already records every completed item.
            # Keeping it unchanged preserves crash recovery during reversal.
            context.completed_ordinals.add(event.ordinal)
            return
        try:
            context.store.mark_move_item_completed(
                context.plan.id,
                event.ordinal,
            )
        except Exception:
            logger.exception("Failed to persist culling move progress")
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        context.completed_ordinals.add(event.ordinal)

    @staticmethod
    def _success_evidence_is_complete(
        context: _ActiveCullingMove,
        event: MoveFinished,
    ) -> bool:
        expected_ordinals = tuple(item.ordinal for item in context.plan.items)
        if (
            not isinstance(event.completed_ordinals, tuple)
            or not all(
                type(ordinal) is int
                for ordinal in event.completed_ordinals
            )
            or event.completed_ordinals != expected_ordinals
            or context.completed_ordinals != set(expected_ordinals)
            or not isinstance(event.recovery_paths, tuple)
            or bool(event.recovery_paths)
            or not isinstance(event.unresolved_ordinals, tuple)
            or bool(event.unresolved_ordinals)
            or event.rollback_complete is not None
            or not isinstance(event.rolled_back_ordinals, tuple)
            or bool(event.rolled_back_ordinals)
            or event.error is not None
            or not isinstance(event.moved_assets, tuple)
            or len(event.moved_assets) != len(context.plan.items)
        ):
            return False
        before_assets = {
            before.kind: before
            for before in context.before_record.source_assets
        }
        for item, asset in zip(
            context.plan.items,
            event.moved_assets,
            strict=True,
        ):
            if not isinstance(asset, PhotoAssetRecord):
                return False
            before = before_assets.get(item.kind)
            if (
                before is None
                or asset.kind is not item.kind
                or asset.fingerprint.path != item.target_path
                or asset.fingerprint.size != item.size
                or asset.fingerprint.mtime_ns != item.mtime_ns
                or asset.origin_path != before.origin_path
            ):
                return False
        return True

    @staticmethod
    def _committed_record_matches_plan(
        record: PhotoRecord,
        plan: PhotoMoveTransaction,
    ) -> bool:
        if (
            record.id != plan.photo_id
            or normalize_legacy_mark(record.mark) is not plan.after_mark
            or len(record.source_assets) != len(plan.items)
        ):
            return False
        assets_by_kind = {asset.kind: asset for asset in record.source_assets}
        for item in plan.items:
            asset = assets_by_kind.get(item.kind)
            if (
                asset is None
                or asset.fingerprint.path != item.target_path
                or asset.fingerprint.size != item.size
                or asset.fingerprint.mtime_ns != item.mtime_ns
            ):
                return False
        return True

    @staticmethod
    def _apply_committed_record(
        model: ImageFileModel,
        record: PhotoRecord,
    ) -> None:
        model.jpeg_path = (
            "" if record.jpeg is None else record.jpeg.fingerprint.path
        )
        model.raw_path = (
            "" if record.raw is None else record.raw.fingerprint.path
        )
        model.path = model.analysis_path
        model.filename = record.filename
        model.is_raw = bool(model.raw_path and not model.jpeg_path)
        model.mark = record.mark

    def _on_move_finished(self, event: MoveFinished) -> None:
        context = self._matching_culling_move(event)
        if context is None or context.blocked:
            return
        if event.success is False:
            self._handle_failed_culling_move(context, event)
            return
        if event.success is not True:
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        if not self._success_evidence_is_complete(context, event):
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        if context.rollback_of is not None:
            self._finish_persistence_rollback(context, event)
            return
        try:
            context.store.commit_photo_move(
                context.plan.id,
                event.moved_assets,
            )
            committed = context.store.get_photo(context.plan.photo_id)
        except Exception:
            logger.exception("Failed to commit completed culling move")
            self._start_persistence_rollback(context)
            return
        if (
            committed is None
            or not self._committed_record_matches_plan(
                committed,
                context.plan,
            )
            or self.model_for_photo_id(context.plan.photo_id) is not context.model
        ):
            self._block_active_culling_move("照片状态同步失败，请重新打开项目")
            return

        self._pending_thumbnail_invalidations.update(
            asset.fingerprint for asset in context.before_record.source_assets
        )
        self._apply_committed_record(context.model, committed)
        self._flush_thumbnail_invalidations()
        self._thumbnail_browser.refresh_photo(
            context.plan.photo_id,
            [
                PhotoRoles.PATH,
                PhotoRoles.FILENAME,
                PhotoRoles.MARK,
                PhotoRoles.ASSET_KIND,
            ],
        )
        self._thumbnail_browser._view.viewport().update()
        self._update_status_bar()

        if context.is_undo:
            if self._undo_command == context.undo_command:
                self._undo_command = None
            if self._culling_session is not None:
                session = self._culling_session
                if context.plan.photo_id not in session.visible_ids():
                    session.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)
                session.select_photo_id(context.plan.photo_id)
        else:
            self._undo_command = UndoCommand(
                context.plan.photo_id,
                context.plan.before_mark,
                context.plan.after_mark,
                context.before_record,
                committed,
                context.plan,
            )
            if self._culling_session is not None:
                self._culling_session.advance_after_mark(
                    context.plan.photo_id
                )
        self._refresh_or_complete_culling()
        self._active_culling_move = None
        self._set_culling_move_pending(False)
        self._status_bar.showMessage("照片标记已安全保存", 5000)

    def _start_persistence_rollback(self, context: _ActiveCullingMove) -> None:
        """Reverse only a proven non-commit, retaining the original journal."""
        try:
            if not context.store.is_photo_move_uncommitted(context.plan, context.before_record):
                self._block_active_culling_move("照片保存结果未能确认，请重新打开项目")
                return
            inverse = self._photo_move_service.prepare_inverse(context.plan)
            rollback = _ActiveCullingMove(
                project_id=context.project_id, store=context.store,
                plan=inverse, before_record=context.before_record,
                model=context.model, is_undo=context.is_undo,
                undo_command=context.undo_command, rollback_of=context.plan,
            )
            self._active_culling_move = rollback
            handle = self._move_coordinator.start(
                context.project_id, inverse, self._job_bridge.publish,
            )
            rollback.handle = handle
            rollback.job_id = getattr(handle, "job_id", None)
            if rollback.job_id is None:
                self._block_active_culling_move("文件回滚需要恢复后才能继续")
                return
            self._status_bar.showMessage("保存失败，正在安全恢复原文件…", 0)
        except Exception:
            logger.exception("Unable to establish or start safe persistence rollback")
            self._active_culling_move = context
            self._block_active_culling_move("照片移动需要恢复后才能继续")

    def _finish_persistence_rollback(
        self, context: _ActiveCullingMove, event: MoveFinished,
    ) -> None:
        original = context.rollback_of
        try:
            if (
                original is None
                or event.moved_assets != context.before_record.source_assets
                or not context.store.is_photo_move_uncommitted(original, context.before_record)
            ):
                self._block_active_culling_move("文件回滚状态未能确认，请重新打开项目")
                return
            context.store.abort_photo_move(original.id)
        except Exception:
            logger.exception("Failed to settle verified persistence rollback")
            self._block_active_culling_move("文件已回滚，项目需要恢复后才能继续")
            return
        self._active_culling_move = None
        self._set_culling_move_pending(False)
        self._status_bar.showMessage("保存失败，文件已恢复，当前标记保持不变", 8000)

    def _handle_failed_culling_move(
        self,
        context: _ActiveCullingMove,
        event: MoveFinished,
    ) -> None:
        if context.rollback_of is not None:
            self._block_active_culling_move("文件回滚受阻，请重新打开项目恢复")
            return
        if not self._failure_evidence_confirms_rollback(context, event):
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        try:
            context.store.abort_photo_move(context.plan.id)
        except Exception:
            logger.exception("Failed to abort rolled-back culling move")
            self._block_active_culling_move("照片移动需要恢复后才能继续")
            return
        self._active_culling_move = None
        self._set_culling_move_pending(False)
        self._status_bar.showMessage("照片未移动，当前标记保持不变", 8000)

    @staticmethod
    def _failure_evidence_confirms_rollback(
        context: _ActiveCullingMove,
        event: MoveFinished,
    ) -> bool:
        if (
            event.rollback_complete is not True
            or not isinstance(event.recovery_paths, tuple)
            or bool(event.recovery_paths)
            or not isinstance(event.unresolved_ordinals, tuple)
            or bool(event.unresolved_ordinals)
            or not isinstance(event.completed_ordinals, tuple)
            or not isinstance(event.rolled_back_ordinals, tuple)
            or not isinstance(event.moved_assets, tuple)
            or bool(event.moved_assets)
            or not isinstance(event.error, str)
            or not event.error
        ):
            return False

        completed = event.completed_ordinals
        rolled_back = event.rolled_back_ordinals
        expected = tuple(item.ordinal for item in context.plan.items)
        if (
            any(type(ordinal) is not int for ordinal in completed)
            or any(type(ordinal) is not int for ordinal in rolled_back)
            or len(set(completed)) != len(completed)
            or len(set(rolled_back)) != len(rolled_back)
            or completed != expected[: len(completed)]
            or context.completed_ordinals != set(completed)
        ):
            return False

        legitimate_rollbacks = {tuple(reversed(completed))}
        if len(completed) < len(expected):
            legitimate_rollbacks.add(
                tuple(reversed(expected[: len(completed) + 1]))
            )
        return rolled_back in legitimate_rollbacks

    def _block_active_culling_move(self, message: str) -> None:
        context = self._active_culling_move
        if context is not None:
            context.blocked = True
        self._project_recovery_blocked = True
        self._set_culling_move_pending(True)
        self._refresh_culling_eligibility()
        self._status_bar.showMessage(message, 0)

    def enter_culling(self) -> None:
        """Enter culling with a fresh, capture-order stable-ID session."""
        self._refresh_culling_eligibility()
        if not self._cull_btn.isEnabled():
            return
        store = self._project_store
        if store is None or self._active_project_id is None:
            return
        photo_ids = tuple(
            model.photo_id
            for model in self._thumbnail_browser.all_models()
        )
        self._culling_session = CullingSession(
            photo_ids,
            self._thumbnail_browser.model_for_photo_id,
        )
        self._culling_workspace.set_session(self._culling_session)
        self._culling_workspace.set_undo_available(
            self._undo_command is not None
        )
        self._stack.setCurrentWidget(self._culling_workspace)
        self._refresh_or_complete_culling()
        self._culling_workspace.setFocus()

    def _refresh_or_complete_culling(self) -> None:
        session = self._culling_session
        if session is None:
            self._culling_workspace.refresh_current()
            return
        if session.is_complete and not session.visible_ids():
            self._culling_workspace.show_summary(session.counts())
            return
        self._culling_workspace.refresh_current()

    def _review_culling_status(self, status_filter: StatusFilter) -> None:
        if (
            self._active_culling_move is not None
            or self._stack.currentWidget() is not self._culling_workspace
            or self._culling_session is None
            or status_filter not in {StatusFilter.HOLD, StatusFilter.DISCARD}
        ):
            return
        self._culling_workspace.set_filters(
            status_filter,
            AnalysisFilter.ALL,
        )

    def _open_culling_project_root(self) -> None:
        if (
            self._active_culling_move is not None
            or self._stack.currentWidget() is not self._culling_workspace
            or self._project_store is None
        ):
            return
        try:
            root_path = self._project_store.root_path
        except Exception:
            logger.exception("Failed to read culling project root")
            self._status_bar.showMessage("无法读取照片目录", 8000)
            return
        if not open_project_root(root_path):
            self._status_bar.showMessage("无法打开照片目录", 8000)

    def exit_culling(self) -> None:
        """Return to browse without destroying its filters or selection."""
        if self._active_culling_move is not None:
            return
        self._culling_workspace.invalidate_preview_requests()
        self._stack.setCurrentWidget(self._thumbnail_browser)
        self._thumbnail_browser.refresh_badges()
        self._thumbnail_browser.setFocus()
        self._update_status_bar()

    def model_for_photo_id(self, photo_id: str) -> ImageFileModel | None:
        return self._thumbnail_browser.model_for_photo_id(photo_id)

    def current_row_for_photo_id(self, photo_id: str) -> int:
        return self._thumbnail_browser._photo_list_model.row_for_id(photo_id)

    def _photo_id_for_current_row(self, row: int) -> str | None:
        return self._thumbnail_browser._photo_list_model.photo_id_at(row)

    def enter_preview_photo(self, photo_id: str) -> None:
        """Switch to preview for a stable ID in the current filtered view."""
        index = self.current_row_for_photo_id(photo_id)
        model = self.model_for_photo_id(photo_id)
        if index < 0 or model is None:
            return
        from src.xuanpian.ui.preview_widget import PreviewWidget

        if not hasattr(self, "_preview_widget") or self._preview_widget is None:
            self._preview_widget = PreviewWidget(self)
            self._stack.addWidget(self._preview_widget)

        self._preview_widget.load_photo(
            photo_id,
            self._thumbnail_browser.current_photo_ids,
            self.model_for_photo_id,
            photo_list_model=self._thumbnail_browser._photo_list_model,
            burst_group_provider=self._thumbnail_browser.burst_member_ids,
        )
        self._stack.setCurrentWidget(self._preview_widget)
        self._preview_widget.setFocus()

    def enter_preview(self, index: int) -> None:
        """Compatibility adapter from an immediate current-view row."""
        photo_id = self._photo_id_for_current_row(index)
        if photo_id is not None:
            self.enter_preview_photo(photo_id)

    def exit_preview(self) -> None:
        """Return to thumbnail grid."""
        self._stack.setCurrentWidget(self._thumbnail_browser)
        self._thumbnail_browser.refresh_badges()
        self._thumbnail_browser.setFocus()
        self._update_status_bar()

    def current_models(self) -> list[ImageFileModel]:
        return self._thumbnail_browser.current_models()

    @property
    def all_models(self) -> list[ImageFileModel]:
        return self._thumbnail_browser.all_models()

    def mark_current(self, index: int, mark: str) -> None:
        """Compatibility adapter from an immediate current-view row."""
        photo_id = self._photo_id_for_current_row(index)
        if photo_id is not None:
            self.mark_photo(photo_id, mark)

    def mark_photo(self, photo_id: str, mark: str) -> bool:
        return self.mark_photos((photo_id,), mark)

    def mark_photos(
        self,
        photo_ids: tuple[str, ...] | list[str],
        mark: str,
    ) -> bool:
        models = [
            model
            for photo_id in dict.fromkeys(photo_ids)
            if (model := self.model_for_photo_id(photo_id)) is not None
        ]
        if not models:
            return False
        if self._project_store is not None:
            try:
                self._project_store.save_marks(
                    tuple((model.photo_id, mark) for model in models)
                )
            except Exception as error:
                self._report_persistence_error("照片标记", error)
                return False
        for model in models:
            model.mark = mark
            self._thumbnail_browser.refresh_photo(
                model.photo_id,
                [PhotoRoles.MARK],
            )
        self._thumbnail_browser._view.viewport().update()
        self._update_status_bar()
        return True

    def delete_current(self, index: int) -> None:
        """Compatibility adapter from an immediate current-view row."""
        photo_id = self._photo_id_for_current_row(index)
        if photo_id is not None:
            self.delete_photo(photo_id)

    def delete_photo(self, photo_id: str) -> None:
        """Confirm and remove one stable photo identity, database first."""
        model = self.model_for_photo_id(photo_id)
        if model is None:
            return
        reply = QMessageBox.question(
            self,
            "确认从项目移除",
            "确定要从当前项目移除此照片吗？\n"
            f"{model.filename}\n\n"
            "这会删除项目内的标记和分析记录，"
            "但不会删除磁盘上的 JPEG/RAW 源文件。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if self._project_store is not None:
            try:
                self._project_store.delete_photos((photo_id,))
            except Exception as error:
                self._report_persistence_error("删除照片", error)
                return
        self._thumbnail_browser.remove_models([model])
        self._update_status_bar()

    def select_all(self) -> None:
        self._thumbnail_browser.select_all()

    @property
    def thumbnail_browser(self) -> ThumbnailBrowser:
        return self._thumbnail_browser
