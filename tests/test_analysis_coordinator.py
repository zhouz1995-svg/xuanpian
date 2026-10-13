from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from threading import Event, Lock
from uuid import uuid4

from src.xuanpian.services.analysis_service import (
    PhotoAnalysisRequest,
    PhotoAnalysisResponse,
)
from src.xuanpian.tasks import analysis_coordinator
from src.xuanpian.tasks.analysis_coordinator import (
    AnalysisCoordinator,
    AnalysisResult,
)
from src.xuanpian.tasks.contracts import JobFinished, JobProgress, JobState


def _requests(count: int) -> tuple[PhotoAnalysisRequest, ...]:
    return tuple(
        PhotoAnalysisRequest(
            photo_id=f"photo-{index}",
            path=f"/fake/{index}.jpg",
            analyzers=("blur",),
        )
        for index in range(count)
    )


class TrackingExecutor(ThreadPoolExecutor):
    def __init__(self, max_workers: int, reached_window: Event):
        super().__init__(max_workers=max_workers)
        self.submitted = 0
        self._reached_window = reached_window

    def submit(self, fn, /, *args, **kwargs):
        self.submitted += 1
        if self.submitted >= 4:
            self._reached_window.set()
        return super().submit(fn, *args, **kwargs)


def test_backpressure_and_cancel_stop_new_dispatch(monkeypatch) -> None:
    release = Event()
    reached_window = Event()
    holder: dict[str, TrackingExecutor] = {}

    def executor_factory(max_workers: int) -> TrackingExecutor:
        executor = TrackingExecutor(max_workers, reached_window)
        holder["executor"] = executor
        return executor

    def blocking_analyze(request: PhotoAnalysisRequest) -> PhotoAnalysisResponse:
        release.wait(2)
        return PhotoAnalysisResponse(photo_id=request.photo_id, outcomes=())

    monkeypatch.setattr(analysis_coordinator, "analyze_photo", blocking_analyze)
    events: list[object] = []
    coordinator = AnalysisCoordinator(max_workers=2, executor_factory=executor_factory)

    handle = coordinator.start(uuid4(), _requests(10), events.append)

    assert reached_window.wait(2) is True
    handle.cancel()
    release.set()
    assert handle.wait(3) is True

    assert holder["executor"].submitted == 4
    results = [event for event in events if isinstance(event, AnalysisResult)]
    progress = [event for event in events if isinstance(event, JobProgress)]
    finished = [event for event in events if isinstance(event, JobFinished)]
    assert len(results) == 4
    assert progress[-1].completed == 4
    assert finished[-1].state is JobState.CANCELED


def test_single_future_error_increments_failed_and_job_continues(monkeypatch) -> None:
    calls: list[str] = []
    lock = Lock()

    def sometimes_fails(request: PhotoAnalysisRequest) -> PhotoAnalysisResponse:
        with lock:
            calls.append(request.photo_id)
        if request.photo_id == "photo-1":
            raise ValueError("worker item failed")
        return PhotoAnalysisResponse(photo_id=request.photo_id, outcomes=())

    monkeypatch.setattr(analysis_coordinator, "analyze_photo", sometimes_fails)
    events: list[object] = []
    coordinator = AnalysisCoordinator(
        max_workers=2,
        executor_factory=ThreadPoolExecutor,
    )

    handle = coordinator.start(uuid4(), _requests(3), events.append)

    assert handle.wait(3) is True
    assert set(calls) == {"photo-0", "photo-1", "photo-2"}
    progress = [event for event in events if isinstance(event, JobProgress)]
    finished = [event for event in events if isinstance(event, JobFinished)]
    assert progress[-1].completed == 3
    assert progress[-1].failed == 1
    assert finished[-1].state is JobState.SUCCEEDED
    assert finished[-1].failed == 1


class BrokenExecutor:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        future.set_exception(BrokenProcessPool("process pool terminated"))
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        return None


def test_executor_failure_marks_whole_job_failed() -> None:
    events: list[object] = []
    coordinator = AnalysisCoordinator(max_workers=1, executor_factory=BrokenExecutor)

    handle = coordinator.start(uuid4(), _requests(2), events.append)

    assert handle.wait(3) is True
    finished = [event for event in events if isinstance(event, JobFinished)]
    assert finished[-1].state is JobState.FAILED
    assert "process pool terminated" in finished[-1].message
