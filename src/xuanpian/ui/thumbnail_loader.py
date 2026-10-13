from __future__ import annotations

import logging
import traceback
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage

from src.xuanpian.ui.image_model import RAW_EXTENSIONS

logger = logging.getLogger(__name__)


def _pil_to_qimage(
    image: Image.Image,
    thumbnail_size: tuple[int, int],
) -> QImage | None:
    image = ImageOps.exif_transpose(image)
    image.thumbnail(thumbnail_size, Image.Resampling.LANCZOS)
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGB")
    buffer = BytesIO()
    image.save(buffer, format="BMP")
    qimage = QImage.fromData(buffer.getvalue())
    return qimage if not qimage.isNull() else None


def _load_raw_thumbnail(
    path: str,
    thumbnail_size: tuple[int, int],
) -> QImage | None:
    import rawpy

    with rawpy.imread(path) as raw:
        try:
            thumb = raw.extract_thumb()
        except Exception as error:
            logger.debug(
                "Embedded RAW thumbnail unavailable for %s: %s",
                path,
                error,
                exc_info=True,
            )
            thumb = None
        if (
            thumb is not None
            and thumb.format == rawpy.ThumbFormat.JPEG
        ):
            with Image.open(BytesIO(thumb.data)) as image:
                return _pil_to_qimage(image, thumbnail_size)
        if (
            thumb is not None
            and thumb.format == rawpy.ThumbFormat.BITMAP
        ):
            image = Image.fromarray(thumb.data)
            return _pil_to_qimage(image, thumbnail_size)

        rgb = raw.postprocess(
            half_size=True,
            use_camera_wb=True,
            no_auto_bright=True,
        )
        image = Image.fromarray(rgb, mode="RGB")
        return _pil_to_qimage(image, thumbnail_size)


def load_thumbnail(
    path: str,
    thumbnail_size: tuple[int, int] = (200, 150),
) -> QImage | None:
    """Decode a thumbnail as QImage without creating GUI-owned objects."""
    if Path(path).suffix.lower() in RAW_EXTENSIONS:
        return _load_raw_thumbnail(path, thumbnail_size)
    with Image.open(path) as image:
        return _pil_to_qimage(image, thumbnail_size)


class ThumbnailLoader(QThread):
    """Background thread for async thumbnail generation."""

    thumbnail_ready = pyqtSignal(int, QImage)
    finished_loading = pyqtSignal(int, int)  # (total, loaded_count)
    load_error = pyqtSignal(int, str)  # (index, error_message)

    def __init__(
        self,
        image_paths: list[str],
        thumbnail_size: tuple[int, int] = (200, 150),
        parent: QThread | None = None,
    ) -> None:
        super().__init__(parent)
        self._image_paths = image_paths
        self._thumbnail_size = thumbnail_size
        self._stopped = False
        self._loaded = 0

    def run(self) -> None:
        logger.info(f"开始加载 {len(self._image_paths)} 张缩略图...")
        for i, path in enumerate(self._image_paths):
            if self._stopped:
                break
            try:
                qimage = load_thumbnail(path, self._thumbnail_size)
                if qimage is not None and not qimage.isNull():
                    self.thumbnail_ready.emit(i, qimage)
                    self._loaded += 1
            except Exception as e:
                logger.warning(f"缩略图加载失败 [{path}]: {e}")
                self.load_error.emit(i, f"{type(e).__name__}: {e}")
                continue
        logger.info(f"缩略图加载完成: {self._loaded}/{len(self._image_paths)}")
        self.finished_loading.emit(len(self._image_paths), self._loaded)

    def _load_standard_thumbnail(self, path: str) -> QImage | None:
        with Image.open(path) as image:
            return _pil_to_qimage(image, self._thumbnail_size)

    def _load_raw_thumbnail(self, path: str) -> QImage | None:
        return _load_raw_thumbnail(path, self._thumbnail_size)

    def _pil_to_qimage(self, img: Image.Image) -> QImage | None:
        return _pil_to_qimage(img, self._thumbnail_size)

    def stop(self) -> None:
        self._stopped = True
