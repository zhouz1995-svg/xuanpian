from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from PyQt6.QtGui import QImage

from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.tasks import io_coordinator
from src.xuanpian.tasks.contracts import JobFinished, JobState
from src.xuanpian.tasks.io_coordinator import (
    IoCoordinator,
    MetadataRequest,
    MetadataResult,
)


class _TrackingExecutor(ThreadPoolExecutor):
    def __init__(self, max_workers: int, reached_window: Event):
        super().__init__(max_workers=max_workers)
        self.submitted = 0
        self._reached_window = reached_window

    def submit(self, fn, /, *args, **kwargs):
        self.submitted += 1
        if self.submitted >= 4:
            self._reached_window.set()
        return super().submit(fn, *args, **kwargs)


def _requests(count: int) -> tuple[MetadataRequest, ...]:
    project_id = uuid4()
    job_id = uuid4()
    return tuple(
        MetadataRequest(
            project_id,
            job_id,
            f"photo-{index}",
            f"/fake/{index}.jpg",
            (180, 135),
        )
        for index in range(count)
    )


def test_worker_count_is_hard_capped_at_four() -> None:
    observed: list[int] = []

    class _Executor:
        def __init__(self, max_workers: int) -> None:
            observed.append(max_workers)

        def shutdown(
            self,
            wait: bool = True,
            cancel_futures: bool = False,
        ) -> None:
            pass

    coordinator = IoCoordinator(
        max_workers=8,
        executor_factory=_Executor,
    )
    coordinator.shutdown()

    assert observed == [4]


def test_bounded_metadata_dispatch_and_cancel(monkeypatch) -> None:
    release = Event()
    reached_window = Event()
    holder = {}

    def executor_factory(max_workers: int):
        executor = _TrackingExecutor(max_workers, reached_window)
        holder["executor"] = executor
        return executor

    def blocking_load(request: MetadataRequest) -> MetadataResult:
        release.wait(2)
        return MetadataResult(
            request.project_id,
            request.job_id,
            request.photo_id,
            request.path,
            QImage(),
            ExifData(request.path),
        )

    monkeypatch.setattr(io_coordinator, "load_metadata", blocking_load)
    events: list[object] = []
    coordinator = IoCoordinator(
        max_workers=2,
        executor_factory=executor_factory,
    )

    handle = coordinator.submit_metadata(_requests(10), events.append)

    assert reached_window.wait(2) is True
    handle.cancel()
    release.set()
    assert handle.wait(3) is True
    coordinator.shutdown()

    assert holder["executor"].submitted == 4
    results = [event for event in events if isinstance(event, MetadataResult)]
    finished = [event for event in events if isinstance(event, JobFinished)]
    assert len(results) == 4
    assert finished[-1].state is JobState.CANCELED


def test_worker_reads_only_submitted_jpeg_path(monkeypatch) -> None:
    requested_paths: list[str] = []
    request = MetadataRequest(
        uuid4(),
        uuid4(),
        "paired",
        "/photos/A.JPG",
        (180, 135),
    )

    def fake_thumbnail(path: str, size: tuple[int, int]) -> QImage:
        requested_paths.append(path)
        return QImage(10, 10, QImage.Format.Format_RGB888)

    def fake_exif(path: str) -> ExifData:
        requested_paths.append(path)
        return ExifData(path, camera_model="D850")

    monkeypatch.setattr(io_coordinator, "load_thumbnail", fake_thumbnail)
    monkeypatch.setattr(io_coordinator, "read_exif", fake_exif)

    result = io_coordinator.load_metadata(request)

    assert requested_paths == ["/photos/A.JPG", "/photos/A.JPG"]
    assert result.path == "/photos/A.JPG"
    assert result.exif.camera_model == "D850"
