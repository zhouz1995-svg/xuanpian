from __future__ import annotations

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from src.xuanpian.tasks.analysis_coordinator import AnalysisResult
from src.xuanpian.tasks.contracts import JobFinished, JobProgress
from src.xuanpian.tasks.import_coordinator import ImportBatch
from src.xuanpian.tasks.io_coordinator import (
    ExportFinished,
    ExportProgress,
    ExportStarted,
    MetadataResult,
)
from src.xuanpian.tasks.move_coordinator import (
    MoveFinished,
    MoveItemCompleted,
    MovePreflightRejected,
    MoveStarted,
)


class QtJobBridge(QObject):
    """Route worker events onto the object's Qt thread."""

    batch = pyqtSignal(object)
    result = pyqtSignal(object)
    metadata = pyqtSignal(object)
    export_started = pyqtSignal(object)
    export_progress = pyqtSignal(object)
    export_finished = pyqtSignal(object)
    move_started = pyqtSignal(object)
    move_item_completed = pyqtSignal(object)
    move_finished = pyqtSignal(object)
    move_preflight_rejected = pyqtSignal(object)
    progress = pyqtSignal(object)
    finished = pyqtSignal(object)
    _incoming = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._incoming.connect(
            self._route,
            Qt.ConnectionType.QueuedConnection,
        )

    def publish(self, event: object) -> None:
        self._incoming.emit(event)

    @pyqtSlot(object)
    def _route(self, event: object) -> None:
        if isinstance(event, ImportBatch):
            self.batch.emit(event)
        elif isinstance(event, AnalysisResult):
            self.result.emit(event)
        elif isinstance(event, MetadataResult):
            self.metadata.emit(event)
        elif isinstance(event, ExportStarted):
            self.export_started.emit(event)
        elif isinstance(event, ExportProgress):
            self.export_progress.emit(event)
        elif isinstance(event, ExportFinished):
            self.export_finished.emit(event)
        elif isinstance(event, MoveStarted):
            self.move_started.emit(event)
        elif isinstance(event, MoveItemCompleted):
            self.move_item_completed.emit(event)
        elif isinstance(event, MoveFinished):
            self.move_finished.emit(event)
        elif isinstance(event, MovePreflightRejected):
            self.move_preflight_rejected.emit(event)
        elif isinstance(event, JobProgress):
            self.progress.emit(event)
        elif isinstance(event, JobFinished):
            self.finished.emit(event)
