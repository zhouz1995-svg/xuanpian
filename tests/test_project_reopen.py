from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from PyQt6 import sip
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QDialog

from src.xuanpian.domain.analysis import AnalysisFailure
from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.domain.photo import FileFingerprint
from src.xuanpian.domain.photo_move import PhotoMoveTransaction
from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.domain.technical_analysis import (
    ExposureConclusion,
    ExposureResultV2,
    FocusConclusion,
    FocusResult,
    TECHNICAL_ALGORITHM_VERSION,
    TechnicalAnalysisResult,
)
from src.xuanpian.services.analysis_service import (
    AnalyzerOutcome,
    PhotoAnalysisResponse,
)
from src.xuanpian.services.export_service import ConflictPolicy
from src.xuanpian.services.import_service import (
    ImportService,
    PhotoCandidate,
    candidate_to_photo_record,
)
from src.xuanpian.services.photo_move_service import PhotoMoveService
from src.xuanpian.store.project_store import PhotoChange, ProjectStore
from src.xuanpian.tasks.analysis_coordinator import AnalysisResult
from src.xuanpian.tasks.contracts import (
    CancellationToken,
    JobFinished,
    JobHandle,
    JobState,
)
from src.xuanpian.tasks.import_coordinator import (
    ImportBatch,
    ImportCoordinator,
)
from src.xuanpian.tasks.io_coordinator import MetadataResult
from src.xuanpian.ui.main_window import default_project_database_path
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui import main_window as main_window_module
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.thumbnail_browser import GROUPS


def _candidate(
    photo_id: str,
    *,
    jpeg: Path | None = None,
    raw: Path | None = None,
) -> PhotoCandidate:
    source = jpeg or raw
    assert source is not None
    return PhotoCandidate(
        photo_id=photo_id,
        display_name=source.name,
        jpeg_path="" if jpeg is None else str(jpeg.resolve()),
        raw_path="" if raw is None else str(raw.resolve()),
    )


def _done_handle(job_id: UUID | None = None) -> JobHandle:
    done = Event()
    done.set()
    return JobHandle(job_id or uuid4(), CancellationToken(), done)


class _ImportCoordinator:
    def __init__(self) -> None:
        self.starts: list[tuple[UUID, Path]] = []

    def start(self, project_id, root, on_event, batch_size=50):
        self.starts.append((project_id, root))
        return _done_handle()


class _AnalysisCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, tuple[object, ...]]] = []

    def start(self, project_id, requests, on_event):
        self.calls.append((project_id, tuple(requests)))
        return _done_handle()


class _IoCoordinator:
    def __init__(self) -> None:
        self.shutdown_calls: list[bool] = []

    def submit_metadata(self, requests, on_event):
        return _done_handle(tuple(requests)[0].job_id)

    def shutdown(self, wait=True) -> None:
        self.shutdown_calls.append(wait)


def _open_window(
    database: Path,
    *,
    import_coordinator: _ImportCoordinator | None = None,
    analysis_coordinator: _AnalysisCoordinator | None = None,
    io_coordinator: _IoCoordinator | None = None,
    thumbnail_cache=None,
) -> MainWindow:
    return MainWindow(
        import_coordinator=import_coordinator or _ImportCoordinator(),
        analysis_coordinator=analysis_coordinator
        or _AnalysisCoordinator(),
        io_coordinator=io_coordinator or _IoCoordinator(),
        thumbnail_cache=thumbnail_cache,
        project_path_resolver=lambda root: database,
        project_store_factory=lambda path, root: ProjectStore.open(
            path,
            root_path=root,
        ),
    )


def _finish_import(window: MainWindow) -> None:
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_job_finished(
        JobFinished(project_id, job_id, JobState.SUCCEEDED)
    )


def test_first_persisted_candidate_uses_the_managed_unprocessed_mark(
    qapp,
    tmp_path: Path,
) -> None:
    source = tmp_path / "first.jpg"
    source.write_bytes(b"jpeg")
    window = _open_window(tmp_path / "project.sqlite3")
    window._project_store = ProjectStore.open(
        tmp_path / "project.sqlite3",
        root_path=tmp_path,
    )
    try:
        window._persist_candidates((_candidate("first", jpeg=source),), {})

        assert window._project_store.get_photo("first").mark == (
            PhotoMark.UNPROCESSED.value
        )
    finally:
        window.close()
        qapp.processEvents()


def test_project_database_path_is_stable_and_outside_import_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    app_data = tmp_path / "app-data"

    first = default_project_database_path(root, app_data_root=app_data)
    second = default_project_database_path(
        root / ".." / "photos",
        app_data_root=app_data,
    )

    assert first == second
    assert first.parent == app_data / "projects"
    assert root not in first.parents


def test_store_retains_real_root_and_atomically_merges_paired_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "A.JPG"
    raw = root / "A.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "app-data" / "project.sqlite3"
    jpeg_only = _candidate("jpeg-id", jpeg=jpeg)
    raw_only = _candidate("raw-id", raw=raw)
    paired = _candidate("jpeg-id", jpeg=jpeg, raw=raw)

    with ProjectStore.open(database, root_path=root) as store:
        assert store.root_path == str(root.resolve())
        store.upsert_photos(
            [
                candidate_to_photo_record(jpeg_only),
                candidate_to_photo_record(raw_only),
            ]
        )
        store.save_mark("jpeg-id", "入选")
        store.save_analysis(
            "jpeg-id",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 0.0, "passed": False},
            None,
        )

        change = store.reconcile_photo(
            candidate_to_photo_record(paired),
            superseded_photo_ids=("raw-id",),
        )

        assert change is PhotoChange.CHANGED
        assert [photo.id for photo in store.list_photos()] == ["jpeg-id"]
        restored = store.list_photos()[0]
        assert restored.mark == "入选"
        assert restored.jpeg is not None
        assert restored.raw is not None
        assert store.get_analysis("jpeg-id", "blur").status is (
            AnalysisStatus.PENDING
        )
        assert store.photo_ids_by_asset_paths(
            (str(jpeg.resolve()), str(raw.resolve()))
        ) == {
            str(jpeg.resolve()): "jpeg-id",
            str(raw.resolve()): "jpeg-id",
        }

    with sqlite3.connect(database) as connection:
        for table in (
            "photos",
            "photo_assets",
            "analysis_results",
            "analysis_failures",
        ):
            assert connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                + (
                    "id = ?"
                    if table == "photos"
                    else "photo_id = ?"
                ),
                ("raw-id",),
            ).fetchone() == (0,)


def test_locked_project_refuses_import_worker_and_can_retry(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "locked.JPG"
    source.write_bytes(b"locked-project-fixture")
    database = tmp_path / "app-data" / "project.sqlite3"
    import_coordinator = _ImportCoordinator()
    window = _open_window(
        database,
        import_coordinator=import_coordinator,
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )

    with ProjectStore.open(database, root_path=root) as owner:
        owner.upsert_photos(
            (candidate_to_photo_record(_candidate("locked", jpeg=source)),)
        )
        before = owner.list_photos()

        window._start_import(str(root))

        assert import_coordinator.starts == []
        assert window._project_store is None
        assert window._active_job_ids["import"] is None
        assert window.thumbnail_browser._loading_bar.isHidden() is True
        assert owner.list_photos() == before

    assert len(warnings) == 1
    assert warnings[0][0] == "项目已打开"
    assert "关闭另一个窗口后重试" in warnings[0][1]

    window._start_import(str(root))

    assert len(import_coordinator.starts) == 1
    assert window._project_store is not None
    assert window._active_job_ids["import"] is not None
    window.close()


def test_reopen_restores_current_success_and_failed_terminal_state(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    first_path = root / "first.JPG"
    second_path = root / "second.JPG"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    database = tmp_path / "app-data" / "project.sqlite3"
    first_session_analysis = _AnalysisCoordinator()
    first = _open_window(
        database,
        analysis_coordinator=first_session_analysis,
    )
    first._start_import(str(root))
    project_id = first._active_project_id
    import_job_id = first._active_job_ids["import"]
    assert import_job_id is not None
    first._on_import_batch(
        ImportBatch(
            project_id,
            import_job_id,
            (
                _candidate("first-id", jpeg=first_path),
                _candidate("second-id", jpeg=second_path),
            ),
        )
    )
    _finish_import(first)
    first.mark_photo("first-id", "入选")

    first.close()

    result = TechnicalAnalysisResult(
        TECHNICAL_ALGORITHM_VERSION,
        FocusResult(FocusConclusion.CLEAR),
        ExposureResultV2(ExposureConclusion.NORMAL, 0.0, 0.0),
        1,
        True,
    )
    with ProjectStore.open(database, root_path=root) as store:
        store.save_analysis(
            "first-id",
            "technical",
            AnalysisStatus.SUCCEEDED,
            result.to_dict(),
            None,
        )
        store.save_analysis(
            "second-id",
            "technical",
            AnalysisStatus.FAILED,
            None,
            AnalysisFailure("technical", "model unavailable", "RuntimeError"),
        )

    reopened_analysis = _AnalysisCoordinator()
    reopened = _open_window(
        database,
        analysis_coordinator=reopened_analysis,
    )
    reopened._start_import(str(root))

    assert [model.photo_id for model in reopened.all_models] == [
        "first-id",
        "second-id",
    ]
    restored_first = reopened.model_for_photo_id("first-id")
    restored_second = reopened.model_for_photo_id("second-id")
    assert restored_first is not None
    assert restored_second is not None
    assert restored_first.mark == "入选"
    assert restored_first.technical_status is AnalysisStatus.SUCCEEDED
    assert restored_first.focus_conclusion == "清晰"
    assert restored_first.exposure_conclusion == "正常"
    assert restored_first.face_count == 1
    assert restored_first.portrait_assessed is True
    assert restored_second.technical_status is AnalysisStatus.FAILED
    assert restored_second.analysis_failures == {
        "technical": AnalysisFailure(
            "technical", "model unavailable", "RuntimeError"
        )
    }

    assert reopened._cull_btn.isEnabled() is False
    _finish_import(reopened)
    assert reopened._cull_btn.isEnabled() is True

    reopened._on_analyze_all()

    assert len(reopened_analysis.calls) == 1
    requests = reopened_analysis.calls[0][1]
    assert [
        (request.photo_id, request.analyzers)
        for request in requests
    ] == [
        ("second-id", ("technical",)),
    ]
    reopened.close()


def test_reopen_restores_five_successes_and_one_failure_for_culling(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    candidates = []
    for index in range(6):
        source = root / f"photo-{index}.JPG"
        source.write_bytes(f"fixture-{index}".encode())
        candidates.append(
            _candidate(f"photo-{index}", jpeg=source)
        )
    database = tmp_path / "app-data" / "project.sqlite3"
    result = TechnicalAnalysisResult(
        TECHNICAL_ALGORITHM_VERSION,
        FocusResult(FocusConclusion.CLEAR),
        ExposureResultV2(ExposureConclusion.NORMAL, 0.0, 0.0),
        0,
        False,
    )
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            tuple(candidate_to_photo_record(item) for item in candidates)
        )
        for index in range(5):
            store.save_analysis(
                f"photo-{index}",
                "technical",
                AnalysisStatus.SUCCEEDED,
                result.to_dict(),
                None,
            )
        store.save_analysis(
            "photo-5",
            "technical",
            AnalysisStatus.FAILED,
            None,
            AnalysisFailure("technical", "model unavailable", "RuntimeError"),
        )

    window = _open_window(database)
    window._start_import(str(root))
    assert [model.technical_status for model in window.all_models] == [
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.FAILED,
    ]
    assert window._cull_btn.isEnabled() is False

    _finish_import(window)

    assert window._cull_btn.isEnabled() is True
    models = {model.photo_id: model for model in window.all_models}
    session = CullingSession(models, models.__getitem__)
    session.set_filters(StatusFilter.ALL, AnalysisFilter.ANALYSIS_UNFINISHED)
    assert session.visible_ids() == ["photo-5"]
    window._culling_workspace._show_information(models["photo-5"])
    assert window._culling_workspace._information_values["focus"].text() == (
        "分析未完成"
    )
    assert window._culling_workspace._information_values["exposure"].text() == (
        "分析未完成"
    )
    assert window._culling_workspace._information_values["portrait"].text() == (
        "不判断"
    )
    window.close()


def test_reopen_restores_canceled_terminal_state_without_forced_rerun(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "canceled.JPG"
    source.write_bytes(b"canceled-fixture")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("canceled", jpeg=source)),)
        )
        store.save_analysis(
            "canceled",
            "technical",
            AnalysisStatus.CANCELED,
            None,
            None,
        )

    analysis = _AnalysisCoordinator()
    window = _open_window(database, analysis_coordinator=analysis)
    window._start_import(str(root))

    model = window.model_for_photo_id("canceled")
    assert model is not None
    assert model.technical_status is AnalysisStatus.CANCELED
    assert model.analysis_failures == {}
    assert analysis.calls == []

    _finish_import(window)

    assert window._cull_btn.isEnabled() is True
    assert analysis.calls == []
    window.close()


@pytest.mark.parametrize(
    "stored_status",
    [AnalysisStatus.PENDING, AnalysisStatus.RUNNING],
)
def test_reopen_does_not_promote_nonterminal_technical_state(
    qapp,
    tmp_path: Path,
    stored_status: AnalysisStatus,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "unfinished.JPG"
    source.write_bytes(b"unfinished-fixture")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("unfinished", jpeg=source)),)
        )
        store.save_analysis(
            "unfinished", "technical", stored_status, None, None
        )

    window = _open_window(database)
    window._start_import(str(root))

    model = window.model_for_photo_id("unfinished")
    assert model is not None
    assert model.technical_status is AnalysisStatus.PENDING
    _finish_import(window)
    assert window._cull_btn.isEnabled() is False
    window.close()


def test_changed_source_invalidates_restored_failed_terminal_state(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "changed.JPG"
    source.write_bytes(b"before")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("changed", jpeg=source)),)
        )
        store.save_analysis(
            "changed",
            "technical",
            AnalysisStatus.FAILED,
            None,
            AnalysisFailure("technical", "decode failed", "OSError"),
        )
    source.write_bytes(b"changed source contents")

    window = _open_window(database)
    window._start_import(str(root))
    assert window.model_for_photo_id("changed").technical_status is (
        AnalysisStatus.FAILED
    )

    _finish_import(window)

    assert window.model_for_photo_id("changed").technical_status is (
        AnalysisStatus.PENDING
    )
    assert window._cull_btn.isEnabled() is False
    window.close()
    with ProjectStore.open(database, root_path=root) as store:
        assert store.get_current_technical_analysis("changed") is None


def test_reopen_requeues_corrupt_current_technical_json(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "corrupt.JPG"
    source.write_bytes(b"corrupt-cache-fixture")
    database = tmp_path / "app-data" / "project.sqlite3"
    current = TechnicalAnalysisResult(
        TECHNICAL_ALGORITHM_VERSION,
        FocusResult(FocusConclusion.CLEAR),
        ExposureResultV2(ExposureConclusion.NORMAL, 0.0, 0.0),
        1,
        True,
    )
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("corrupt-id", jpeg=source)),)
        )
        store.save_analysis(
            "corrupt-id",
            "technical",
            AnalysisStatus.SUCCEEDED,
            current.to_dict(),
            None,
        )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE analysis_results SET result_json = ? "
            "WHERE photo_id = ? AND analyzer = ?",
            ("{invalid-json", "corrupt-id", "technical"),
        )

    analysis = _AnalysisCoordinator()
    reopened = _open_window(database, analysis_coordinator=analysis)
    reopened._start_import(str(root))

    model = reopened.model_for_photo_id("corrupt-id")
    assert model is not None
    assert model.technical_status is AnalysisStatus.PENDING
    assert model.focus_conclusion == ""
    assert model.exposure_conclusion == ""
    assert model.face_count == 0
    assert model.portrait_assessed is False

    reopened._on_analyze_all()

    assert len(analysis.calls) == 1
    assert [
        (request.photo_id, request.analyzers)
        for request in analysis.calls[0][1]
    ] == [("corrupt-id", ("technical",))]
    assert reopened._project_store.get_current_technical_analysis(
        "corrupt-id"
    ) is None
    reopened.close()


def test_reopen_ignores_v5_and_v6_conclusions_preserves_marks_and_queues_stale_cache(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    legacy_path = root / "legacy.JPG"
    v6_path = root / "v6.JPG"
    current_path = root / "current.JPG"
    legacy_path.write_bytes(b"legacy")
    v6_path.write_bytes(b"v6")
    current_path.write_bytes(b"current")
    database = tmp_path / "app-data" / "project.sqlite3"

    legacy_payload = {
        "algorithm_version": "technical-v5",
        "focus": {"conclusion": "建议复查", "confidence": 0.0, "evidence": []},
        "exposure": {
            "conclusion": "正常",
            "overexposed_pct": 0.0,
            "underexposed_pct": 0.0,
        },
        "face_count": 0,
        "portrait_assessed": False,
    }
    v6_payload = {
        "algorithm_version": "technical-v6",
        "focus": {"conclusion": "建议复查", "confidence": 0.0, "evidence": []},
        "exposure": {
            "conclusion": "正常",
            "overexposed_pct": 0.0,
            "underexposed_pct": 0.0,
        },
        "face_count": 0,
        "portrait_assessed": False,
    }
    current_payload = {**v6_payload, "algorithm_version": "technical-v7"}

    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(
                    _candidate("legacy-id", jpeg=legacy_path)
                ),
                candidate_to_photo_record(
                    _candidate("v6-id", jpeg=v6_path)
                ),
                candidate_to_photo_record(
                    _candidate("current-id", jpeg=current_path)
                ),
            )
        )
        store.save_analysis(
            "v6-id",
            "technical",
            AnalysisStatus.SUCCEEDED,
            v6_payload,
            None,
        )
        store.save_mark("v6-id", PhotoMark.HOLD.value)
        store.save_analysis(
            "legacy-id",
            "technical",
            AnalysisStatus.SUCCEEDED,
            legacy_payload,
            None,
        )
        store.save_analysis(
            "current-id",
            "technical",
            AnalysisStatus.SUCCEEDED,
            current_payload,
            None,
        )

    with ProjectStore.open(database, root_path=root) as store:
        assert store.get_current_technical_analysis("legacy-id") is None
        assert store.get_current_technical_analysis("v6-id") is None
        assert store.get_photo("v6-id").mark == PhotoMark.HOLD.value
        assert store.get_current_technical_analysis("current-id").result == current_payload

    analysis = _AnalysisCoordinator()
    reopened = _open_window(database, analysis_coordinator=analysis)
    reopened._start_import(str(root))

    legacy = reopened.model_for_photo_id("legacy-id")
    v6 = reopened.model_for_photo_id("v6-id")
    current = reopened.model_for_photo_id("current-id")
    assert legacy is not None
    assert v6 is not None
    assert current is not None
    assert legacy.technical_status is AnalysisStatus.PENDING
    assert legacy.focus_conclusion == ""
    assert legacy.exposure_conclusion == ""
    assert legacy.face_count == 0
    assert legacy.portrait_assessed is False
    assert v6.mark == PhotoMark.HOLD.value
    assert v6.technical_status is AnalysisStatus.PENDING
    assert v6.focus_conclusion == ""
    assert v6.exposure_conclusion == ""
    assert v6.face_count == 0
    assert v6.portrait_assessed is False
    assert current.technical_status is AnalysisStatus.SUCCEEDED
    assert current.focus_conclusion == "建议复查"
    assert current.exposure_conclusion == "正常"
    assert current.face_count == 0
    assert current.portrait_assessed is False

    reopened._on_analyze_all()

    assert len(analysis.calls) == 1
    assert [
        (request.photo_id, request.analyzers)
        for request in analysis.calls[0][1]
    ] == [
        ("legacy-id", ("technical",)),
        ("v6-id", ("technical",)),
    ]
    reopened.close()


def test_paired_photo_reopens_as_one_jpeg_preferred_logical_photo(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "A.JPG"
    raw = root / "A.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "app-data" / "project.sqlite3"
    first = _open_window(database)
    first._start_import(str(root))
    project_id = first._active_project_id
    job_id = first._active_job_ids["import"]
    assert job_id is not None
    first._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("jpeg-id", jpeg=jpeg),
                _candidate("raw-id", raw=raw),
                PhotoCandidate(
                    "jpeg-id",
                    jpeg.name,
                    jpeg_path=str(jpeg.resolve()),
                    raw_path=str(raw.resolve()),
                    superseded_photo_ids=("raw-id",),
                ),
            ),
        )
    )
    _finish_import(first)
    first.close()

    reopened = _open_window(database)
    reopened._start_import(str(root))

    assert len(reopened.all_models) == 1
    model = reopened.all_models[0]
    assert model.photo_id == "jpeg-id"
    assert model.analysis_path == str(jpeg.resolve())
    assert model.source_paths == (
        str(jpeg.resolve()),
        str(raw.resolve()),
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT id FROM photos ORDER BY id"
        ).fetchall() == [("jpeg-id",)]
    reopened.close()


def test_jpeg_first_rescan_upgrades_restored_raw_identity_without_losing_mark(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "A.JPG"
    raw = root / "A.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(
                    _candidate("persisted-raw", raw=raw)
                )
            ]
        )
        store.save_mark("persisted-raw", "入选")

    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("new-jpeg", jpeg=jpeg),
                _candidate("transient-raw", raw=raw),
                PhotoCandidate(
                    "new-jpeg",
                    jpeg.name,
                    jpeg_path=str(jpeg.resolve()),
                    raw_path=str(raw.resolve()),
                    superseded_photo_ids=("transient-raw",),
                ),
            ),
        )
    )
    _finish_import(window)

    assert len(window.all_models) == 1
    model = window.all_models[0]
    assert model.photo_id == "persisted-raw"
    assert model.mark == "入选"
    assert model.analysis_path == str(jpeg.resolve())
    assert model.source_paths == (
        str(jpeg.resolve()),
        str(raw.resolve()),
    )
    assert [photo.id for photo in window._project_store.list_photos()] == [
        "persisted-raw"
    ]
    window.close()


def test_rescan_invalidates_only_changed_photo_and_preserves_stable_ids(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    changed = root / "changed.JPG"
    stable = root / "stable.JPG"
    changed.write_bytes(b"first")
    stable.write_bytes(b"stable")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(
                    _candidate("changed-id", jpeg=changed)
                ),
                candidate_to_photo_record(
                    _candidate("stable-id", jpeg=stable)
                ),
            ]
        )
        for photo_id in ("changed-id", "stable-id"):
            for analyzer in ("blur", "exposure", "composition", "face"):
                store.save_analysis(
                    photo_id,
                    analyzer,
                    AnalysisStatus.SUCCEEDED,
                    {},
                    None,
                )
        old_changed_fingerprint = next(
            photo.jpeg.fingerprint
            for photo in store.list_photos()
            if photo.id == "changed-id" and photo.jpeg is not None
        )
    changed.write_bytes(b"changed contents")

    class RecordingCache:
        def __init__(self) -> None:
            self.invalidated: list[FileFingerprint] = []

        def invalidate_many(self, fingerprints) -> None:
            self.invalidated.extend(fingerprints)

    cache = RecordingCache()
    analysis = _AnalysisCoordinator()
    window = _open_window(
        database,
        analysis_coordinator=analysis,
        thumbnail_cache=cache,
    )
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("transient-changed", jpeg=changed),
                _candidate("transient-stable", jpeg=stable),
            ),
        )
    )
    _finish_import(window)

    assert [model.photo_id for model in window.all_models] == [
        "changed-id",
        "stable-id",
    ]
    assert window.model_for_photo_id(
        "changed-id"
    ).technical_status is AnalysisStatus.PENDING
    assert window.model_for_photo_id(
        "stable-id"
    ).technical_status is AnalysisStatus.PENDING
    assert cache.invalidated == [old_changed_fingerprint]

    window._on_analyze_all()

    assert [
        (request.photo_id, request.analyzers)
        for request in analysis.calls[0][1]
    ] == [
        ("changed-id", ("technical",)),
        ("stable-id", ("technical",)),
    ]
    window.close()


def test_rescan_removes_missing_paired_asset_and_invalidates_only_that_photo(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "paired.JPG"
    raw = root / "paired.NEF"
    stable = root / "stable.JPG"
    for path, contents in (
        (jpeg, b"jpeg"),
        (raw, b"raw"),
        (stable, b"stable"),
    ):
        path.write_bytes(contents)
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(
                    _candidate("paired-id", jpeg=jpeg, raw=raw)
                ),
                candidate_to_photo_record(
                    _candidate("stable-id", jpeg=stable)
                ),
            ]
        )
        for photo_id in ("paired-id", "stable-id"):
            store.save_analysis(
                photo_id,
                "blur",
                AnalysisStatus.SUCCEEDED,
                {"score": 10.0},
                None,
            )
    raw.unlink()

    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("transient-paired", jpeg=jpeg),
                _candidate("transient-stable", jpeg=stable),
            ),
        )
    )
    _finish_import(window)

    paired = window.model_for_photo_id("paired-id")
    stable_model = window.model_for_photo_id("stable-id")
    assert paired is not None
    assert paired.source_paths == (str(jpeg.resolve()),)
    assert paired.technical_status is AnalysisStatus.PENDING
    assert stable_model is not None
    assert stable_model.technical_status is AnalysisStatus.PENDING
    assert window._project_store.list_photos()[0].raw is None
    window.close()


def test_rescan_updates_changed_jpeg_when_raw_is_removed_and_invalidates_both(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "paired.JPG"
    raw = root / "paired.NEF"
    jpeg.write_bytes(b"old jpeg")
    raw.write_bytes(b"old raw")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(
                    _candidate("paired-id", jpeg=jpeg, raw=raw)
                )
            ]
        )
        store.save_analysis(
            "paired-id",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 10.0},
            None,
        )
        previous = store.list_photos()[0]
        assert previous.jpeg is not None and previous.raw is not None
        old_jpeg = previous.jpeg.fingerprint
        old_raw = previous.raw.fingerprint

    raw.unlink()
    jpeg.write_bytes(b"new jpeg contents with a different size")

    class RecordingCache:
        def __init__(self) -> None:
            self.invalidated_batches: list[tuple[FileFingerprint, ...]] = []

        def invalidate_many(self, fingerprints) -> None:
            self.invalidated_batches.append(tuple(fingerprints))

    cache = RecordingCache()
    window = _open_window(database, thumbnail_cache=cache)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (_candidate("transient-paired", jpeg=jpeg),),
        )
    )
    _finish_import(window)

    stored = window._project_store.list_photos()[0]
    assert stored.jpeg is not None
    jpeg_stat = jpeg.stat()
    assert stored.jpeg.fingerprint == FileFingerprint(
        path=str(jpeg.resolve()),
        size=jpeg_stat.st_size,
        mtime_ns=jpeg_stat.st_mtime_ns,
    )
    assert stored.raw is None
    assert [
        analysis.status
        for analysis in window._project_store.list_analyses("paired-id")
    ] == [AnalysisStatus.PENDING]
    assert window.model_for_photo_id(
        "paired-id"
    ).technical_status is AnalysisStatus.PENDING
    invalidated = {
        fingerprint
        for batch in cache.invalidated_batches
        for fingerprint in batch
    }
    assert invalidated == {old_jpeg, old_raw}
    window.close()


def test_canceled_partial_pair_rescan_keeps_changed_jpeg_and_queued_invalidation(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "paired.JPG"
    raw = root / "paired.NEF"
    jpeg.write_bytes(b"old jpeg")
    raw.write_bytes(b"old raw")
    database = tmp_path / "app-data" / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(
                    _candidate("paired-id", jpeg=jpeg, raw=raw)
                )
            ]
        )
        store.save_analysis(
            "paired-id",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 10.0},
            None,
        )
        previous = store.list_photos()[0]
        assert previous.jpeg is not None
        old_jpeg = previous.jpeg.fingerprint

    raw.unlink()
    jpeg.write_bytes(b"changed jpeg even though scan will cancel")

    class RecordingCache:
        def __init__(self) -> None:
            self.invalidated_batches: list[tuple[FileFingerprint, ...]] = []

        def invalidate_many(self, fingerprints) -> None:
            self.invalidated_batches.append(tuple(fingerprints))

    cache = RecordingCache()
    window = _open_window(database, thumbnail_cache=cache)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (_candidate("transient-paired", jpeg=jpeg),),
        )
    )
    window._on_job_finished(
        JobFinished(project_id, job_id, JobState.CANCELED)
    )

    stored = window._project_store.list_photos()[0]
    assert stored.jpeg is not None
    assert stored.jpeg.fingerprint != old_jpeg
    assert [
        analysis.status
        for analysis in window._project_store.list_analyses("paired-id")
    ] == [AnalysisStatus.PENDING]
    assert cache.invalidated_batches == [(old_jpeg,)]
    window.close()


class _OrderedHandle:
    def __init__(self, log: list[object]) -> None:
        self.job_id = uuid4()
        self.log = log
        self.window: MainWindow | None = None
        self.old_project_id = None

    def cancel(self) -> None:
        assert self.window is not None
        self.log.append(
            (
                "cancel",
                self.window._active_project_id != self.old_project_id,
                self.window._active_job_ids["import"] is None,
            )
        )

    def wait(self, timeout=None) -> bool:
        self.log.append(("wait", timeout))
        return True


class _OrderedImportCoordinator:
    def __init__(self, handle: _OrderedHandle) -> None:
        self.handle = handle

    def start(self, project_id, root, on_event, batch_size=50):
        self.handle.old_project_id = project_id
        return self.handle


class _OrderedStore:
    def __init__(self, log: list[object]) -> None:
        self.log = log
        self.closed = False

    def recover_interrupted(self) -> int:
        return 0

    def list_pending_photo_moves(self):
        return ()

    def list_photos(self):
        return []

    def close(self) -> None:
        self.closed = True
        self.log.append("store.close")


class _OrderedIoCoordinator:
    def __init__(self, log: list[object]) -> None:
        self.log = log

    def shutdown(self, wait=True) -> None:
        self.log.append(("io.shutdown", wait))


class _OrderedThumbnailCache:
    def __init__(self, log: list[object]) -> None:
        self.log = log

    def invalidate_many(self, fingerprints) -> None:
        self.log.append(("cache.invalidate_many", tuple(fingerprints)))

    def close(self) -> None:
        self.log.append("cache.close")


def test_close_retries_pending_thumbnail_invalidation_before_cache_shutdown(
    qapp,
    tmp_path: Path,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    stat = source.stat()
    stale = FileFingerprint(
        path=str(source.resolve()),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )

    class RetryCache:
        def __init__(self) -> None:
            self.submissions: list[tuple[FileFingerprint, ...]] = []
            self.drained = 0
            self.closed = 0

        def invalidate_many(self, fingerprints) -> None:
            batch = tuple(fingerprints)
            self.submissions.append(batch)
            if len(self.submissions) == 1:
                raise OSError("temporary queue failure")

        def drain(self) -> None:
            self.drained += 1

        def close(self) -> None:
            self.closed += 1

    cache = RetryCache()
    window = MainWindow(
        import_coordinator=_ImportCoordinator(),
        analysis_coordinator=_AnalysisCoordinator(),
        io_coordinator=_IoCoordinator(),
        thumbnail_cache=cache,
    )
    window._pending_thumbnail_invalidations.add(stale)

    window.closeEvent(QCloseEvent())

    assert cache.submissions == [(stale,), (stale,)]
    assert cache.drained == 1
    assert cache.closed == 1
    assert window._pending_thumbnail_invalidations == set()


def test_close_invalidates_then_cancels_waits_closes_store_and_shuts_down(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "A.JPG"
    source.write_bytes(b"jpeg")
    log: list[object] = []
    handle = _OrderedHandle(log)
    store = _OrderedStore(log)
    window = MainWindow(
        import_coordinator=_OrderedImportCoordinator(handle),
        analysis_coordinator=_AnalysisCoordinator(),
        io_coordinator=_OrderedIoCoordinator(log),
        thumbnail_cache=_OrderedThumbnailCache(log),
        project_path_resolver=lambda root: tmp_path / "project.sqlite3",
        project_store_factory=lambda path, root: store,
        shutdown_timeout=0.05,
    )
    handle.window = window
    window._start_import(str(root))
    old_project_id = window._active_project_id
    old_job_id = window._active_job_ids["import"]
    log.clear()

    window.closeEvent(QCloseEvent())

    assert log[0] == ("cancel", True, True)
    assert log[1][0] == "wait"
    assert 0.0 <= log[1][1] <= 0.05
    assert log[2:] == [
        "store.close",
        ("io.shutdown", False),
        "cache.close",
    ]

    window._on_import_batch(
        ImportBatch(
            old_project_id,
            old_job_id,
            (_candidate("late", jpeg=source),),
        )
    )
    assert window.all_models == []


def test_main_window_persists_each_import_batch_in_one_transaction(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    candidates = []
    for index in range(100):
        path = root / f"{index:04d}.jpg"
        path.write_bytes(f"jpeg-{index}".encode())
        candidates.append(_candidate(f"photo-{index:04d}", jpeg=path))
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    store = window._project_store
    assert store is not None
    transactions: list[str] = []
    store._connection.set_trace_callback(  # type: ignore[attr-defined]
        lambda statement: transactions.append(statement)
    )
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None

    window._on_import_batch(
        ImportBatch(project_id, job_id, tuple(candidates))
    )

    assert len(window.all_models) == 100
    assert len(store.list_photos()) == 100
    assert transactions.count("BEGIN IMMEDIATE") == 1
    window.close()


def test_import_batch_store_failure_rolls_back_db_and_leaves_ui_unchanged(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    first = root / "first.jpg"
    bad = root / "bad.jpg"
    first.write_bytes(b"first")
    bad.write_bytes(b"bad")
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    store = window._project_store
    assert store is not None
    store._connection.execute(  # type: ignore[attr-defined]
        """
        CREATE TRIGGER reject_bad_main_window_import
        BEFORE INSERT ON photos
        WHEN NEW.id = 'bad'
        BEGIN
            SELECT RAISE(ABORT, 'injected main window import failure');
        END
        """
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None

    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("first", jpeg=first),
                _candidate("bad", jpeg=bad),
            ),
        )
    )

    assert store.list_photos() == []
    assert window.all_models == []
    assert warnings
    window.close()


def test_transient_asset_stat_failure_keeps_database_ui_and_analysis_atomic(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    first = root / "first.jpg"
    second = root / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(_candidate("first", jpeg=first)),
                candidate_to_photo_record(_candidate("second", jpeg=second)),
            )
        )
        store.save_analysis(
            "first",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 91.0},
            None,
        )

    window = _open_window(database)
    window._start_import(str(root))
    store = window._project_store
    assert store is not None
    before_records = store.list_photos()
    before_analysis = store.get_analysis("first", "blur")
    before_models = [
        (
            model.photo_id,
            model.path,
            model.technical_status,
            model.focus_conclusion,
        )
        for model in window.all_models
    ]
    original_fingerprint = main_window_module.fingerprint

    def fail_second(path: Path):
        if path == second:
            raise PermissionError("temporary stat denial")
        return original_fingerprint(path)

    monkeypatch.setattr(main_window_module, "fingerprint", fail_second)
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    _finish_import(window)

    assert store.list_photos() == before_records
    assert store.get_analysis("first", "blur") == before_analysis
    assert [
        (
            model.photo_id,
            model.path,
            model.technical_status,
            model.focus_conclusion,
        )
        for model in window.all_models
    ] == before_models
    assert warnings and "PermissionError" in warnings[0]

    monkeypatch.setattr(
        main_window_module,
        "fingerprint",
        original_fingerprint,
    )
    window._reconcile_missing_assets()
    assert store.list_photos() == before_records
    window.close()


def test_vanished_new_candidate_creates_no_database_ui_or_metadata_ghost(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    vanished = root / "vanished.jpg"
    vanished.write_bytes(b"jpeg")
    database = tmp_path / "project.sqlite3"
    io = _IoCoordinator()
    window = _open_window(database, io_coordinator=io)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    candidate = _candidate("transient", jpeg=vanished)
    vanished.unlink()
    submissions: list[tuple[object, ...]] = []

    def capture_metadata(requests, _on_event):
        items = tuple(requests)
        submissions.append(items)
        return _done_handle(items[0].job_id)

    monkeypatch.setattr(io, "submit_metadata", capture_metadata)
    window._on_import_batch(ImportBatch(project_id, job_id, (candidate,)))

    assert window._project_store.list_photos() == []
    assert window.all_models == []
    assert submissions == []

    vanished.write_bytes(b"restored")
    window._on_import_batch(ImportBatch(project_id, job_id, (candidate,)))
    assert [photo.id for photo in window._project_store.list_photos()] == [
        "transient"
    ]
    assert [model.photo_id for model in window.all_models] == ["transient"]
    assert len(submissions) == 1
    window.close()


def test_batch_transient_stat_failure_is_atomic_and_pair_keeps_surviving_asset(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    first = root / "first.jpg"
    denied = root / "denied.jpg"
    jpeg = root / "pair.jpg"
    raw = root / "pair.nef"
    for path in (first, denied, jpeg, raw):
        path.write_bytes(path.name.encode())
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    original_fingerprint = main_window_module.fingerprint

    def deny_one(path: Path):
        if path == denied:
            raise PermissionError("temporary batch denial")
        return original_fingerprint(path)

    monkeypatch.setattr(
        "src.xuanpian.services.import_service.fingerprint",
        deny_one,
    )
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda *_args: None,
    )
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("first", jpeg=first),
                _candidate("denied", jpeg=denied),
            ),
        )
    )
    assert window._project_store.list_photos() == []
    assert window.all_models == []

    monkeypatch.setattr(
        "src.xuanpian.services.import_service.fingerprint",
        original_fingerprint,
    )
    raw.unlink()
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (_candidate("pair", jpeg=jpeg, raw=raw),),
        )
    )
    stored = window._project_store.get_photo("pair")
    assert stored is not None
    assert stored.jpeg is not None
    assert stored.raw is None
    model = window.model_for_photo_id("pair")
    assert model is not None
    assert model.jpeg_path == str(jpeg.resolve())
    assert model.raw_path == ""
    window.close()


def test_pair_merge_preserves_vanished_superseded_path_on_survivor_identity(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "pair.jpg"
    raw = root / "pair.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("jpeg-id", jpeg=jpeg),
                _candidate("raw-id", raw=raw),
            ),
        )
    )
    store = window._project_store
    store.save_mark("jpeg-id", "入选")
    window.model_for_photo_id("jpeg-id").mark = "入选"

    raw.unlink()
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                PhotoCandidate(
                    "jpeg-id",
                    jpeg.name,
                    jpeg_path=str(jpeg.resolve()),
                    raw_path=str(raw.resolve()),
                    superseded_photo_ids=("raw-id",),
                ),
            ),
        )
    )

    assert [record.id for record in store.list_photos()] == ["jpeg-id"]
    vanished_record = store.get_photo("jpeg-id")
    assert vanished_record is not None
    assert vanished_record.raw is None
    assert vanished_record.mark == "入选"
    assert store._connection.execute(  # type: ignore[attr-defined]
        """
        SELECT present FROM photo_assets
        WHERE photo_id = 'jpeg-id' AND kind = 'raw'
        """
    ).fetchone() == (0,)
    assert store.photo_ids_by_asset_paths((str(raw),)) == {
        str(raw.resolve()): "jpeg-id"
    }
    model = window.model_for_photo_id("jpeg-id")
    assert model is not None
    assert model.jpeg_path == str(jpeg.resolve())
    assert model.raw_path == ""
    assert model.mark == "入选"
    assert window.model_for_photo_id("raw-id") is None

    raw.write_bytes(b"raw-restored")
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                PhotoCandidate(
                    "restored-transient",
                    jpeg.name,
                    jpeg_path=str(jpeg.resolve()),
                    raw_path=str(raw.resolve()),
                ),
            ),
        )
    )
    restored = store.get_photo("jpeg-id")
    assert restored is not None and restored.raw is not None
    assert restored.mark == "入选"
    assert window.model_for_photo_id("restored-transient") is None
    assert [model.photo_id for model in window.all_models] == ["jpeg-id"]
    window.close()


def test_missing_asset_reconcile_failure_keeps_persisted_and_visible_state(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "gone.jpg"
    source.write_bytes(b"jpeg")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("gone", jpeg=source)),)
        )
    window = _open_window(database)
    window._start_import(str(root))
    model = window.model_for_photo_id("gone")
    assert model is not None
    source.unlink()
    store = window._project_store
    assert store is not None
    store._connection.execute(  # type: ignore[attr-defined]
        """
        CREATE TRIGGER reject_missing_asset_reconcile
        BEFORE UPDATE OF present ON photo_assets
        BEGIN
            SELECT RAISE(ABORT, 'injected missing reconcile failure');
        END
        """
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    _finish_import(window)

    assert window.model_for_photo_id("gone") is model
    assert model.jpeg_path == str(source.resolve())
    persisted = store.get_photo("gone")
    assert persisted is not None and persisted.jpeg is not None
    assert warnings
    window.close()


def test_missing_asset_reconcile_notifies_each_changed_photo_by_its_own_id(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    first_path = root / "first.jpg"
    second_path = root / "second.jpg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(
                    _candidate("first", jpeg=first_path)
                ),
                candidate_to_photo_record(
                    _candidate("second", jpeg=second_path)
                ),
            )
        )
    window = _open_window(database)
    window._start_import(str(root))
    first_path.write_bytes(b"first-changed")
    second_path.write_bytes(b"second-changed")
    notifications: list[tuple[str, tuple[PhotoRoles, ...]]] = []
    monkeypatch.setattr(
        window.thumbnail_browser._photo_list_model,
        "update_photo",
        lambda photo_id, roles: notifications.append(
            (photo_id, tuple(roles))
        ),
    )

    window._reconcile_missing_assets()

    expected_roles = (
        PhotoRoles.PATH,
        PhotoRoles.ASSET_KIND,
        PhotoRoles.ANALYSIS,
    )
    assert notifications == [
        ("first", expected_roles),
        ("second", expected_roles),
    ]
    window.close()


def test_missing_asset_reset_incrementally_leaves_active_group_filter(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    changed_jpeg = root / "changed.JPG"
    changed_raw = root / "changed.NEF"
    stable_jpeg = root / "stable.JPG"
    changed_jpeg.write_bytes(b"jpeg")
    changed_raw.write_bytes(b"raw")
    stable_jpeg.write_bytes(b"stable")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(
                    _candidate(
                        "changed",
                        jpeg=changed_jpeg,
                        raw=changed_raw,
                    )
                ),
                candidate_to_photo_record(
                    _candidate("stable", jpeg=stable_jpeg)
                ),
            )
        )
        for photo_id in ("changed", "stable"):
            store.save_analysis(
                photo_id,
                "blur",
                AnalysisStatus.SUCCEEDED,
                {"score": 900.0},
                None,
            )
    window = _open_window(database)
    window._start_import(str(root))
    browser = window.thumbnail_browser
    changed = window.model_for_photo_id("changed")
    assert changed is not None
    changed.technical_status = AnalysisStatus.SUCCEEDED
    changed.focus_conclusion = "清晰"
    changed.exposure_conclusion = "正常"
    changed.face_count = 1
    changed.portrait_assessed = True
    browser.refresh_photo(changed.photo_id, [PhotoRoles.ANALYSIS])
    browser._set_group("clear")
    browser.select_photo_id("changed")
    assert browser.current_photo_ids() == ["changed"]
    changed_jpeg.unlink()
    monkeypatch.setattr(
        browser,
        "_compute_group_counts",
        lambda: (_ for _ in ()).throw(
            AssertionError("reconcile rebuilt all group counts")
        ),
    )
    monkeypatch.setattr(
        browser,
        "_get_filtered",
        lambda: (_ for _ in ()).throw(
            AssertionError("reconcile rebuilt the full filter")
        ),
    )
    original_classify = browser.classify_group
    classifications = 0

    def counted_classify(model, group_key):
        nonlocal classifications
        classifications += 1
        return original_classify(model, group_key)

    monkeypatch.setattr(browser, "classify_group", counted_classify)

    window._reconcile_missing_assets()

    changed = window.model_for_photo_id("changed")
    assert changed is not None
    assert changed.technical_status is AnalysisStatus.PENDING
    assert changed.focus_conclusion == ""
    assert changed.exposure_conclusion == ""
    assert changed.face_count == 0
    assert changed.portrait_assessed is False
    assert browser.current_photo_ids() == []
    assert browser.selected_photo_ids() == []
    assert browser._group_counts["clear"] == 0
    assert browser._analysis_activity_count == 0
    assert classifications == len(GROUPS)
    window.close()


def test_real_1000_photo_scan_batches_bound_main_window_transactions(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    for index in range(400):
        (root / f"paired_{index:04d}.JPG").write_bytes(
            f"jpeg-pair-{index}".encode()
        )
        (root / f"paired_{index:04d}.NEF").write_bytes(
            f"raw-pair-{index}".encode()
        )
    for index in range(500):
        (root / f"jpeg_{index:04d}.JPG").write_bytes(
            f"jpeg-{index}".encode()
        )
    for index in range(100):
        (root / f"raw_{index:04d}.NEF").write_bytes(
            f"raw-{index}".encode()
        )

    restored_jpeg = root / "paired_0000.JPG"
    restored_raw = root / "paired_0000.NEF"
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(
                    _candidate("restored-pair", jpeg=restored_jpeg)
                ),
            )
        )
        store.save_mark("restored-pair", "入选")
    restored_jpeg.write_bytes(b"changed-restored-jpeg")

    scan_project_id = uuid4()
    scan_events: list[object] = []
    scan_handle = ImportCoordinator(ImportService()).start(
        scan_project_id,
        root,
        scan_events.append,
        batch_size=50,
    )
    assert scan_handle.wait(10)
    batches = [
        event for event in scan_events if isinstance(event, ImportBatch)
    ]
    finished = next(
        event for event in scan_events if isinstance(event, JobFinished)
    )
    assert finished.state is JobState.SUCCEEDED
    assert len(batches) == 36
    assert all(len(batch.items) == 50 for batch in batches)
    assert sum(len(batch.items) for batch in batches) == 1800

    restored_updates = [
        candidate
        for batch in batches
        for candidate in batch.items
        if {
            str(restored_jpeg.resolve()),
            str(restored_raw.resolve()),
        }.intersection(candidate.source_paths)
    ]
    assert any(
        candidate.jpeg_path and not candidate.raw_path
        for candidate in restored_updates
    )
    assert any(
        candidate.raw_path and not candidate.jpeg_path
        for candidate in restored_updates
    )
    assert any(
        candidate.jpeg_path
        and candidate.raw_path
        and candidate.superseded_photo_ids
        for candidate in restored_updates
    )

    window = _open_window(database)
    window._start_import(str(root))
    store = window._project_store
    assert store is not None
    transactions: list[str] = []
    store._connection.set_trace_callback(  # type: ignore[attr-defined]
        lambda statement: transactions.append(statement)
    )
    window._active_project_id = scan_project_id
    window._active_job_ids["import"] = scan_handle.job_id
    for batch in batches:
        window._on_import_batch(batch)
    window._on_job_finished(finished)

    # 1,800 real candidate updates arrive in 36 coordinator batches.
    # Each batch commits once; the completion reconcile may add at most one
    # transaction if the filesystem changes between scan and completion.
    assert 36 <= transactions.count("BEGIN IMMEDIATE") <= 37
    records = store.list_photos()
    assert len(records) == 1000
    assert sum(
        record.jpeg is not None and record.raw is not None
        for record in records
    ) == 400
    restored_record = store.get_photo("restored-pair")
    assert restored_record is not None
    assert restored_record.jpeg is not None
    assert restored_record.raw is not None
    assert restored_record.jpeg.fingerprint == FileFingerprint(
        path=str(restored_jpeg.resolve()),
        size=restored_jpeg.stat().st_size,
        mtime_ns=restored_jpeg.stat().st_mtime_ns,
    )
    assert restored_record.raw.fingerprint == FileFingerprint(
        path=str(restored_raw.resolve()),
        size=restored_raw.stat().st_size,
        mtime_ns=restored_raw.stat().st_mtime_ns,
    )
    assert restored_record.mark == "入选"
    restored = window.model_for_photo_id("restored-pair")
    assert restored is not None
    assert restored.source_paths == (
        str(restored_jpeg.resolve()),
        str(restored_raw.resolve()),
    )
    assert restored.mark == "入选"
    window.close()


def test_canceled_import_keeps_only_already_committed_batches(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    candidates = []
    for index in range(150):
        path = root / f"{index:04d}.jpg"
        path.write_bytes(f"jpeg-{index}".encode())
        candidates.append(_candidate(f"photo-{index:04d}", jpeg=path))
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    store = window._project_store
    assert store is not None
    transactions: list[str] = []
    store._connection.set_trace_callback(  # type: ignore[attr-defined]
        lambda statement: transactions.append(statement)
    )
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    for offset in (0, 50):
        window._on_import_batch(
            ImportBatch(
                project_id,
                job_id,
                tuple(candidates[offset : offset + 50]),
            )
        )
    window._on_job_finished(
        JobFinished(project_id, job_id, JobState.CANCELED)
    )

    assert len(window.all_models) == 100
    assert len(store.list_photos()) == 100
    assert store.get_photo("photo-0100") is None
    assert transactions.count("BEGIN IMMEDIATE") == 2
    window.close()


def test_same_batch_partial_pair_update_does_not_drop_planned_raw_asset(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "A.jpg"
    raw = root / "A.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None

    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("paired", jpeg=jpeg, raw=raw),
                _candidate("paired", jpeg=jpeg),
            ),
        )
    )

    stored = window._project_store.get_photo("paired")
    assert stored is not None
    assert stored.jpeg is not None and stored.raw is not None
    model = window.model_for_photo_id("paired")
    assert model is not None
    assert model.source_paths == (
        str(jpeg.resolve()),
        str(raw.resolve()),
    )
    window.close()


def test_all_missing_then_restored_file_reuses_stable_id_and_mark(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "restored.jpg"
    source.write_bytes(b"original")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("stable", jpeg=source)),)
        )
        store.save_mark("stable", "入选")

    source.unlink()
    missing_window = _open_window(database)
    missing_window._start_import(str(root))
    _finish_import(missing_window)
    assert missing_window.model_for_photo_id("stable") is None
    missing_window.close()

    source.write_bytes(b"restored with changed fingerprint")
    restored_window = _open_window(database)
    restored_window._start_import(str(root))
    project_id = restored_window._active_project_id
    job_id = restored_window._active_job_ids["import"]
    assert job_id is not None
    restored_window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (_candidate("transient", jpeg=source),),
        )
    )
    _finish_import(restored_window)

    restored = restored_window.model_for_photo_id("stable")
    assert restored is not None
    assert restored.mark == "入选"
    assert restored.analysis_path == str(source.resolve())
    assert restored_window.model_for_photo_id("transient") is None
    restored_window.close()


def test_removed_photo_stays_removed_across_reopen_and_rescan(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    source = root / "removed.jpg"
    source.write_bytes(b"still on disk")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (candidate_to_photo_record(_candidate("stable", jpeg=source)),)
        )
        store.delete_photos(("stable",))

    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (_candidate("new-uuid", jpeg=source),),
        )
    )
    _finish_import(window)

    assert window.all_models == []
    assert window._project_store.list_photos() == []
    assert window._project_store.removed_asset_paths((str(source),)) == {
        str(source.resolve())
    }
    assert source.read_bytes() == b"still on disk"
    window.close()


def test_canceled_scan_before_pair_event_keeps_both_identities_and_marks(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "pair.jpg"
    raw = root / "pair.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                candidate_to_photo_record(
                    _candidate("jpeg-stable", jpeg=jpeg)
                ),
                candidate_to_photo_record(
                    _candidate("raw-stable", raw=raw)
                ),
            )
        )
        store.save_mark("jpeg-stable", "入选")
        store.save_mark("raw-stable", "待定")

    window = _open_window(database)
    window._start_import(str(root))
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("jpeg-transient", jpeg=jpeg),
                _candidate("raw-transient", raw=raw),
            ),
        )
    )
    window._on_job_finished(
        JobFinished(project_id, job_id, JobState.CANCELED)
    )

    assert {
        photo.id: photo.mark
        for photo in window._project_store.list_photos()
    } == {
        "jpeg-stable": "入选",
        "raw-stable": "待定",
    }
    assert {
        model.photo_id: model.mark
        for model in window.all_models
    } == {
        "jpeg-stable": "入选",
        "raw-stable": "待定",
    }
    window.close()


@pytest.mark.parametrize("survivor_kind", ["jpeg", "raw"])
def test_pair_merge_syncs_resolved_superseded_mark_to_existing_ui_consumers(
    qapp,
    tmp_path: Path,
    monkeypatch,
    survivor_kind: str,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "pair.jpg"
    raw = root / "pair.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    survivor_id = f"{survivor_kind}-stable"
    superseded_kind = "raw" if survivor_kind == "jpeg" else "jpeg"
    superseded_id = f"{superseded_kind}-stable"
    records = {
        "jpeg": candidate_to_photo_record(
            _candidate("jpeg-stable", jpeg=jpeg)
        ),
        "raw": candidate_to_photo_record(
            _candidate("raw-stable", raw=raw)
        ),
    }
    records[survivor_kind] = replace(
        records[survivor_kind],
        mark="",
    )
    records[superseded_kind] = replace(
        records[superseded_kind],
        mark="入选",
    )
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos((records["jpeg"], records["raw"]))

    window = _open_window(database)
    window._start_import(str(root))
    survivor_model = window.model_for_photo_id(survivor_id)
    assert survivor_model is not None
    assert survivor_model.mark == ""
    window.enter_preview_photo(survivor_id)
    changes: list[tuple[str, list[int]]] = []
    photo_model = window.thumbnail_browser._photo_list_model
    photo_model.dataChanged.connect(
        lambda top_left, _bottom_right, roles: changes.append(
            (
                str(top_left.data(int(PhotoRoles.ID))),
                list(roles),
            )
        )
    )

    scans = {
        "jpeg": _candidate("jpeg-scan", jpeg=jpeg),
        "raw": _candidate("raw-scan", raw=raw),
    }
    survivor_scan_id = f"{survivor_kind}-scan"
    superseded_scan_id = f"{superseded_kind}-scan"
    pair = PhotoCandidate(
        survivor_scan_id,
        jpeg.name,
        jpeg_path=str(jpeg.resolve()),
        raw_path=str(raw.resolve()),
        superseded_photo_ids=(superseded_scan_id,),
    )
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                scans[survivor_kind],
                scans[superseded_kind],
                pair,
            ),
        )
    )

    stored = window._project_store.get_photo(survivor_id)
    assert stored is not None and stored.mark == "入选"
    assert window._project_store.get_photo(superseded_id) is None
    assert window.model_for_photo_id(survivor_id) is survivor_model
    assert survivor_model.mark == "入选"
    assert any(
        photo_id == survivor_id
        and roles == [int(PhotoRoles.MARK)]
        for photo_id, roles in changes
    )
    assert window._preview_widget._mark_label.text() == "标记: 保留"

    assert window.thumbnail_browser.toggle_picks_only() is True
    assert window.thumbnail_browser.current_photo_ids() == [survivor_id]

    class _AcceptedCopyDialog:
        DialogCode = QDialog.DialogCode
        selected_group = "all"
        copy_mode = True
        prefix = ""
        start_number = 1
        conflict_policy = ConflictPolicy.UNIQUE

        def __init__(self, groups, parent) -> None:
            self.groups = groups

        def exec(self):
            return self.DialogCode.Accepted

    export_requests = []
    monkeypatch.setattr(
        main_window_module,
        "ExportDialog",
        _AcceptedCopyDialog,
    )
    monkeypatch.setattr(
        main_window_module.QFileDialog,
        "getExistingDirectory",
        lambda *_args: str(tmp_path / "export"),
    )
    monkeypatch.setattr(
        window._io_coordinator,
        "submit_export",
        lambda _project_id, request, _callback: (
            export_requests.append(request) or _done_handle()
        ),
        raising=False,
    )
    window._on_export()

    assert [
        unit.unit_id
        for unit in export_requests[0].units
    ] == [survivor_id]
    window.close()
    sip.delete(window)


def test_pair_mark_conflict_keeps_database_and_existing_ui_unchanged(
    qapp,
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    jpeg = root / "pair.jpg"
    raw = root / "pair.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database, root_path=root) as store:
        store.upsert_photos(
            (
                replace(
                    candidate_to_photo_record(
                        _candidate("jpeg-stable", jpeg=jpeg)
                    ),
                    mark="入选",
                ),
                replace(
                    candidate_to_photo_record(
                        _candidate("raw-stable", raw=raw)
                    ),
                    mark="淘汰",
                ),
            )
        )

    window = _open_window(database)
    window._start_import(str(root))
    before_models = tuple(window.all_models)
    warnings: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    project_id = window._active_project_id
    job_id = window._active_job_ids["import"]
    assert job_id is not None
    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                _candidate("jpeg-scan", jpeg=jpeg),
                _candidate("raw-scan", raw=raw),
                PhotoCandidate(
                    "jpeg-scan",
                    jpeg.name,
                    jpeg_path=str(jpeg.resolve()),
                    raw_path=str(raw.resolve()),
                    superseded_photo_ids=("raw-scan",),
                ),
            ),
        )
    )

    assert {
        photo.id: photo.mark
        for photo in window._project_store.list_photos()
    } == {
        "jpeg-stable": "入选",
        "raw-stable": "淘汰",
    }
    assert tuple(window.all_models) == before_models
    assert {
        model.photo_id: model.mark
        for model in window.all_models
    } == {
        "jpeg-stable": "入选",
        "raw-stable": "淘汰",
    }
    assert warnings
    window.close()
    sip.delete(window)


def _seed_pending_photo_move(
    database: Path,
    root: Path,
    *,
    renamed_ordinals: tuple[int, ...] = (),
    completed_ordinals: tuple[int, ...] = (),
) -> PhotoMoveTransaction:
    jpeg = root / "pending.jpg"
    raw = root / "pending.nef"
    jpeg.write_bytes(b"pending-jpeg")
    raw.write_bytes(b"pending-raw")
    with ProjectStore.open(database, root_path=root) as store:
        record = candidate_to_photo_record(
            _candidate("stable-pending-id", jpeg=jpeg, raw=raw)
        )
        store.upsert_photos((record,))
        transaction = PhotoMoveService().plan(record, PhotoMark.KEEP)
        store.begin_photo_move(transaction)
        for ordinal in renamed_ordinals:
            item = transaction.items[ordinal]
            Path(item.target_path).parent.mkdir(parents=True, exist_ok=True)
            os.replace(item.source_path, item.target_path)
        for ordinal in completed_ordinals:
            store.mark_move_item_completed(transaction.id, ordinal)
    return transaction


@pytest.mark.parametrize(
    ("renamed", "completed", "expected_mark", "uses_targets"),
    (
        ((), (), PhotoMark.UNPROCESSED.value, False),
        ((0,), (), PhotoMark.UNPROCESSED.value, False),
        ((0,), (0,), PhotoMark.UNPROCESSED.value, False),
        ((0, 1), (), PhotoMark.KEEP.value, True),
        ((0, 1), (0, 1), PhotoMark.KEEP.value, True),
    ),
)
def test_reopen_recovers_every_precommit_move_crash_point_before_scan(
    qapp,
    tmp_path: Path,
    renamed: tuple[int, ...],
    completed: tuple[int, ...],
    expected_mark: str,
    uses_targets: bool,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    database = tmp_path / "project.sqlite3"
    transaction = _seed_pending_photo_move(
        database,
        root,
        renamed_ordinals=renamed,
        completed_ordinals=completed,
    )
    coordinator = _ImportCoordinator()
    window = _open_window(database, import_coordinator=coordinator)

    window._start_import(str(root))

    assert len(coordinator.starts) == 1
    assert window._project_store.list_pending_photo_moves() == ()
    stored = window._project_store.get_photo("stable-pending-id")
    assert stored is not None
    assert stored.id == "stable-pending-id"
    assert stored.mark == expected_mark
    expected_paths = tuple(
        item.target_path if uses_targets else item.source_path
        for item in transaction.items
    )
    assert tuple(asset.fingerprint.path for asset in stored.source_assets) == (
        expected_paths
    )
    assert tuple(asset.origin_path for asset in stored.source_assets) == tuple(
        item.source_path for item in transaction.items
    )
    assert all(Path(path).is_file() for path in expected_paths)
    window.close()


def test_reopen_needs_user_preserves_journal_and_blocks_scan_and_analysis(
    qapp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    database = tmp_path / "project.sqlite3"
    transaction = _seed_pending_photo_move(database, root)
    conflict_paths = []
    for item in transaction.items:
        source = Path(item.source_path)
        target = Path(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        os.utime(target, ns=(item.mtime_ns, item.mtime_ns))
        conflict_paths.append(target)
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        lambda _parent, title, message: warnings.append((title, message)),
    )
    imports = _ImportCoordinator()
    analyses = _AnalysisCoordinator()
    window = _open_window(
        database,
        import_coordinator=imports,
        analysis_coordinator=analyses,
    )

    window._start_import(str(root))
    window._on_analyze_all()

    assert imports.starts == []
    assert analyses.calls == []
    assert not window._analyze_btn.isEnabled()
    assert len(window._project_store.list_pending_photo_moves()) == 1
    assert warnings and warnings[0][0] == "照片移动需要处理"
    assert "Traceback" not in warnings[0][1]
    assert all(str(path) in warnings[0][1] for path in conflict_paths)
    assert all(Path(item.source_path).exists() for item in transaction.items)
    assert all(Path(item.target_path).exists() for item in transaction.items)
    window.close()


class _MetadataCapturingIo(_IoCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.request_batches: list[tuple[object, ...]] = []

    def submit_metadata(self, requests, on_event):
        batch = tuple(requests)
        self.request_batches.append(batch)
        return _done_handle(batch[0].job_id)


def test_committed_managed_photos_reopen_with_identity_analysis_and_burst_metadata(
    qapp,
    tmp_path: Path,
) -> None:
    root = tmp_path / "photos"
    root.mkdir()
    database = tmp_path / "project.sqlite3"
    origins: dict[str, str] = {}
    targets: dict[str, str] = {}
    with ProjectStore.open(database, root_path=root) as store:
        for photo_id, contents in (
            ("similar-a", b"photo-a"),
            ("similar-b", b"photo-b"),
        ):
            source = root / f"{photo_id}.jpg"
            source.write_bytes(contents)
            record = candidate_to_photo_record(_candidate(photo_id, jpeg=source))
            store.upsert_photos((record,))
            store.save_analysis(
                photo_id,
                "blur",
                AnalysisStatus.SUCCEEDED,
                {"score": 42.0},
                None,
            )
            transaction = PhotoMoveService().plan(record, PhotoMark.KEEP)
            store.begin_photo_move(transaction)
            result = PhotoMoveService().execute(
                transaction,
                lambda ordinal, move_id=transaction.id: (
                    store.mark_move_item_completed(move_id, ordinal)
                ),
            )
            assert result.success
            store.commit_photo_move(transaction.id, result.moved_assets)
            origins[photo_id] = transaction.items[0].source_path
            targets[photo_id] = transaction.items[0].target_path
        assert store.list_pending_photo_moves() == ()

    io = _MetadataCapturingIo()
    window = _open_window(database, io_coordinator=io)
    window._start_import(str(root))

    assert [model.photo_id for model in window.all_models] == [
        "similar-a",
        "similar-b",
    ]
    assert len(io.request_batches) == 1
    requests = io.request_batches[0]
    assert {request.photo_id: request.path for request in requests} == targets
    for request, taken in zip(
        requests,
        ("2026:05:02 16:40:31", "2026:05:02 16:40:32"),
        strict=True,
    ):
        window._on_metadata_result(
            MetadataResult(
                project_id=request.project_id,
                job_id=request.job_id,
                photo_id=request.photo_id,
                path=request.path,
                qimage=None,
                exif=ExifData(path=request.path, date_taken=taken),
            )
        )
    window._thumbnail_browser.refresh_burst_groups()
    _finish_import(window)

    stored = {
        photo.id: photo for photo in window._project_store.list_photos()
    }
    assert set(stored) == {"similar-a", "similar-b"}
    for photo_id, record in stored.items():
        assert record.id == photo_id
        assert record.mark == PhotoMark.KEEP.value
        assert record.jpeg is not None
        assert record.jpeg.fingerprint.path == targets[photo_id]
        assert record.jpeg.origin_path == origins[photo_id]
        analyses = window._project_store.list_analyses(photo_id)
        assert len(analyses) == 1
        assert analyses[0].status is AnalysisStatus.SUCCEEDED
        assert analyses[0].result == {"score": 42.0}
    assert window._thumbnail_browser.burst_member_ids("similar-a") == (
        "similar-a",
        "similar-b",
    )
    assert window._thumbnail_browser.burst_member_ids("similar-b") == (
        "similar-a",
        "similar-b",
    )
    window.close()
