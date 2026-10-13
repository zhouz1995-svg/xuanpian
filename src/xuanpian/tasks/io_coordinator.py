from __future__ import annotations

import logging
import os
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import BoundedSemaphore, Event, Thread
from typing import Callable, Iterable

from PyQt6.QtGui import QImage

from src.xuanpian.cache.thumbnail_cache import (
    ThumbnailCache,
    ThumbnailCacheError,
    ThumbnailKey,
)
from src.xuanpian.engine.exif_reader import ExifData, read_exif
from src.xuanpian.services.fingerprints import fingerprint
from src.xuanpian.services.export_service import (
    ExportReport,
    ExportRequest,
    ExportService,
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
from src.xuanpian.ui.thumbnail_loader import load_thumbnail


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetadataRequest:
    project_id: ProjectId
    job_id: JobId
    photo_id: str
    path: str
    thumbnail_size: tuple[int, int]


@dataclass(frozen=True, slots=True)
class MetadataResult:
    project_id: ProjectId
    job_id: JobId
    photo_id: str
    path: str
    qimage: QImage | None
    exif: ExifData
    error: str = ""


@dataclass(frozen=True, slots=True)
class ExportStarted:
    project_id: ProjectId
    job_id: JobId


@dataclass(frozen=True, slots=True)
class ExportProgress:
    project_id: ProjectId
    job_id: JobId
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class ExportFinished:
    project_id: ProjectId
    job_id: JobId
    report: ExportReport | None
    error: str = ""
    recovery_paths: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()


def load_metadata(
    request: MetadataRequest,
    *,
    thumbnail_cache: ThumbnailCache | None = None,
    thumbnail_decoder: Callable[
        [str, tuple[int, int]], QImage | None
    ] | None = None,
    exif_loader: Callable[[str], ExifData] | None = None,
) -> MetadataResult:
    errors: list[str] = []
    qimage = None
    decoder = thumbnail_decoder or load_thumbnail
    metadata_reader = exif_loader or read_exif
    try:
        if thumbnail_cache is not None:
            key = ThumbnailKey(
                fingerprint(Path(request.path)),
                *request.thumbnail_size,
            )
            try:
                qimage = thumbnail_cache.get_or_create(
                    key,
                    lambda: decoder(
                        request.path,
                        request.thumbnail_size,
                    ),
                )
            except ThumbnailCacheError as error:
                logger.warning(
                    "Thumbnail cache unavailable for %s; decoding directly: %s",
                    request.path,
                    error,
                )
                qimage = decoder(
                    request.path,
                    request.thumbnail_size,
                )
        else:
            qimage = decoder(
                request.path,
                request.thumbnail_size,
            )
        if qimage is None or qimage.isNull():
            errors.append(f"{request.path}: thumbnail decode returned no image")
    except Exception as error:
        errors.append(
            f"{request.path}: {type(error).__name__}: {error}"
        )

    exif = metadata_reader(request.path)
    if exif.error:
        errors.append(exif.error)
    return MetadataResult(
        project_id=request.project_id,
        job_id=request.job_id,
        photo_id=request.photo_id,
        path=request.path,
        qimage=qimage,
        exif=exif,
        error="; ".join(errors),
    )


class IoCoordinator:
    def __init__(
        self,
        max_workers: int = 4,
        executor_factory: Callable[..., object] = ThreadPoolExecutor,
        export_service: ExportService | None = None,
        *,
        thumbnail_cache: ThumbnailCache | None = None,
        thumbnail_decoder: Callable[
            [str, tuple[int, int]], QImage | None
        ] | None = None,
        exif_loader: Callable[[str], ExifData] | None = None,
        metadata_loader: Callable[[MetadataRequest], MetadataResult]
        | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        self._max_workers = min(max_workers, 4)
        self._executor = executor_factory(max_workers=self._max_workers)
        self._capacity = BoundedSemaphore(self._max_workers * 2)
        self._export_service = export_service or ExportService()
        if metadata_loader is not None:
            self._metadata_loader = metadata_loader
        elif any(
            dependency is not None
            for dependency in (
                thumbnail_cache,
                thumbnail_decoder,
                exif_loader,
            )
        ):
            self._metadata_loader = partial(
                load_metadata,
                thumbnail_cache=thumbnail_cache,
                thumbnail_decoder=thumbnail_decoder,
                exif_loader=exif_loader,
            )
        else:
            self._metadata_loader = load_metadata

    def submit_metadata(
        self,
        requests: Iterable[MetadataRequest],
        on_event: Callable[[object], None],
    ) -> JobHandle:
        items = tuple(requests)
        if not items:
            raise ValueError("at least one metadata request is required")
        first = items[0]
        if any(
            item.project_id != first.project_id
            or item.job_id != first.job_id
            for item in items
        ):
            raise ValueError("metadata requests must share project/job identity")
        handle = JobHandle(
            first.job_id,
            CancellationToken(),
            Event(),
        )
        Thread(
            target=self._run,
            args=(handle, items, on_event),
            name=f"metadata-{first.job_id}",
            daemon=True,
        ).start()
        return handle

    def submit_export(
        self,
        project_id: ProjectId,
        request: ExportRequest,
        on_event: Callable[[object], None],
    ) -> JobHandle:
        from uuid import uuid4

        handle = JobHandle(
            uuid4(),
            CancellationToken(),
            Event(),
        )
        Thread(
            target=self._run_export,
            args=(project_id, handle, request, on_event),
            name=f"export-{handle.job_id}",
            daemon=True,
        ).start()
        return handle

    @staticmethod
    def _emit(on_event: Callable[[object], None], event: object) -> None:
        try:
            on_event(event)
        except Exception:
            logger.exception("Metadata job event callback failed")

    def _run(
        self,
        handle: JobHandle,
        requests: tuple[MetadataRequest, ...],
        on_event: Callable[[object], None],
    ) -> None:
        first = requests[0]
        total = len(requests)
        next_index = 0
        completed = 0
        failed = 0
        pending: dict[Future, MetadataRequest] = {}
        message = ""

        def fill_window() -> None:
            nonlocal next_index
            while (
                not handle._token.is_canceled()
                and next_index < total
                and len(pending) < self._max_workers * 2
            ):
                if not self._capacity.acquire(timeout=0.05):
                    break
                request = requests[next_index]
                next_index += 1
                try:
                    future = self._executor.submit(
                        self._metadata_loader,
                        request,
                    )
                except Exception:
                    self._capacity.release()
                    raise
                future.add_done_callback(
                    lambda completed_future: self._capacity.release()
                )
                pending[future] = request

        self._emit(
            on_event,
            JobProgress(
                first.project_id,
                first.job_id,
                0,
                total,
                message="元数据任务已启动",
            ),
        )
        try:
            while next_index < total or pending:
                fill_window()
                if not pending:
                    if handle._token.is_canceled():
                        break
                    continue
                completed_futures, _ = wait(
                    tuple(pending),
                    return_when=FIRST_COMPLETED,
                )
                for future in completed_futures:
                    request = pending.pop(future)
                    completed += 1
                    try:
                        result = future.result()
                    except Exception as error:
                        failed += 1
                        message = (
                            f"{request.path}: "
                            f"{type(error).__name__}: {error}"
                        )
                        logger.exception(
                            "Metadata worker failed for %s",
                            request.path,
                        )
                    else:
                        failed += bool(result.error)
                        self._emit(on_event, result)
                    self._emit(
                        on_event,
                        JobProgress(
                            first.project_id,
                            first.job_id,
                            completed,
                            total,
                            failed=failed,
                            message=message,
                        ),
                    )
        except Exception as error:
            failed += 1
            message = f"{type(error).__name__}: {error}"
            logger.exception("Metadata coordinator failed")
            state = JobState.FAILED
        else:
            state = (
                JobState.CANCELED
                if handle._token.is_canceled()
                else JobState.SUCCEEDED
            )
        finally:
            self._emit(
                on_event,
                JobFinished(
                    first.project_id,
                    first.job_id,
                    state,
                    failed=failed,
                    message=message,
                ),
            )
            handle._done.set()

    def _run_export(
        self,
        project_id: ProjectId,
        handle: JobHandle,
        request: ExportRequest,
        on_event: Callable[[object], None],
    ) -> None:
        self._emit(on_event, ExportStarted(project_id, handle.job_id))
        acquired = False
        future = None
        try:
            while not handle._token.is_canceled():
                if self._capacity.acquire(timeout=0.05):
                    acquired = True
                    break
            if not acquired:
                report = ExportReport((), canceled=True)
            else:
                future = self._executor.submit(
                    self._export_service.export,
                    request,
                    handle._token.is_canceled,
                )
                future.add_done_callback(
                    lambda completed_future: self._capacity.release()
                )
                report = future.result()
            self._emit(
                on_event,
                ExportProgress(
                    project_id,
                    handle.job_id,
                    len(report.unit_results) or len(report.results),
                    len(request.units) or len(request.items),
                ),
            )
            self._emit(
                on_event,
                ExportFinished(
                    project_id,
                    handle.job_id,
                    report,
                ),
            )
        except BaseException as error:
            if acquired and future is None:
                self._capacity.release()
            logger.exception("Background export failed")
            self._emit(
                on_event,
                ExportFinished(
                    project_id,
                    handle.job_id,
                    None,
                    f"{type(error).__name__}: {error}",
                    tuple(
                        path
                        for path in getattr(error, "recovery_paths", ())
                        if isinstance(path, Path) and os.path.lexists(path)
                    ),
                    tuple(getattr(error, "__notes__", ())),
                ),
            )
            if not isinstance(error, Exception):
                raise
        finally:
            handle._done.set()

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
