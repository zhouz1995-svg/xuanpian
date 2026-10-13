from __future__ import annotations

import numpy as np

from src.xuanpian.domain.technical_analysis import ExposureConclusion, ExposureResultV2


class ExposureAnalyzer:
    def __init__(self) -> None:
        pass

    @staticmethod
    def classify_percentages(over_pct: float, under_pct: float) -> ExposureConclusion:
        over = over_pct >= 10.0
        under = under_pct >= 30.0
        if over and under:
            return ExposureConclusion.BOTH
        if over:
            return ExposureConclusion.OVEREXPOSED
        if under:
            return ExposureConclusion.UNDEREXPOSED
        return ExposureConclusion.NORMAL

    def analyze_context(self, context: object) -> ExposureResultV2:
        """Classify clipping percentages after removing solid letterbox borders."""
        gray = getattr(context, "gray")
        valid = self._without_letterbox(np.asarray(gray))
        total_pixels = valid.size
        over_pct = float(np.sum(valid > 240) / total_pixels * 100.0)
        under_pct = float(np.sum(valid < 15) / total_pixels * 100.0)
        return ExposureResultV2(
            self.classify_percentages(over_pct, under_pct), over_pct, under_pct
        )

    @staticmethod
    def _without_letterbox(gray: np.ndarray) -> np.ndarray:
        top, bottom = 0, gray.shape[0]
        max_trim = max(1, gray.shape[0] // 5)
        while top < max_trim and np.mean(gray[top] < 15) >= 0.95:
            top += 1
        while bottom > gray.shape[0] - max_trim and np.mean(gray[bottom - 1] < 15) >= 0.95:
            bottom -= 1
        return gray[top:bottom] if top < bottom else gray
