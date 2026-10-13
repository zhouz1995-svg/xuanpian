from __future__ import annotations

import logging
import os
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    wait,
)
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from threading import Event, Thread
from typing import Callable, Iterable
from uuid import uuid4

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.services.analysis_service import (
    PhotoAnalysisRequest,
    PhotoAnalysisResponse,
    analyze_photo,
)
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
class AnalysisResult:
    project_id: ProjectId
    job_id: JobId
    response: PhotoAnalysisResponse


class AnalysisCoordinator:
    def __init__(
        self,
        max_workers: int | None = None,
        executor_factory: Callable[..., object] = ProcessPoolExecutor,
    ) -> None:
        cpu_count = os.cpu_count() or 1
        self._max_workers = max_workers or min(max(cpu_count - 1, 1), 4)
        if self._max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._executor_factory = executor_factory

    def start(
        self,
        project_id: ProjectId,
        requests: Iterable[PhotoAnalysisRequest],
        on_event: Callable[[object], None],
    ) -> JobHandle:
        job_id = uuid4()
        token = CancellationToken()
        done = Event()
        handle = JobHandle(job_id=job_id, _token=token, _done=done)
        request_items = tuple(requests)
        Thread(
            target=self._run_job,
            args=(project_id, handle, request_items, on_event),
            name=f"analysis-{job_id}",
            daemon=True,
        ).start()
        return handle

    @staticmethod
    def _emit(on_event: Callable[[object], None], event: object) -> None:
        try:
            on_event(event)
        except Exception:
            logger.exception("Analysis job event callback failed")

    def _run_job(
        self,
        project_id: ProjectId,
        handle: JobHandle,
        requests: tuple[PhotoAnalysisRequest, ...],
        on_event: Callable[[object], None],
    ) -> None:
        total = len(requests)
        completed = 0
        failed = 0
        state = JobState.RUNNING
        terminal_message = ""
        executor = None
        pending: dict[Future, PhotoAnalysisRequest] = {}
        next_index = 0

        def emit_progress(message: str = "") -> None:
            self._emit(
                on_event,
                JobProgress(
                    project_id=project_id,
                    job_id=handle.job_id,
                    completed=completed,
                    total=total,
                    failed=failed,
                    message=message,
                ),
            )

        def fill_window() -> None:
            nonlocal next_index
            limit = self._max_workers * 2
            while (
                not handle._token.is_canceled()
                and next_index < total
                and len(pending) < limit
            ):
                request = requests[next_index]
                next_index += 1
                pending[executor.submit(analyze_photo, request)] = request

        emit_progress("分析任务已启动")
        try:
            executor = self._executor_factory(max_workers=self._max_workers)
            fill_window()
            while pending:
                completed_futures, _ = wait(
                    tuple(pending),
                    return_when=FIRST_COMPLETED,
                )
                pool_failed = False
                for future in completed_futures:
                    request = pending.pop(future)
                    completed += 1
                    try:
                        response = future.result()
                    except BrokenProcessPool as error:
                        failed += 1
                        state = JobState.FAILED
                        terminal_message = str(error)
                        emit_progress(terminal_message)
                        pool_failed = True
                        break
                    except Exception as error:
                        failed += 1
                        emit_progress(
                            f"{request.photo_id}: {type(error).__name__}: {error}"
                        )
                        continue

                    if any(
                        outcome.status is AnalysisStatus.FAILED
                        for outcome in response.outcomes
                    ):
                        failed += 1
                    self._emit(
                        on_event,
                        AnalysisResult(
                            project_id=project_id,
                            job_id=handle.job_id,
                            response=response,
                        ),
                    )
                    emit_progress()

                if pool_failed:
                    break
                fill_window()

            if state is not JobState.FAILED:
                state = (
                    JobState.CANCELED
                    if handle._token.is_canceled()
                    else JobState.SUCCEEDED
                )
        except Exception as error:
            state = JobState.FAILED
            terminal_message = f"{type(error).__name__}: {error}"
            logger.exception("Analysis coordinator failed")
        finally:
            if executor is not None:
                executor.shutdown(
                    wait=state is not JobState.FAILED,
                    cancel_futures=state is JobState.FAILED,
                )
            self._emit(
                on_event,
                JobFinished(
                    project_id=project_id,
                    job_id=handle.job_id,
                    state=state,
                    failed=failed,
                    message=terminal_message,
                ),
            )
            handle._done.set()
