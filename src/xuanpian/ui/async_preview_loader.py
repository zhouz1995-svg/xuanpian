"""Public bounded async preview loading boundary for Qt owner widgets."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    QSize,
    QThread,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QImage, QImageReader

from src.xuanpian.ui.image_model import RAW_EXTENSIONS

logger = logging.getLogger(__name__)
PREVIEW_MAX_EDGE = 2560
PREVIEW_SHUTDOWN_GRACE_MS = 100
PREVIEW_EXIT_TERMINATE_WAIT_MS = 1_000
DEFAULT_PREVIEW_MAX_EDGE = PREVIEW_MAX_EDGE
PreviewDecoder = Callable[[str, int], QImage]


def _pil_preview_to_qimage(image: object, max_edge: int) -> QImage:
    from PIL import Image, ImageOps

    if not isinstance(image, Image.Image):
        return QImage()
    image = ImageOps.exif_transpose(image)
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="BMP")
    return QImage.fromData(buffer.getvalue())


def _decode_raw_preview(path: str, max_edge: int) -> QImage:
    import rawpy
    from PIL import Image

    with rawpy.imread(path) as raw:
        embedded_error: Exception | None = None
        try:
            thumb = raw.extract_thumb()
            embedded = QImage()
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(BytesIO(thumb.data)) as image:
                    image.load()
                    embedded = _pil_preview_to_qimage(image, max_edge)
            elif (
                thumb is not None
                and thumb.format == rawpy.ThumbFormat.BITMAP
            ):
                embedded = _pil_preview_to_qimage(
                    Image.fromarray(thumb.data),
                    max_edge,
                )
            if not embedded.isNull():
                return embedded
            if thumb is not None:
                raise OSError("embedded RAW preview is empty")
        except Exception as error:
            embedded_error = error
            logger.debug(
                "Embedded RAW preview unavailable for %s: %s",
                path,
                error,
                exc_info=True,
            )
        try:
            rgb = raw.postprocess(
                half_size=True,
                use_camera_wb=True,
                no_auto_bright=True,
            )
            fallback = _pil_preview_to_qimage(
                Image.fromarray(rgb, mode="RGB"),
                max_edge,
            )
            if fallback.isNull():
                raise OSError("RAW postprocess returned an empty image")
            return fallback
        except Exception as fallback_error:
            if embedded_error is not None:
                raise fallback_error from embedded_error
            raise


def decode_preview_image(path: str, max_edge: int = PREVIEW_MAX_EDGE) -> QImage:
    """Decode a bounded preview into a worker-safe QImage."""
    if max_edge <= 0:
        raise ValueError("max_edge must be positive")
    if Path(path).suffix.lower() in RAW_EXTENSIONS:
        image = _decode_raw_preview(path, max_edge)
    else:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > max_edge:
            scale = max_edge / max(size.width(), size.height())
            reader.setScaledSize(
                QSize(
                    max(1, round(size.width() * scale)),
                    max(1, round(size.height() * scale)),
                )
            )
        image = reader.read()
        if image.isNull():
            raise OSError(reader.errorString() or "image decode failed")
    if image.isNull():
        raise OSError("image decode returned an empty image")
    return image


@dataclass(frozen=True)
class _PreviewRequest:
    photo_id: str
    path: str
    token: int
    max_edge: int


class _PreviewDecodeThread(QThread):
    decoded = pyqtSignal(str, int, QImage)
    failed = pyqtSignal(str, int, str)

    def __init__(
        self,
        request: _PreviewRequest,
        decoder: PreviewDecoder,
    ) -> None:
        super().__init__()
        self.request = request
        self._decoder = decoder

    def run(self) -> None:
        request = self.request
        if self.isInterruptionRequested():
            return
        try:
            image = self._decoder(request.path, request.max_edge)
            if self.isInterruptionRequested():
                return
            if image is None or image.isNull():
                raise OSError("image decode returned an empty image")
        except Exception as error:
            if self.isInterruptionRequested():
                return
            self.failed.emit(
                request.photo_id,
                request.token,
                f"{type(error).__name__}: {error}",
            )
            return
        if self.isInterruptionRequested():
            return
        self.decoded.emit(request.photo_id, request.token, image)


class _PreviewDecodeReaper(QObject):
    """Own decode threads independently of any closing preview widget."""

    def __init__(self) -> None:
        super().__init__()
        self._threads: set[_PreviewDecodeThread] = set()
        self._connected_application: QCoreApplication | None = None

    def track(self, thread: _PreviewDecodeThread) -> None:
        self._connect_application_shutdown()
        self._threads.add(thread)
        thread.finished.connect(self._thread_finished)

    def _connect_application_shutdown(self) -> None:
        application = QCoreApplication.instance()
        if application is None or application is self._connected_application:
            return
        application.aboutToQuit.connect(self.shutdown_for_application_exit)
        self._connected_application = application

    @pyqtSlot()
    def _thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, _PreviewDecodeThread):
            return
        self._threads.discard(thread)
        thread.deleteLater()

    @pyqtSlot()
    def shutdown_for_application_exit(self) -> None:
        """Use termination only as the final process-exit safety policy."""
        threads = tuple(self._threads)
        for thread in threads:
            thread.requestInterruption()

        deadline = time.monotonic() + PREVIEW_SHUTDOWN_GRACE_MS / 1_000
        for thread in threads:
            remaining_ms = max(
                0,
                round((deadline - time.monotonic()) * 1_000),
            )
            if thread.isRunning() and remaining_ms:
                thread.wait(remaining_ms)

        for thread in threads:
            if not thread.isRunning():
                continue
            logger.warning(
                "Terminating an unresponsive preview decoder during app exit"
            )
            thread.terminate()
            thread.wait(PREVIEW_EXIT_TERMINATE_WAIT_MS)

    @property
    def count(self) -> int:
        return len(self._threads)


_PREVIEW_DECODE_REAPER = _PreviewDecodeReaper()


def preview_decode_reaper_count() -> int:
    """Expose outstanding thread ownership for lifecycle diagnostics/tests."""
    return _PREVIEW_DECODE_REAPER.count


class BoundedPreviewLoader(QObject):
    """Run one decode and coalesce pending work to the latest request.

    Results are forwarded from this object's owner thread. ``invalidate``
    suppresses all older results, and ``close`` transfers any unresponsive
    worker's remaining lifetime to the application's shared decode reaper.
    """

    decoded = pyqtSignal(str, QImage)
    error = pyqtSignal(str, str)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        decoder: PreviewDecoder = decode_preview_image,
    ) -> None:
        super().__init__(parent)
        self._decoder = decoder
        self._generation = 0
        self._active: _PreviewDecodeThread | None = None
        self._pending: _PreviewRequest | None = None
        self._closed = False

    def request(self, photo_id: str, path: str, max_edge: int) -> int:
        if self._closed:
            raise RuntimeError("preview loader is closed")
        if not isinstance(photo_id, str) or not photo_id:
            raise ValueError("photo_id must be a non-empty string")
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if max_edge <= 0:
            raise ValueError("max_edge must be positive")
        self._generation += 1
        request = _PreviewRequest(
            photo_id,
            path,
            self._generation,
            max_edge,
        )
        active = self._active
        if active is not None and active.isRunning():
            self._pending = request
        else:
            self._pending = None
            self._start(request)
        return request.token

    def invalidate(self) -> None:
        self._generation += 1
        self._pending = None

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    @property
    def unfinished_count(self) -> int:
        active = self._active
        return int(active is not None and active.isRunning()) + self.pending_count

    @property
    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.invalidate()
        active = self._active
        if active is not None:
            for signal, slot in (
                (active.decoded, self._on_decoded),
                (active.failed, self._on_error),
                (active.finished, self._on_finished),
            ):
                try:
                    signal.disconnect(slot)
                except TypeError:
                    pass
            if active.isRunning():
                active.requestInterruption()
                active.wait(PREVIEW_SHUTDOWN_GRACE_MS)
        self._active = None

    def _start(self, request: _PreviewRequest) -> None:
        thread = _PreviewDecodeThread(request, self._decoder)
        self._active = thread
        thread.decoded.connect(self._on_decoded)
        thread.failed.connect(self._on_error)
        thread.finished.connect(self._on_finished)
        _PREVIEW_DECODE_REAPER.track(thread)
        thread.start()

    def _on_decoded(self, photo_id: str, token: int, image: QImage) -> None:
        if not self._closed and token == self._generation:
            self.decoded.emit(photo_id, image)

    def _on_error(self, photo_id: str, token: int, message: str) -> None:
        if not self._closed and token == self._generation:
            self.error.emit(photo_id, message)

    def _on_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, _PreviewDecodeThread):
            return
        if self._active is not thread:
            return
        self._active = None
        request = self._pending
        self._pending = None
        if request is not None and not self._closed:
            self._start(request)
