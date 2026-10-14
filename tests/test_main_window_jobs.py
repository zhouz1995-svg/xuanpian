from types import SimpleNamespace
from uuid import uuid4

from PyQt6.QtGui import QImage

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.technical_analysis import (
    ExposureConclusion, ExposureResultV2, FocusConclusion, FocusResult,
    TECHNICAL_ALGORITHM_VERSION,
    TechnicalAnalysisResult,
)
from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.services.analysis_service import (
    AnalyzerOutcome,
    PhotoAnalysisResponse,
)
from src.xuanpian.services.import_service import PhotoCandidate
from src.xuanpian.store.project_store import PhotoChange
from src.xuanpian.tasks.analysis_coordinator import AnalysisResult
from src.xuanpian.tasks.contracts import JobFinished, JobState
from src.xuanpian.tasks.import_coordinator import ImportBatch
from src.xuanpian.tasks.io_coordinator import MetadataResult
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.photo_list_model import PhotoRoles


def _activate_import(window: MainWindow):
    project_id = uuid4()
    job_id = uuid4()
    window._active_project_id = project_id
    window._active_job_ids["import"] = job_id
    return project_id, job_id


def test_stale_import_events_do_not_mutate_models(qapp) -> None:
    window = MainWindow()
    project_id, job_id = _activate_import(window)
    candidate = PhotoCandidate("p1", "one.jpg", jpeg_path="/one.jpg")

    window._on_import_batch(
        ImportBatch(uuid4(), job_id, (candidate,))
    )
    window._on_import_batch(
        ImportBatch(project_id, uuid4(), (candidate,))
    )

    assert window.thumbnail_browser.all_models() == []


def test_main_window_has_no_parallel_photo_collections(qapp) -> None:
    window = MainWindow()

    assert not hasattr(window, "_models")
    assert not hasattr(window, "_model_by_photo_id")


def test_import_upsert_preserves_survivor_and_removes_superseded_id(qapp) -> None:
    window = MainWindow()
    project_id, job_id = _activate_import(window)
    jpeg = PhotoCandidate("jpeg", "A.JPG", jpeg_path="/A.JPG")
    raw = PhotoCandidate("raw", "A.NEF", raw_path="/A.NEF")
    window._on_import_batch(ImportBatch(project_id, job_id, (jpeg, raw)))
    survivor = window.thumbnail_browser.model_for_photo_id("jpeg")
    assert survivor is not None
    survivor.mark = "入选"

    paired = PhotoCandidate(
        "jpeg",
        "A.JPG",
        jpeg_path="/A.JPG",
        raw_path="/A.NEF",
        superseded_photo_ids=("raw",),
    )
    window._on_import_batch(ImportBatch(project_id, job_id, (paired,)))

    assert window.thumbnail_browser.all_models() == [survivor]
    assert survivor.source_paths == ("/A.JPG", "/A.NEF")
    assert survivor.mark == "入选"
    assert window.thumbnail_browser.current_photo_ids() == ["jpeg"]
    assert window.thumbnail_browser.model_for_photo_id("jpeg") is survivor


def test_survivor_merge_notifies_only_changed_roles_after_superseded_removal(
    qapp,
) -> None:
    window = MainWindow(io_coordinator=_RecordingIoCoordinator())
    project_id, job_id = _activate_import(window)
    survivor = PhotoCandidate(
        "survivor",
        "A.NEF",
        raw_path="/A.NEF",
    )
    superseded = PhotoCandidate(
        "superseded",
        "A.JPG",
        jpeg_path="/A.JPG",
    )
    window._on_import_batch(
        ImportBatch(project_id, job_id, (survivor, superseded))
    )
    changes: list[tuple[int, list[int]]] = []
    removals: list[tuple[int, int]] = []
    photo_model = window.thumbnail_browser._photo_list_model
    photo_model.dataChanged.connect(
        lambda top_left, _bottom_right, roles: changes.append(
            (top_left.row(), list(roles))
        )
    )
    photo_model.rowsRemoved.connect(
        lambda _parent, first, last: removals.append((first, last))
    )
    merged = PhotoCandidate(
        "survivor",
        "A.JPG",
        jpeg_path="/A.JPG",
        raw_path="/A.NEF",
        superseded_photo_ids=("superseded",),
    )

    window._on_import_batch(ImportBatch(project_id, job_id, (merged,)))
    window._on_import_batch(ImportBatch(project_id, job_id, (merged,)))

    assert window.thumbnail_browser.current_photo_ids() == ["survivor"]
    assert removals == [(1, 1)]
    assert changes == [
        (0, [int(PhotoRoles.ANALYSIS)]),
        (
            0,
            [
                int(PhotoRoles.PATH),
                int(PhotoRoles.FILENAME),
                int(PhotoRoles.ASSET_KIND),
            ],
        )
    ]


def test_changed_import_notifies_analysis_for_same_model(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow(io_coordinator=_RecordingIoCoordinator())
    project_id, job_id = _activate_import(window)
    model = ImageFileModel(
        "/A.JPG",
        photo_id="p1",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    window.thumbnail_browser.set_models([model])
    monkeypatch.setattr(
        window,
        "_persist_candidates",
        lambda candidates, _models: (
            candidates,
            (PhotoChange.CHANGED,),
            {},
        ),
    )
    changes: list[list[int]] = []
    window.thumbnail_browser._photo_list_model.dataChanged.connect(
        lambda _top_left, _bottom_right, roles: changes.append(list(roles))
    )

    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (PhotoCandidate("p1", "A.JPG", jpeg_path="/A.JPG"),),
        )
    )

    assert model.technical_status is AnalysisStatus.PENDING
    assert [int(PhotoRoles.ANALYSIS)] in changes


def test_no_store_jpeg_upgrade_clears_raw_analysis_and_notifies(
    qapp,
) -> None:
    window = MainWindow(io_coordinator=_RecordingIoCoordinator())
    project_id, job_id = _activate_import(window)
    model = ImageFileModel(
        "/A.NEF",
        photo_id="p1",
        raw_path="/A.NEF",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    window.thumbnail_browser.set_models([model])
    changes: list[list[int]] = []
    window.thumbnail_browser._photo_list_model.dataChanged.connect(
        lambda _top_left, _bottom_right, roles: changes.append(list(roles))
    )

    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                PhotoCandidate(
                    "p1",
                    "A.JPG",
                    jpeg_path="/A.JPG",
                    raw_path="/A.NEF",
                ),
            ),
        )
    )

    assert model.analysis_path == "/A.JPG"
    assert model.technical_status is AnalysisStatus.PENDING
    assert model.focus_conclusion == ""
    assert model.exposure_conclusion == ""
    assert model.face_count == 0
    assert model.portrait_assessed is False
    assert [int(PhotoRoles.ANALYSIS)] in changes


def test_no_store_raw_companion_keeps_jpeg_analysis_current(qapp) -> None:
    window = MainWindow(io_coordinator=_RecordingIoCoordinator())
    project_id, job_id = _activate_import(window)
    model = ImageFileModel(
        "/A.JPG",
        photo_id="p1",
        jpeg_path="/A.JPG",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    window.thumbnail_browser.set_models([model])

    window._on_import_batch(
        ImportBatch(
            project_id,
            job_id,
            (
                PhotoCandidate(
                    "p1",
                    "A.JPG",
                    jpeg_path="/A.JPG",
                    raw_path="/A.NEF",
                ),
            ),
        )
    )

    assert model.analysis_path == "/A.JPG"
    assert model.technical_status is AnalysisStatus.SUCCEEDED
    assert model.focus_conclusion == "清晰"
    assert model.exposure_conclusion == "正常"
    assert model.face_count == 1
    assert model.portrait_assessed is True


class _RecordingAnalysisCoordinator:
    def __init__(self) -> None:
        self.requests = ()
        self.callback = None
        self.job_id = uuid4()

    def start(self, project_id, requests, on_event):
        self.requests = tuple(requests)
        self.callback = on_event
        return SimpleNamespace(job_id=self.job_id, cancel=lambda: None)


class _RecordingIoCoordinator:
    def __init__(self) -> None:
        self.requests = []

    def submit_metadata(self, requests, on_event):
        items = tuple(requests)
        self.requests.extend(items)
        return SimpleNamespace(
            job_id=items[0].job_id,
            cancel=lambda: None,
        )

    def shutdown(self) -> None:
        pass


def test_paired_import_submits_jpeg_only_for_metadata(qapp) -> None:
    coordinator = _RecordingIoCoordinator()
    window = MainWindow(io_coordinator=coordinator)
    project_id, job_id = _activate_import(window)
    paired = PhotoCandidate(
        "p1",
        "A.JPG",
        jpeg_path="/A.JPG",
        raw_path="/A.NEF",
    )

    window._on_import_batch(ImportBatch(project_id, job_id, (paired,)))

    assert [
        (request.photo_id, request.path)
        for request in coordinator.requests
    ] == [("p1", "/A.JPG")]


def test_raw_only_import_submits_analysis_path_for_metadata(qapp) -> None:
    coordinator = _RecordingIoCoordinator()
    window = MainWindow(io_coordinator=coordinator)
    project_id, job_id = _activate_import(window)
    raw_only = PhotoCandidate(
        "raw",
        "A.NEF",
        raw_path="/A.NEF",
    )

    window._on_import_batch(
        ImportBatch(project_id, job_id, (raw_only,))
    )

    assert [
        (request.photo_id, request.path)
        for request in coordinator.requests
    ] == [("raw", "/A.NEF")]


def test_raw_metadata_result_populates_shared_thumbnail_roles(qapp) -> None:
    coordinator = _RecordingIoCoordinator()
    window = MainWindow(io_coordinator=coordinator)
    project_id, import_job_id = _activate_import(window)
    raw_only = PhotoCandidate(
        "raw",
        "A.NEF",
        raw_path="/A.NEF",
    )

    window._on_import_batch(
        ImportBatch(project_id, import_job_id, (raw_only,))
    )
    assert coordinator.requests, "RAW-only import must schedule metadata"
    request = coordinator.requests[0]
    qimage = QImage(8, 6, QImage.Format.Format_RGB32)
    window._on_metadata_result(
        MetadataResult(
            project_id=request.project_id,
            job_id=request.job_id,
            photo_id=request.photo_id,
            path=request.path,
            qimage=qimage,
            exif=ExifData(path=request.path),
        )
    )

    model = window.thumbnail_browser.model_for_photo_id("raw")
    assert model is not None
    assert model.thumbnail is not None
    browser_model = window.thumbnail_browser._photo_list_model
    browser_index = browser_model.index(0, 0)
    assert browser_model.data(
        browser_index,
        PhotoRoles.THUMBNAIL,
    ) is model.thumbnail
    filmstrip = window._culling_workspace._filmstrip
    filmstrip.set_photo_ids(["raw"])
    assert filmstrip._model.data(
        filmstrip._model.index(0, 0),
        PhotoRoles.THUMBNAIL,
    ) is model.thumbnail


def test_analysis_submits_one_jpeg_preferred_request_per_photo(qapp) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    model = ImageFileModel.from_candidate(
        PhotoCandidate(
            "p1",
            "A.JPG",
            jpeg_path="/A.JPG",
            raw_path="/A.NEF",
        )
    )
    window.thumbnail_browser.set_models([model])

    window._on_analyze_all()

    assert [(item.photo_id, item.path) for item in coordinator.requests] == [
        ("p1", "/A.JPG")
    ]
    assert coordinator.requests[0].analyzers == ("technical",)
    assert model.technical_status is AnalysisStatus.RUNNING


def test_analysis_restart_clears_stale_technical_conclusions(qapp) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    model = ImageFileModel(
        "/A.JPG",
        photo_id="p1",
        technical_status=AnalysisStatus.FAILED,
        focus_conclusion="清晰",
        exposure_conclusion="过曝",
        face_count=1,
        portrait_assessed=True,
    )
    window.thumbnail_browser.set_models([model])

    window._on_analyze_all()

    assert model.technical_status is AnalysisStatus.RUNNING
    assert model.focus_conclusion == ""
    assert model.exposure_conclusion == ""
    assert model.face_count == 0
    assert model.portrait_assessed is False


def test_technical_result_is_applied_atomically_to_ui_model(qapp) -> None:
    model = ImageFileModel("/p1.jpg")
    result = TechnicalAnalysisResult.review_only()

    MainWindow._apply_technical_result(model, result)

    assert model.focus_conclusion == "建议复查"
    assert model.exposure_conclusion == "正常"
    assert model.face_count == 0
    assert model.portrait_assessed is False


def test_analysis_start_refreshes_each_successfully_persisted_photo_incrementally(
    qapp,
    monkeypatch,
) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    models = [
        ImageFileModel(f"/{photo_id}.JPG", photo_id=photo_id)
        for photo_id in ("p1", "p2")
    ]
    window.thumbnail_browser.set_models(models)
    refreshed: list[tuple[str, tuple[PhotoRoles | int, ...]]] = []
    monkeypatch.setattr(
        window.thumbnail_browser,
        "refresh_photo",
        lambda photo_id, roles: refreshed.append(
            (photo_id, tuple(roles))
        ),
    )

    window._on_analyze_all()

    assert refreshed == [
        ("p1", (PhotoRoles.ANALYSIS,)),
        ("p2", (PhotoRoles.ANALYSIS,)),
    ]
    assert window.thumbnail_browser.all_models() == models


def test_analysis_start_updates_activity_and_filter_without_global_rebuild(
    qapp,
    monkeypatch,
) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    model = ImageFileModel("/p1.JPG", photo_id="p1")
    browser = window.thumbnail_browser
    browser.set_models([model])
    browser._set_group("sharp")
    assert browser._analysis_activity_count == 0
    assert browser.current_photo_ids() == []
    monkeypatch.setattr(
        browser,
        "_compute_group_counts",
        lambda: (_ for _ in ()).throw(
            AssertionError("analysis start performed a global rebuild")
        ),
    )

    window._on_analyze_all()

    assert model.technical_status is AnalysisStatus.RUNNING
    assert browser._analysis_activity_count == 1
    assert browser.current_photo_ids() == []
    assert browser._empty_label.text() == "此筛选条件下没有照片"


def test_analysis_start_store_failure_keeps_model_and_derived_state_unchanged(
    qapp,
    monkeypatch,
) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    model = ImageFileModel("/p1.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    activity_before = window.thumbnail_browser._analysis_activity_count
    window._project_store = _FailingStore(fail_analyzers=("technical",))
    refreshed: list[str] = []
    monkeypatch.setattr(
        window.thumbnail_browser,
        "refresh_photo",
        lambda photo_id, _roles: refreshed.append(photo_id),
    )
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda *_args: None,
    )

    window._on_analyze_all()

    assert model.technical_status is AnalysisStatus.PENDING
    assert window.thumbnail_browser._analysis_activity_count == activity_before
    assert refreshed == []


def test_stale_analysis_result_is_ignored(qapp) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    window._active_project_id = uuid4()
    window._active_job_ids["analysis"] = uuid4()
    response = PhotoAnalysisResponse(
        "p1",
        (
            AnalyzerOutcome(
                "technical",
                AnalysisStatus.SUCCEEDED,
                result=SimpleNamespace(
                    focus=SimpleNamespace(conclusion=SimpleNamespace(value="清晰")),
                    exposure=SimpleNamespace(conclusion=SimpleNamespace(value="正常")),
                    face_count=0,
                    portrait_assessed=False,
                ),
            ),
        ),
    )

    window._on_analysis_result(
        AnalysisResult(uuid4(), window._active_job_ids["analysis"], response)
    )

    assert model.technical_status is AnalysisStatus.PENDING


def test_analysis_result_notifies_only_the_visible_photo_analysis_role(
    qapp,
) -> None:
    window = MainWindow()
    first = ImageFileModel("/A.JPG", photo_id="p1")
    second = ImageFileModel("/B.JPG", photo_id="p2")
    window.thumbnail_browser.set_models([first, second])
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    changes: list[tuple[int, list[int]]] = []
    window.thumbnail_browser._photo_list_model.dataChanged.connect(
        lambda top_left, _bottom_right, roles: changes.append(
            (top_left.row(), list(roles))
        )
    )
    response = PhotoAnalysisResponse(
        "p2",
        (
            AnalyzerOutcome(
                "blur",
                AnalysisStatus.SUCCEEDED,
                result=SimpleNamespace(
                    score=900.0,
                    severity="清晰",
                    passed=True,
                ),
            ),
        ),
    )

    window._on_analysis_result(
        AnalysisResult(window._active_project_id, job_id, response)
    )

    assert changes == [(1, [int(PhotoRoles.ANALYSIS)])]


def test_failed_analysis_finish_reconciles_missing_results(qapp) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    for name in ("technical",):
        setattr(model, f"{name}_status", AnalysisStatus.RUNNING)

    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.FAILED,
            failed=1,
            message="worker pool crashed",
        )
    )

    assert model.technical_status is AnalysisStatus.FAILED
    assert model.analysis_failures["technical"].message == "worker pool crashed"
    assert (
        model.analysis_failures["technical"].exception_type
        == "AnalysisJobError"
    )


def test_successful_finish_marks_missing_responses_failed(qapp) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    model.technical_status = AnalysisStatus.RUNNING

    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.SUCCEEDED,
            failed=1,
        )
    )

    assert model.technical_status is AnalysisStatus.FAILED
    assert (
        model.analysis_failures["technical"].exception_type
        == "MissingAnalysisResult"
    )


def test_analysis_finish_refreshes_culling_eligibility(qapp) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    window._project_store = SimpleNamespace(
        culling_notice_confirmed=lambda: True,
        save_analysis=lambda *_args: None,
        close=lambda: None,
    )
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    model.technical_status = AnalysisStatus.RUNNING
    window._refresh_culling_eligibility()
    assert window._cull_btn.isEnabled() is False

    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.CANCELED,
        )
    )

    assert model.technical_status is AnalysisStatus.CANCELED
    assert window._cull_btn.isEnabled() is True
    window.close()
    qapp.processEvents()


def test_terminal_analysis_failure_refreshes_visible_preview(qapp) -> None:
    window = MainWindow()
    model = ImageFileModel(
        "/A.JPG",
        photo_id="p1",
        technical_status=AnalysisStatus.RUNNING,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
        face_count=1,
        portrait_assessed=True,
    )
    window.thumbnail_browser.set_models([model])
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    window.enter_preview_photo("p1")
    assert window._preview_widget._blur_value.text() == "分析中"

    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.FAILED,
            message="worker pool crashed",
        )
    )

    assert window._preview_widget._blur_value.text() == "分析失败"
    assert window._preview_widget._portrait_value.text() == "不判断"
    window.close()


class _FailingStore:
    def __init__(self, *, fail_analyzers=()) -> None:
        self.fail_analyzers = set(fail_analyzers)
        self.saved_analyses: list[str] = []

    def save_mark(self, photo_id: str, mark: str) -> None:
        raise OSError("mark database unavailable")

    def save_marks(self, marks) -> None:
        raise OSError("mark database unavailable")

    def save_analysis(
        self,
        photo_id,
        analyzer,
        status,
        result,
        failure,
    ) -> None:
        if not self.fail_analyzers or analyzer in self.fail_analyzers:
            raise OSError(f"{analyzer} database unavailable")
        self.saved_analyses.append(analyzer)

    def save_analyses(self, analyses) -> None:
        items = tuple(analyses)
        failing = next(
            (
                analyzer
                for _photo_id, analyzer, _status, _result, _failure in items
                if not self.fail_analyzers
                or analyzer in self.fail_analyzers
            ),
            None,
        )
        if failing is not None:
            raise OSError(f"{failing} database unavailable")
        self.saved_analyses.extend(
            analyzer
            for _photo_id, analyzer, _status, _result, _failure in items
        )


class _RecordingMarkStore:
    def __init__(self) -> None:
        self.saved_marks: list[tuple[tuple[str, str], ...]] = []

    def save_marks(self, marks) -> None:
        self.saved_marks.append(tuple(marks))


def test_batch_mark_enables_for_selection_and_persists_atomically(qapp) -> None:
    window = MainWindow()
    first = ImageFileModel("/A.JPG", photo_id="p1")
    second = ImageFileModel("/B.JPG", photo_id="p2")
    store = _RecordingMarkStore()
    window._project_store = store
    window.thumbnail_browser.set_models([first, second])
    window.select_all()

    window._update_batch_mark_state()

    assert window._batch_mark_btn.isEnabled()
    assert all(
        action.isEnabled() for action in window._batch_mark_menu.actions()
    )
    window._on_batch_mark("保留")
    assert [first.mark, second.mark] == ["保留", "保留"]
    assert store.saved_marks == [(("p1", "保留"), ("p2", "保留"))]


def test_mark_failure_keeps_model_selection_and_visible_mark(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1", mark="待定")
    window.thumbnail_browser.set_models([model])
    window.thumbnail_browser.select_photo_id("p1")
    window._project_store = _FailingStore()
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window.mark_photo("p1", "入选")

    assert model.mark == "待定"
    assert window.thumbnail_browser.selected_photo_ids() == ["p1"]
    assert window.model_for_photo_id("p1") is model
    assert warnings and "mark database unavailable" in warnings[0]


def test_batch_mark_failure_keeps_every_model_unchanged(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow()
    first = ImageFileModel("/A.JPG", photo_id="p1", mark="待定")
    second = ImageFileModel("/B.JPG", photo_id="p2", mark="")
    window.thumbnail_browser.set_models([first, second])
    window._project_store = _FailingStore()
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window.mark_photos(("p1", "p2"), "入选")

    assert first.mark == "待定"
    assert second.mark == ""
    assert len(warnings) == 1


def test_analysis_persistence_failure_keeps_old_model_and_continues_outcomes(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    model.technical_status = AnalysisStatus.RUNNING
    window.thumbnail_browser.set_models([model])
    window._project_store = _FailingStore(fail_analyzers=("technical",))
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )
    response = PhotoAnalysisResponse(
        "p1",
        (
            AnalyzerOutcome("technical", AnalysisStatus.SUCCEEDED, result=TechnicalAnalysisResult.review_only()),
        ),
    )

    window._on_analysis_result(
        AnalysisResult(window._active_project_id, job_id, response)
    )

    assert model.technical_status is AnalysisStatus.RUNNING
    assert model.focus_conclusion == ""
    assert warnings and "technical database unavailable" in warnings[0]


def test_analysis_result_incrementally_reapplies_active_group_filter(
    qapp,
) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    window.thumbnail_browser.set_models([model])
    window.thumbnail_browser._set_group("clear")
    assert window.thumbnail_browser.current_photo_ids() == []
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    response = PhotoAnalysisResponse(
        "p1",
        (
                AnalyzerOutcome("technical", AnalysisStatus.SUCCEEDED, result=TechnicalAnalysisResult(
                    TECHNICAL_ALGORITHM_VERSION, FocusResult(FocusConclusion.CLEAR),
                    ExposureResultV2(ExposureConclusion.NORMAL, 0, 0),
                    0, True,
                )),
        ),
    )

    window._on_analysis_result(
        AnalysisResult(window._active_project_id, job_id, response)
    )

    assert window.thumbnail_browser.current_photo_ids() == ["p1"]


def test_analysis_finish_persistence_failure_does_not_escape_or_mutate_model(
    qapp,
    monkeypatch,
) -> None:
    window = MainWindow()
    model = ImageFileModel("/A.JPG", photo_id="p1")
    model.technical_status = AnalysisStatus.RUNNING
    window.thumbnail_browser.set_models([model])
    window._project_store = _FailingStore(fail_analyzers=("technical",))
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    warnings: list[str] = []
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda _parent, _title, message: warnings.append(message),
    )

    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.FAILED,
            message="worker failed",
        )
    )

    assert window._active_job_ids["analysis"] is None
    assert model.technical_status is AnalysisStatus.RUNNING
    assert "technical" not in model.analysis_failures
    assert len(warnings) == 1


def test_running_analysis_left_by_store_failure_can_be_retried(
    qapp,
    monkeypatch,
) -> None:
    coordinator = _RecordingAnalysisCoordinator()
    window = MainWindow(analysis_coordinator=coordinator)
    model = ImageFileModel("/A.JPG", photo_id="p1")
    model.technical_status = AnalysisStatus.RUNNING
    window.thumbnail_browser.set_models([model])
    window._project_store = _FailingStore(fail_analyzers=("technical",))
    window._active_project_id = uuid4()
    job_id = uuid4()
    window._active_job_ids["analysis"] = job_id
    monkeypatch.setattr(
        "src.xuanpian.ui.main_window.QMessageBox.warning",
        lambda *_args: None,
    )
    window._on_job_finished(
        JobFinished(
            window._active_project_id,
            job_id,
            JobState.FAILED,
            message="worker failed",
        )
    )
    window._project_store = None

    window._on_analyze_all()

    assert coordinator.requests
    assert "technical" in coordinator.requests[0].analyzers
