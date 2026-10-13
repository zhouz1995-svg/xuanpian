from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Callable
from uuid import uuid4

from src.xuanpian.services.import_service import ImportService, PhotoCandidate
from src.xuanpian.tasks.contracts import (
    CancellationToken,
    JobFinished,
    JobHandle,
    JobId,
    JobProgress,
    JobState,
    ProjectId,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportBatch:
    project_id: ProjectId
    job_id: JobId
    items: tuple[PhotoCandidate, ...]


class ImportCoordinator:
    def __init__(self, service: ImportService | None = None) -> None:
        self._service = service or ImportService()

    def start(
        self,
        project_id: ProjectId,
        root: Path,
        on_event: Callable[[object], None],
        batch_size: int = 50,
    ) -> JobHandle:
        job_id = uuid4()
        handle = JobHandle(job_id, CancellationToken(), Event())
        Thread(
            target=self._run,
            args=(project_id, handle, root, on_event, batch_size),
            name=f"import-{job_id}",
            daemon=True,
        ).start()
        return handle

    @staticmethod
    def _emit(on_event: Callable[[object], None], event: object) -> None:
        try:
            on_event(event)
        except Exception:
            logger.exception("Import job event callback failed")

    def _run(
        self,
        project_id: ProjectId,
        handle: JobHandle,
        root: Path,
        on_event: Callable[[object], None],
        batch_size: int,
    ) -> None:
        completed = 0
        state = JobState.RUNNING
        message = ""
        self._emit(
            on_event,
            JobProgress(project_id, handle.job_id, 0, 0, message="扫描已启动"),
        )
        try:
            batches = iter(self._service.scan(root, batch_size))
            while not handle._token.is_canceled():
                try:
                    items = next(batches)
                except StopIteration:
                    break
                self._emit(
                    on_event,
                    ImportBatch(project_id, handle.job_id, tuple(items)),
                )
                completed += len(items)
                self._emit(
                    on_event,
                    JobProgress(
                        project_id,
                        handle.job_id,
                        completed,
                        0,
                        message=f"已发现 {completed} 个照片更新",
                    ),
                )
            state = (
                JobState.CANCELED
                if handle._token.is_canceled()
                else JobState.SUCCEEDED
            )
        except Exception as error:
            state = JobState.FAILED
            message = f"{type(error).__name__}: {error}"
            logger.exception("Import coordinator failed for %s", root)
        finally:
            self._emit(
                on_event,
                JobFinished(
                    project_id,
                    handle.job_id,
                    state,
                    message=message,
                ),
            )
            handle._done.set()
