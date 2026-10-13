"""Subject-aware focus analysis with conservative conclusions."""

from __future__ import annotations

import cv2
import numpy as np

from src.xuanpian.domain.technical_analysis import FocusConclusion, FocusResult, Rect
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.af_focus import analyze_af_focus
from src.xuanpian.engine.subject_locator import SubjectLocationResult


class FocusAnalyzer:
    """Measure focus in the primary subject region before considering the scene."""

    def analyze(
        self, context: AnalysisContext, subjects: SubjectLocationResult
    ) -> FocusResult:
        if context.af_hint.bounds is not None:
            return analyze_af_focus(context.gray, context.af_hint, subjects.primary)
        legacy = self._analyze_legacy(context, subjects)
        reason = context.af_hint.reason
        reason_flag = "".join(
            character if character.isalnum() or character == "_" else "_"
            for character in reason.lower()
        ).strip("_") or "unknown"
        return FocusResult(
            legacy.conclusion,
            legacy.confidence,
            legacy.evidence + ((f"af_unavailable_{reason_flag}", 1.0),),
        )

    def _analyze_legacy(
        self, context: AnalysisContext, subjects: SubjectLocationResult
    ) -> FocusResult:
        primary = subjects.primary
        if primary is None and {"face", "pose"} <= subjects.failed_sources:
            return FocusResult(
                FocusConclusion.REVIEW,
                evidence=(("human_subject_capability_unavailable", 1.0),),
            )
        region = primary.bounds if primary is not None else Rect(0.0, 0.0, 1.0, 1.0)
        roi = self._roi(context.gray, region)
        if roi.size == 0 or min(roi.shape[:2]) < 24:
            return FocusResult(FocusConclusion.REVIEW, evidence=(("roi_too_small", 1.0),))

        texture = float(np.std(roi))
        if texture < 12.0:
            return FocusResult(
                FocusConclusion.REVIEW,
                confidence=0.3 if primary is not None else 0.0,
                evidence=(("texture", texture),),
            )

        laplacian = float(cv2.Laplacian(roi, cv2.CV_64F).var())
        grad_x = cv2.Sobel(roi, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(roi, cv2.CV_64F, 0, 1, ksize=3)
        tenengrad = float(np.mean(grad_x * grad_x + grad_y * grad_y))
        evidence = (("laplacian", laplacian), ("tenengrad", tenengrad), ("texture", texture))
        subject_confidence = primary.confidence if primary is not None else 0.45

        if laplacian >= 500.0 or (laplacian >= 100.0 and tenengrad >= 1_000.0):
            return FocusResult(FocusConclusion.CLEAR, subject_confidence, evidence)
        if (
            primary is not None
            and primary.confidence >= 0.5
            and laplacian >= 55.0
            and tenengrad >= 700.0
        ):
            return FocusResult(FocusConclusion.CLEAR, subject_confidence, evidence)
        if (
            primary is not None
            and primary.confidence >= 0.85
            and laplacian < 35.0
            and tenengrad < 400.0
        ):
            return FocusResult(FocusConclusion.OUT_OF_FOCUS, primary.confidence, evidence)
        return FocusResult(FocusConclusion.REVIEW, subject_confidence, evidence)

    @staticmethod
    def _roi(gray: np.ndarray, rect: Rect) -> np.ndarray:
        height, width = gray.shape[:2]
        x0, y0 = round(rect.x * width), round(rect.y * height)
        x1 = round((rect.x + rect.width) * width)
        y1 = round((rect.y + rect.height) * height)
        return gray[max(0, y0):min(height, y1), max(0, x0):min(width, x1)]
