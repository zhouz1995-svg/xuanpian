from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog

from src.xuanpian.services.export_service import (
    ConflictPolicy,
    ExportItemResult,
    ExportMode,
    ExportReport,
    ExportUnitResult,
)
from src.xuanpian.services.import_service import PhotoCandidate
from src.xuanpian.tasks.io_coordinator import ExportFinished, IoCoordinator
from src.xuanpian.ui import main_window
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


class _AcceptedMoveDialog:
    DialogCode = QDialog.DialogCode
    selected_group = "all"
    copy_mode = False
    prefix = ""
    start_number = 1
    conflict_policy = ConflictPolicy.SKIP

    def __init__(self, groups, parent) -> None:
        pass

    def exec(self):
        return self.DialogCode.Accepted


class _BlockingExportService:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.requests = []

    def export(self, request, is_canceled):
        self.requests.append(request)
        self.started.set()
        self.release.wait(3)
        results = tuple(
            ExportItemResult(
                item.source,
                request.destination / item.target_name,
                True,
                None,
            )
            for unit in request.units
            for item in unit.items
        )
        unit_results = tuple(
            ExportUnitResult(
                unit.unit_id,
                tuple(
                    result
                    for result in results
                    if result.source in {
                        item.source for item in unit.items
                    }
                ),
                True,
            )
            for unit in request.units
        )
        return ExportReport(results, False, unit_results=unit_results)


@pytest.fixture(autouse=True)
def accept_destructive_export_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        MainWindow,
        "_confirm_destructive_export",
        lambda *_args: True,
    )


def test_paired_export_runs_off_qt_thread_and_builds_one_unit(
    qapp, qtbot, monkeypatch, tmp_path
) -> None:
    service = _BlockingExportService()
    coordinator = IoCoordinator(max_workers=1, export_service=service)
    window = MainWindow(
        export_service=service,
        io_coordinator=coordinator,
    )
    model = ImageFileModel.from_candidate(
        PhotoCandidate(
            "p1",
            "A.JPG",
            jpeg_path=str(tmp_path / "A.JPG"),
            raw_path=str(tmp_path / "A.NEF"),
        )
    )
    window._thumbnail_browser.set_models([model])
    destination = tmp_path / "out"
    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedMoveDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(destination),
    )

    window._on_export()
    assert service.started.wait(2)
    assert window._export_context["photo_ids"] == ("p1",)
    assert "models" not in window._export_context
    timer_fired = []
    QTimer.singleShot(0, lambda: timer_fired.append(True))
    qtbot.waitUntil(lambda: bool(timer_fired), timeout=1000)

    request = service.requests[0]
    assert len(request.units) == 1
    assert request.units[0].unit_id == "p1"
    assert [item.source for item in request.units[0].items] == [
        tmp_path / "A.JPG",
        tmp_path / "A.NEF",
    ]

    service.release.set()
    qtbot.waitUntil(
        lambda: window._active_job_ids["export"] is None,
        timeout=3000,
    )
    assert model.mark == "已移出"
    window.close()


def test_stale_export_finish_does_not_mark_model(qapp, tmp_path) -> None:
    window = MainWindow()
    model = ImageFileModel(str(tmp_path / "A.JPG"), photo_id="p1")
    window._active_project_id = uuid4()
    window._active_job_ids["export"] = uuid4()
    window._export_context = {
        "mode": ExportMode.MOVE,
        "photo_ids": ("p1",),
        "destination": tmp_path / "out",
    }
    report = ExportReport(
        (
            ExportItemResult(
                Path(model.path),
                tmp_path / "out" / "A.JPG",
                True,
                None,
            ),
        ),
        False,
        unit_results=(
            ExportUnitResult(
                "p1",
                (
                    ExportItemResult(
                        Path(model.path),
                        tmp_path / "out" / "A.JPG",
                        True,
                        None,
                    ),
                ),
                True,
            ),
        ),
    )

    window._on_export_finished(
        ExportFinished(
            window._active_project_id,
            uuid4(),
            report,
        )
    )

    assert model.mark == ""


def test_new_import_keeps_export_disabled_during_reset(
    qapp, tmp_path
) -> None:
    enabled_when_canceled = []

    class _ImportCoordinator:
        def start(self, project_id, root, on_event):
            return SimpleNamespace(job_id=uuid4(), cancel=lambda: None)

    window = MainWindow(import_coordinator=_ImportCoordinator())
    window._export_btn.setEnabled(False)
    window._export_handle = SimpleNamespace(
        job_id=uuid4(),
        cancel=lambda: enabled_when_canceled.append(
            window._export_btn.isEnabled()
        ),
    )
    window._active_job_ids["export"] = window._export_handle.job_id
    window._export_context = {"active": True}

    window._start_import(str(tmp_path))

    assert enabled_when_canceled == [False]
    assert window._export_btn.isEnabled() is False
    assert window._export_context is None
    assert window._active_job_ids["export"] is None


def test_export_submission_failure_restores_ui_state(
    qapp, monkeypatch, tmp_path
) -> None:
    class _FailingIoCoordinator:
        def submit_export(self, project_id, request, on_event):
            raise RuntimeError("executor unavailable")

        def shutdown(self, wait=True):
            pass

    window = MainWindow(io_coordinator=_FailingIoCoordinator())
    model = ImageFileModel.from_candidate(
        PhotoCandidate("p1", "A.JPG", jpeg_path=str(tmp_path / "A.JPG"))
    )
    window._thumbnail_browser.set_models([model])
    monkeypatch.setattr(main_window, "ExportDialog", _AcceptedMoveDialog)
    monkeypatch.setattr(
        main_window.QFileDialog,
        "getExistingDirectory",
        lambda *args: str(tmp_path / "out"),
    )

    window._on_export()

    assert window._export_btn.isEnabled() is True
    assert window._export_context is None
    assert window._active_job_ids["export"] is None
    assert "executor unavailable" in window._status_bar.currentMessage()


def test_move_mark_persistence_failure_keeps_exported_model_mark_unchanged(
    qapp,
    monkeypatch,
    tmp_path,
) -> None:
    class _FailingMarkStore:
        def save_marks(self, marks) -> None:
            raise OSError("export mark database unavailable")

    window = MainWindow()
    model = ImageFileModel(
        str(tmp_path / "A.JPG"),
        photo_id="p1",
        mark="入选",
    )
    window.thumbnail_browser.set_models([model])
    window._project_store = _FailingMarkStore()
    warnings: list[str] = []
    monkeypatch.setattr(
        main_window.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    report = ExportReport(
        (),
        False,
        unit_results=(
            ExportUnitResult("p1", (), True),
        ),
    )

    window._consume_export_report(
        report,
        ExportMode.MOVE,
        {"p1": model},
        tmp_path / "out",
    )

    assert model.mark == "入选"
    assert warnings and "export mark database unavailable" in warnings[0]


def test_export_worker_preserves_recovery_locations_and_notes(tmp_path) -> None:
    recovery = tmp_path / ".A.NEF.recovery.moving"
    recovery.symlink_to(tmp_path / "missing-raw-target")

    class _CatastrophicService:
        def export(self, request, is_canceled):
            error = RuntimeError("catastrophic rollback")
            error.recovery_paths = (recovery,)
            error.add_note(f"照片数据位置: {recovery}")
            raise error

    coordinator = IoCoordinator(
        max_workers=1,
        export_service=_CatastrophicService(),
    )
    events = []
    request = main_window.ExportRequest(
        destination=tmp_path / "out",
        items=(),
        units=(),
        mode=ExportMode.MOVE,
    )

    handle = coordinator.submit_export(uuid4(), request, events.append)
    assert handle.wait(3)
    coordinator.shutdown()
    finished = [
        event for event in events if isinstance(event, ExportFinished)
    ][0]

    assert finished.recovery_paths == (recovery,)
    assert any(str(recovery) in note for note in finished.notes)
