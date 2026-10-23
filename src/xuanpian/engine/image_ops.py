"""Small deterministic image operations without a native video dependency."""

from __future__ import annotations
import numpy as np
from PIL import Image


def bgr_to_gray(bgr: np.ndarray) -> np.ndarray:
    result = np.empty(bgr.shape[:2], np.uint8)
    # Bound temporary allocations on full-resolution RAW/JPEG frames.
    for start in range(0, len(bgr), 256):
        block = bgr[start : start + 256].astype(np.uint32)
        gray = (
            block[:, :, 0] * 1868 + block[:, :, 1] * 9617 + block[:, :, 2] * 4899 + 8192
        ) >> 14
        result[start : start + 256] = gray.astype(np.uint8)
    return result


def resize_bgr(bgr: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(Image.fromarray(bgr).resize(size, Image.Resampling.BOX)).copy()


def laplacian(gray: np.ndarray) -> np.ndarray:
    padded = np.pad(gray.astype(np.int16), 1, mode="reflect")
    return (
        padded[:-2, 1:-1]
        + padded[2:, 1:-1]
        + padded[1:-1, :-2]
        + padded[1:-1, 2:]
        - 4 * padded[1:-1, 1:-1]
    )


def sobel(gray: np.ndarray, dx: int, dy: int) -> np.ndarray:
    if (dx, dy) not in ((1, 0), (0, 1)):
        raise ValueError("Only first-order Sobel derivatives are supported")
    p = np.pad(gray.astype(np.int16), 1, mode="reflect")
    if dx:
        return (
            p[:-2, 2:]
            + 2 * p[1:-1, 2:]
            + p[2:, 2:]
            - p[:-2, :-2]
            - 2 * p[1:-1, :-2]
            - p[2:, :-2]
        )
    return (
        p[2:, :-2]
        + 2 * p[2:, 1:-1]
        + p[2:, 2:]
        - p[:-2, :-2]
        - 2 * p[:-2, 1:-1]
        - p[:-2, 2:]
    )


def mean_square(values: np.ndarray) -> float:
    return float(
        np.einsum("ij,ij->", values, values, dtype=np.float64, optimize=False)
        / values.size
    )
