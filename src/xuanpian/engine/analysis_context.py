from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from src.xuanpian.engine.image_ops import bgr_to_gray, resize_bgr

from src.xuanpian.domain.technical_analysis import Rect
from src.xuanpian.engine.af_metadata import AFHint, read_af_hint
from src.xuanpian.engine.image_reader import read_image_color


@dataclass(frozen=True, slots=True)
class AnalysisContext:
    path: str
    bgr: np.ndarray
    gray: np.ndarray
    locate_bgr: np.ndarray
    locate_gray: np.ndarray
    scale_x: float
    scale_y: float
    af_hint: AFHint = AFHint()

    @classmethod
    def from_path(cls, path: str, *, locate_long_edge: int = 1024) -> "AnalysisContext":
        if locate_long_edge <= 0:
            raise ValueError("locate_long_edge must be positive")
        bgr = read_image_color(path)
        if bgr.ndim != 3 or bgr.shape[2] != 3:
            raise ValueError(f"Failed to read color image: {path}")
        height, width = bgr.shape[:2]
        longest = max(width, height)
        scale = min(1.0, locate_long_edge / longest)
        locate_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        locate_bgr = bgr if scale == 1.0 else resize_bgr(bgr, locate_size)
        return cls(
            path=path,
            bgr=bgr,
            gray=bgr_to_gray(bgr),
            locate_bgr=locate_bgr,
            locate_gray=bgr_to_gray(locate_bgr),
            scale_x=locate_bgr.shape[1] / width,
            scale_y=locate_bgr.shape[0] / height,
            af_hint=read_af_hint(path, (width, height)),
        )

    def to_source_rect(self, rect: Rect) -> Rect:
        """Normalized regions are invariant across locator and source images."""
        return rect
