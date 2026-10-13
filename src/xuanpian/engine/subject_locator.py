"""Conservative mixed-subject location for technical photo analysis."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from math import isfinite
from typing import Any

from src.xuanpian.domain.technical_analysis import Keypoint, Rect, SubjectCandidate
from src.xuanpian.engine.analysis_context import AnalysisContext


SUBJECT_SOURCES = frozenset({"face", "pose", "object"})
_DUPLICATE_SUBJECT_IOU = 0.65


@dataclass(frozen=True, slots=True)
class SubjectLocationResult:
    subjects: tuple[SubjectCandidate, ...]
    primary_index: int | None
    face_count: int
    diagnostics: tuple[str, ...] = ()
    failed_sources: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.failed_sources, Set):
            raise ValueError("failed sources must be a set-like container")
        failed_sources = frozenset(self.failed_sources)
        if not failed_sources <= SUBJECT_SOURCES:
            raise ValueError("unsupported failed source")
        object.__setattr__(self, "failed_sources", failed_sources)

    @property
    def primary(self) -> SubjectCandidate | None:
        if self.primary_index is None:
            return None
        return self.subjects[self.primary_index]


class SubjectLocator:
    """Find candidates without inventing a primary subject on weak evidence."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def locate(self, context: AnalysisContext) -> SubjectLocationResult:
        subjects: list[SubjectCandidate] = []
        diagnostics: list[str] = []
        failed_sources: set[str] = set()
        face_count = 0
        for source, method_name in (
            ("face", "detect_faces"),
            ("pose", "detect_poses"),
            ("object", "detect_objects"),
        ):
            try:
                candidates = tuple(getattr(self._backend, method_name)(context.locate_bgr))
            except Exception as exc:
                diagnostics.append(f"{source}: {exc}")
                failed_sources.add(source)
                continue
            typed_candidates = self._normalize_candidates(
                candidates, source, context.locate_bgr.shape[:2]
            )
            if source == "face":
                face_count = len(typed_candidates)
            subjects.extend(typed_candidates)

        ordered = tuple(sorted(subjects, key=self._rank, reverse=True))
        return SubjectLocationResult(
            subjects=ordered,
            primary_index=self._primary_index(ordered),
            face_count=face_count,
            diagnostics=tuple(diagnostics),
            failed_sources=frozenset(failed_sources),
        )

    @staticmethod
    def _normalize_candidates(
        candidates: tuple[Any, ...], source: str, image_shape: tuple[int, int]
    ) -> tuple[SubjectCandidate, ...]:
        normalized: list[SubjectCandidate] = []
        for candidate in candidates:
            if isinstance(candidate, SubjectCandidate):
                normalized.append(candidate)
            elif source == "pose":
                pose = SubjectLocator._pose_candidate(candidate)
                if pose is not None:
                    normalized.append(pose)
            else:
                detected = SubjectLocator._detection_candidate(candidate, source, image_shape)
                if detected is not None:
                    normalized.append(detected)
        return tuple(normalized)

    @staticmethod
    def _detection_candidate(
        detection: Any, source: str, image_shape: tuple[int, int]
    ) -> SubjectCandidate | None:
        box = getattr(detection, "bounding_box", None)
        if box is None:
            return None
        height, width = image_shape
        try:
            x = max(0.0, float(box.origin_x) / width)
            y = max(0.0, float(box.origin_y) / height)
            right = min(1.0, (float(box.origin_x) + float(box.width)) / width)
            bottom = min(1.0, (float(box.origin_y) + float(box.height)) / height)
            bounds = Rect(x, y, max(0.0, right - x), max(0.0, bottom - y))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            return None
        if bounds.width == 0.0 or bounds.height == 0.0:
            return None
        categories = tuple(getattr(detection, "categories", ()) or ())
        confidence = float(getattr(categories[0], "score", 0.0)) if categories else 0.0
        if not 0.0 <= confidence <= 1.0:
            return None
        kind = "face" if source == "face" else str(
            getattr(categories[0], "category_name", "object") if categories else "object"
        )
        return SubjectCandidate(kind, bounds, confidence, bounds.width * bounds.height)

    @staticmethod
    def _pose_candidate(landmarks: Any) -> SubjectCandidate | None:
        points: list[Keypoint] = []
        for landmark in landmarks:
            try:
                confidence = float(
                    getattr(landmark, "visibility", getattr(landmark, "presence", 1.0))
                )
                x = float(landmark.x)
                y = float(landmark.y)
            except (AttributeError, TypeError, ValueError):
                continue
            if not all(isfinite(value) for value in (x, y, confidence)):
                continue
            if not 0.0 <= confidence <= 1.0:
                continue
            points.append(Keypoint(min(1.0, max(0.0, x)), min(1.0, max(0.0, y)), confidence))
        if len(points) < 4:
            return None
        min_x, max_x = min(point.x for point in points), max(point.x for point in points)
        min_y, max_y = min(point.y for point in points), max(point.y for point in points)
        if min_x == max_x or min_y == max_y:
            return None
        confidence = sum(point.confidence for point in points) / len(points)
        bounds = Rect(min_x, min_y, max_x - min_x, max_y - min_y)
        return SubjectCandidate("person", bounds, confidence, bounds.width * bounds.height, tuple(points))

    @staticmethod
    def _rank(candidate: SubjectCandidate) -> float:
        face_bonus = 0.3 if candidate.kind == "face" else 0.0
        return candidate.confidence * 0.7 + candidate.salience * 0.3 + face_bonus

    def _primary_index(self, subjects: tuple[SubjectCandidate, ...]) -> int | None:
        if not subjects or self._rank(subjects[0]) < 0.55:
            return None
        competitors: list[SubjectCandidate] = []
        for subject in subjects:
            if any(
                subject.kind == competitor.kind
                and self._intersection_over_union(subject.bounds, competitor.bounds)
                >= _DUPLICATE_SUBJECT_IOU
                for competitor in competitors
            ):
                continue
            competitors.append(subject)
        if (
            len(competitors) > 1
            and self._rank(competitors[0]) - self._rank(competitors[1]) < 0.1
        ):
            return None
        return 0

    @staticmethod
    def _intersection_over_union(first: Rect, second: Rect) -> float:
        left = max(first.x, second.x)
        top = max(first.y, second.y)
        right = min(first.x + first.width, second.x + second.width)
        bottom = min(first.y + first.height, second.y + second.height)
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        union = (
            first.width * first.height
            + second.width * second.height
            - intersection
        )
        return intersection / union if union > 0.0 else 0.0
