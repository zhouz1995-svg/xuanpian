"""Shared image reading utilities with RAW format support.

For RAW files, tries full decode first; falls back to embedded JPEG preview
if the raw sensor data is corrupted or unsupported.
"""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

RAW_EXTENSIONS: set[str] = set()
try:
    import rawpy  # noqa: F401

    RAW_EXTENSIONS = {
        ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng",
        ".raf", ".orf", ".rw2", ".pef", ".srw", ".crw",
    }
except ImportError:
    logger.info("rawpy not available — RAW format support disabled")


def _is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def read_image_gray(path: str) -> np.ndarray:
    """Read image as grayscale numpy array. Supports RAW via rawpy."""
    if _is_raw(path):
        return _read_raw_gray(path)

    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Failed to read image: {path}")
    return img


def read_image_color(path: str) -> np.ndarray:
    """Read image as BGR color numpy array. Supports RAW via rawpy."""
    if _is_raw(path):
        return _read_raw_bgr(path)

    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to read image: {path}")
    return img


def _try_extract_thumb(path: str) -> np.ndarray | None:
    """Extract embedded JPEG preview from a RAW file and return as RGB numpy array."""
    try:
        import rawpy
        with rawpy.imread(path) as raw:
            thumb = raw.extract_thumb()
            if thumb is not None and thumb.format is not None:
                pil_img = Image.open(BytesIO(thumb.data))
                return np.array(pil_img.convert("RGB"))
    except Exception as exc:
        logger.warning(
            "Embedded RAW preview extraction failed for %s: %s",
            path,
            exc,
        )
    return None


def _read_raw_gray(path: str) -> np.ndarray:
    import rawpy

    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                                  no_auto_bright=True)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    except Exception:
        logger.debug("RAW decode failed for %s, trying embedded preview", path)

    thumb = _try_extract_thumb(path)
    if thumb is not None:
        return cv2.cvtColor(thumb, cv2.COLOR_RGB2GRAY)

    raise ValueError(f"Failed to read RAW file (no embedded preview): {path}")


def _read_raw_bgr(path: str) -> np.ndarray:
    import rawpy

    try:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                                  no_auto_bright=True)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        logger.debug("RAW decode failed for %s, trying embedded preview", path)

    thumb = _try_extract_thumb(path)
    if thumb is not None:
        return cv2.cvtColor(thumb, cv2.COLOR_RGB2BGR)

    raise ValueError(f"Failed to read RAW file (no embedded preview): {path}")
