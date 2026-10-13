from pathlib import Path
from threading import Event
from uuid import uuid4

from src.xuanpian.services.import_service import PhotoCandidate
from src.xuanpian.tasks.import_coordinator import ImportBatch, ImportCoordinator
from src.xuanpian.tasks.contracts import JobFinished, JobProgress, JobState


def test_import_batches_keep_identity_and_cancel_stops_iteration(tmp_path) -> None:
    first_ready = Event()
    release_callback = Event()
    requested: list[int] = []

    class Service:
        def scan(self, root: Path, batch_size: int):
            requested.append(1)
            yield (PhotoCandidate("p1", "1.jpg", jpeg_path="/1.jpg"),)
            requested.append(2)
            yield (PhotoCandidate("p2", "2.jpg", jpeg_path="/2.jpg"),)

    events: list[object] = []

    def on_event(event: object) -> None:
        events.append(event)
        if isinstance(event, ImportBatch):
            first_ready.set()
            release_callback.wait(2)

    project_id = uuid4()
    handle = ImportCoordinator(Service()).start(
        project_id,
        tmp_path,
        on_event,
        batch_size=1,
    )

    assert first_ready.wait(2) is True
    handle.cancel()
    release_callback.set()
    assert handle.wait(2) is True

    assert requested == [1]
    assert all(event.project_id == project_id for event in events)
    batches = [event for event in events if isinstance(event, ImportBatch)]
    progress = [event for event in events if isinstance(event, JobProgress)]
    finished = [event for event in events if isinstance(event, JobFinished)]
    assert batches[0].items[0].photo_id == "p1"
    assert progress[-1].completed == 1
    assert finished[-1].state is JobState.CANCELED
    assert all(event.job_id == handle.job_id for event in events)


def test_import_service_failure_is_path_qualified(tmp_path) -> None:
    class BrokenService:
        def scan(self, root: Path, batch_size: int):
            raise OSError(f"cannot scan {root}")
            yield

    events: list[object] = []
    handle = ImportCoordinator(BrokenService()).start(
        uuid4(),
        tmp_path,
        events.append,
    )

    assert handle.wait(2) is True
    finished = [event for event in events if isinstance(event, JobFinished)][-1]
    assert finished.state is JobState.FAILED
    assert str(tmp_path) in finished.message
    assert "OSError" in finished.message
