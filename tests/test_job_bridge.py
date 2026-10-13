from threading import Thread
from uuid import uuid4

from PyQt6.QtCore import QThread

from src.xuanpian.tasks.contracts import JobProgress
from src.xuanpian.tasks.move_coordinator import (
    MoveFinished,
    MoveItemCompleted,
    MoveStarted,
)
from src.xuanpian.ui.job_bridge import QtJobBridge


def test_publish_from_worker_is_delivered_on_qt_thread(qapp, qtbot) -> None:
    bridge = QtJobBridge()
    event = JobProgress(uuid4(), uuid4(), completed=1, total=2)
    receiving_threads: list[QThread] = []
    bridge.progress.connect(
        lambda received: receiving_threads.append(QThread.currentThread())
    )

    with qtbot.waitSignal(bridge.progress, timeout=1000) as blocker:
        worker = Thread(target=bridge.publish, args=(event,))
        worker.start()
        worker.join()

    assert blocker.args == [event]
    assert receiving_threads == [qapp.thread()]


def test_move_events_are_delivered_by_named_signals_on_qt_owner_thread(
    qapp,
    qtbot,
) -> None:
    bridge = QtJobBridge()
    project_id = uuid4()
    job_id = uuid4()
    events = (
        MoveStarted(project_id, job_id),
        MoveItemCompleted(project_id, job_id, ordinal=0),
        MoveFinished(
            project_id,
            job_id,
            success=False,
            completed_ordinals=(0,),
            recovery_paths=(),
            error="injected failure",
        ),
    )
    received: list[object] = []
    receiving_threads: list[QThread] = []

    def record(event: object) -> None:
        received.append(event)
        receiving_threads.append(QThread.currentThread())

    bridge.move_started.connect(record)
    bridge.move_item_completed.connect(record)
    bridge.move_finished.connect(record)

    with qtbot.waitSignal(bridge.move_finished, timeout=1000):
        worker = Thread(target=lambda: [bridge.publish(event) for event in events])
        worker.start()
        worker.join()

    assert received == list(events)
    assert receiving_threads == [qapp.thread(), qapp.thread(), qapp.thread()]
