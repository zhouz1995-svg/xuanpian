from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QDialog

from src.xuanpian.services.export_service import (
    ConflictPolicy,
    ExportItem,
    ExportItemResult,
    ExportMode,
    ExportReport,
    ExportRequest,
)
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui import main_window
from src.xuanpian.ui.main_window import MainWindow


class _AcceptedMoveDialog:
    DialogCode = QDialog.DialogCode

    def __init__(self, groups: dict[str, tuple[str, int]], parent: object) -> None:
        self.groups = groups

    def exec(self) -> QDialog.DialogCode:
        return self.DialogCode.Accepted

    selected_group = "all"
    copy_mode = False
    prefix = "selected"
    start_number = 7
    conflict_policy = ConflictPolicy.UNIQUE


class _ProgressDialog:
    def __init__(self, *args: object) -> None:
        self.value = 0
        self.cancel_checks = 0
        self.closed = False

    def setWindowTitle(self, title: str) -> None:
        pass

    def setWindowModality(self, modality: object) -> None:
        pass

    def setMinimumDuration(self, duration: int) -> None:
        pass

    def setValue(self, value: int) -> None:
        self.value = value

    def close(self) -> None:
        self.closed = True

    def wasCanceled(self) -> bool:
        self.cancel_checks += 1
        return False


class _FakeExportService:
    def __init__(self, report: ExportReport) -> None:
        self.report = report
        self.requests: list[ExportRequest] = []
        self.observed_cancel = False

    def export(self, request: ExportRequest, is_canceled) -> ExportReport:
        self.requests.append(request)
        self.observed_cancel = is_canceled()
        return self.report


def test_on_export_routes_request_through_service_and_consumes_report(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.png"
    destination = tmp_path / "exported"
    first = ImageFileModel(str(first_path))
    second = ImageFileModel(str(second_path))
    report = ExportReport(
        results=(
            ExportItemResult(
                source=first_path,
                target=destination / "selected_007.jpg",
                success=True,
                error=None,
            ),
            ExportItemResult(
                source=second_path,
                target=destination / "selected_008.png",
                success=False,
                error="destination is read-only",
            ),
        ),
        canceled=True,
    )
    service = _FakeExportService(report)
    window = MainWindow(export_service=service)
    window._thumbnail_browser.set_models([first, second])

    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedMoveDialog)
    monkeypatch.setattr(main_window, "QProgressDialog", _ProgressDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(destination),
    )

    def unsafe_ui_file_operation(*args: object, **kwargs: object) -> None:
        raise AssertionError("MainWindow must not perform export file operations")

    monkeypatch.setattr(Path, "read_bytes", unsafe_ui_file_operation)
    monkeypatch.setattr(Path, "write_bytes", unsafe_ui_file_operation)
    monkeypatch.setattr(Path, "unlink", unsafe_ui_file_operation)

    window._on_export()

    assert service.requests == [
        ExportRequest(
            destination=destination,
            items=(
                ExportItem(first_path, "selected_007.jpg"),
                ExportItem(second_path, "selected_008.png"),
            ),
            mode=ExportMode.MOVE,
            conflict=ConflictPolicy.UNIQUE,
        )
    ]
    assert service.observed_cancel is False
    assert first.mark == "已移出"
    assert second.mark == ""
    status = window._status_bar.currentMessage()
    assert "移动 1 张" in status
    assert "1 张失败" in status
    assert "已取消" in status


@pytest.mark.parametrize(
    "conflict", [ConflictPolicy.SKIP, ConflictPolicy.OVERWRITE]
)
def test_on_export_forwards_copy_no_prefix_and_conflict_policy(
    qapp, monkeypatch, tmp_path: Path, conflict: ConflictPolicy
) -> None:
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.png"
    destination = tmp_path / "exported"
    service = _FakeExportService(ExportReport(results=(), canceled=False))
    window = MainWindow(export_service=service)
    window._thumbnail_browser.set_models(
        [ImageFileModel(str(first_path)), ImageFileModel(str(second_path))]
    )

    class _AcceptedCopyNoPrefixDialog(_AcceptedMoveDialog):
        copy_mode = True
        prefix = ""
        conflict_policy = conflict

    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedCopyNoPrefixDialog)
    monkeypatch.setattr(main_window, "QProgressDialog", _ProgressDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(destination),
    )

    window._on_export()

    assert service.requests == [
        ExportRequest(
            destination=destination,
            items=(
                ExportItem(first_path, first_path.name),
                ExportItem(second_path, second_path.name),
            ),
            mode=ExportMode.COPY,
            conflict=conflict,
        )
    ]


def test_recovery_path_and_batch_stop_are_prominent_in_ui(
    qapp, monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "first.jpg"
    destination = tmp_path / "exported"
    target = destination / "selected_007.jpg"
    recovery = tmp_path / ".first.jpg.recovery.moving"
    report = ExportReport(
        results=(
            ExportItemResult(
                source=source,
                target=target,
                success=False,
                error="恢复失败",
                recovery_path=recovery,
            ),
        ),
        canceled=False,
        stopped_reason="磁盘空间不足，已停止后续导出",
    )
    service = _FakeExportService(report)
    window = MainWindow(export_service=service)
    window._thumbnail_browser.set_models([ImageFileModel(str(source))])
    messages: list[tuple[str, str]] = []

    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedMoveDialog)
    monkeypatch.setattr(main_window, "QProgressDialog", _ProgressDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(destination),
    )
    monkeypatch.setattr(
        main_window.QMessageBox,
        "critical",
        lambda _parent, title, text: messages.append((title, text)),
    )

    window._on_export()

    assert len(messages) == 1
    title, detail = messages[0]
    assert "人工处理" in title
    assert str(source) in detail
    assert str(target) in detail
    assert str(recovery) in detail
    assert "磁盘空间不足，已停止后续导出" in window._status_bar.currentMessage()


@pytest.mark.parametrize(
    ("report", "expected_progress"),
    [
        pytest.param(
            ExportReport(results=(), canceled=True),
            0,
            id="canceled-before-first-item",
        ),
        pytest.param(
            ExportReport(
                results=(
                    ExportItemResult(
                        source=Path("/tmp/first.jpg"),
                        target=Path("/tmp/exported/selected_007.jpg"),
                        success=False,
                        error="disk full",
                    ),
                ),
                canceled=False,
                stopped_reason="磁盘空间不足，已停止后续导出",
            ),
            1,
            id="capacity-stop-after-first-item",
        ),
    ],
)
def test_incomplete_export_does_not_report_full_progress(
    qapp,
    monkeypatch,
    tmp_path: Path,
    report: ExportReport,
    expected_progress: int,
) -> None:
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    destination = tmp_path / "exported"
    service = _FakeExportService(report)
    window = MainWindow(export_service=service)
    window._thumbnail_browser.set_models(
        [ImageFileModel(str(first_path)), ImageFileModel(str(second_path))]
    )
    progress_instances: list[_ProgressDialog] = []

    class _RecordingProgressDialog(_ProgressDialog):
        def __init__(self, *args: object) -> None:
            super().__init__(*args)
            progress_instances.append(self)

    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedMoveDialog)
    monkeypatch.setattr(main_window, "QProgressDialog", _RecordingProgressDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(destination),
    )

    window._on_export()

    assert len(progress_instances) == 1
    assert progress_instances[0].value == expected_progress
    assert progress_instances[0].closed is True
