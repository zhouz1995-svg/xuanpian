"""Subject-aware focus analysis with conservative conclusions."""

from __future__ import annotations

import numpy as np

from src.xuanpian.domain.technical_analysis import (
    FocusConclusion,
    FocusResult,
    Rect,
    SubjectCandidate,
)
from src.xuanpian.engine import image_ops
from src.xuanpian.engine.af_focus import analyze_af_focus
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.subject_locator import SubjectLocationResult, SubjectLocator


class FocusAnalyzer:
    """Measure focus in the primary subject region before considering the scene."""

    def analyze(
        self, context: AnalysisContext, subjects: SubjectLocationResult
    ) -> FocusResult:
        if context.af_hint.bounds is not None:
            result = analyze_af_focus(context.gray, context.af_hint, subjects.primary)
            primary = subjects.primary
            if (
                primary is not None
                and primary.kind == "face"
                and "af_review_subject_conflict" in dict(result.evidence)
            ):
                # A face and its body are the same subject. Retain the old
                # body envelope for AF validation when the new face box is tighter.
                people = sorted(
                    (
                        candidate
                        for candidate in subjects.subjects
                        if candidate.kind == "person"
                        and candidate.confidence >= 0.65
                        and SubjectLocator.face_in_person(
                            primary.bounds, candidate.bounds
                        )
                    ),
                    key=lambda candidate: candidate.salience,
                )
                for person in people:
                    envelope = analyze_af_focus(context.gray, context.af_hint, person)
                    if "af_review_subject_conflict" not in dict(envelope.evidence):
                        return FocusResult(
                            envelope.conclusion,
                            envelope.confidence,
                            envelope.evidence + (("af_primary_envelope", 1.0),),
                        )
            return result
        legacy = self._analyze_legacy(context, subjects)
        reason = context.af_hint.reason
        reason_flag = (
            "".join(
                character if character.isalnum() or character == "_" else "_"
                for character in reason.lower()
            ).strip("_")
            or "unknown"
        )
        return FocusResult(
            legacy.conclusion,
            legacy.confidence,
            legacy.evidence + ((f"af_unavailable_{reason_flag}", 1.0),),
        )

    def _analyze_legacy(
        self, context: AnalysisContext, subjects: SubjectLocationResult
    ) -> FocusResult:
        primary = subjects.primary
        if primary is None and {"face", "object"} <= subjects.failed_sources:
            return FocusResult(
                FocusConclusion.REVIEW,
                evidence=(("subject_capability_unavailable", 1.0),),
            )
        if primary is None:
            salient = subjects.face_count > 0 or any(
                candidate.salience >= 0.02 for candidate in subjects.subjects
            )
            if salient or subjects.failed_sources:
                return FocusResult(
                    FocusConclusion.REVIEW,
                    evidence=(("subject_ambiguous_or_unavailable", 1.0),),
                )
            return self._scene_focus(context.gray)
        region = self._subject_region(primary)
        roi = self._roi(context.gray, region)
        if roi.size == 0 or min(roi.shape[:2]) < 24:
            return FocusResult(
                FocusConclusion.REVIEW, evidence=(("roi_too_small", 1.0),)
            )

        texture = float(np.std(roi))
        if texture < 12.0:
            return FocusResult(
                FocusConclusion.REVIEW,
                confidence=0.3 if primary is not None else 0.0,
                evidence=(("texture", texture),),
            )

        laplacian, tenengrad = self._metrics(roi)
        evidence = (
            ("laplacian", laplacian),
            ("tenengrad", tenengrad),
            ("texture", texture),
            ("subject_roi_x", region.x),
            ("subject_roi_y", region.y),
            ("subject_roi_width", region.width),
            ("subject_roi_height", region.height),
        )
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
            return FocusResult(
                FocusConclusion.OUT_OF_FOCUS, primary.confidence, evidence
            )
        return FocusResult(FocusConclusion.REVIEW, subject_confidence, evidence)

    @staticmethod
    def _subject_region(primary: SubjectCandidate) -> Rect:
        box = primary.bounds
        x0, y0 = box.x + 0.15 * box.width, box.y + 0.15 * box.height
        x1, y1 = box.x + 0.85 * box.width, box.y + 0.85 * box.height
        if primary.kind == "person":
            y0, y1 = box.y + 0.08 * box.height, box.y + 0.60 * box.height
        elif primary.kind == "face" and len(primary.keypoints) >= 3:
            points = primary.keypoints[:3]
            if all(
                point.confidence >= 0.65
                and box.x <= point.x <= box.x + box.width
                and box.y <= point.y <= box.y + box.height
                for point in points
            ):
                left, right = min(p.x for p in points), max(p.x for p in points)
                top, bottom = min(p.y for p in points), max(p.y for p in points)
                if right > left and bottom > top:
                    x0, x1 = (
                        max(box.x, left - 0.08 * box.width),
                        min(box.x + box.width, right + 0.08 * box.width),
                    )
                    y0, y1 = (
                        max(box.y, top - 0.08 * box.height),
                        min(box.y + box.height, bottom + 0.12 * box.height),
                    )
        return Rect(x0, y0, x1 - x0, y1 - y0)

    @staticmethod
    def _metrics(roi: np.ndarray) -> tuple[float, float]:
        # Exclude the crop boundary so a padded detection box cannot create edges.
        lap = image_ops.laplacian(roi)[1:-1, 1:-1]
        lap_variance = float(np.var(lap, dtype=np.float64))
        tenengrad = sum(
            image_ops.mean_square(image_ops.sobel(roi, dx, dy)[1:-1, 1:-1])
            for dx, dy in ((1, 0), (0, 1))
        )
        return lap_variance, tenengrad

    def _scene_focus(self, gray: np.ndarray) -> FocusResult:
        metrics = []
        for y in (0.0, 0.25, 0.5):
            for x in (0.0, 0.25, 0.5):
                roi = self._roi(gray, Rect(x, y, 0.5, 0.5))
                if min(roi.shape) < 24 or float(np.std(roi)) < 12:
                    continue
                lap, ten = self._metrics(roi)
                weight = 2 if (x, y) == (0.25, 0.25) else 1
                metrics.extend([(lap, ten)] * weight)
        if len(metrics) < 5:
            return FocusResult(
                FocusConclusion.REVIEW, evidence=(("scene_insufficient_texture", 1.0),)
            )
        lap, ten = np.median(metrics, axis=0)
        clear = lap >= 500 or (lap >= 100 and ten >= 1000)
        return FocusResult(
            FocusConclusion.CLEAR if clear else FocusConclusion.REVIEW,
            0.45,
            (
                ("scene_regions", float(len(metrics))),
                ("laplacian", float(lap)),
                ("tenengrad", float(ten)),
            ),
        )

    @staticmethod
    def _roi(gray: np.ndarray, rect: Rect) -> np.ndarray:
        height, width = gray.shape[:2]
        x0, y0 = round(rect.x * width), round(rect.y * height)
        x1 = round((rect.x + rect.width) * width)
        y1 = round((rect.y + rect.height) * height)
        return gray[max(0, y0) : min(height, y1), max(0, x0) : min(width, x1)]
