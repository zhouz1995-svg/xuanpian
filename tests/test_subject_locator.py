"""Tests for conservative mixed-subject location."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.xuanpian.domain.technical_analysis import (
    FocusConclusion,
    Rect,
    SubjectCandidate,
)
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SubjectLocationResult, SubjectLocator


def _candidate(kind: str, confidence: float, area: float) -> SubjectCandidate:
    side = area**0.5
    return SubjectCandidate(
        kind=kind,
        bounds=Rect(0.1, 0.1, side, side),
        confidence=confidence,
        salience=area,
    )


def _bounded_candidate(
    kind: str,
    confidence: float,
    bounds: Rect,
) -> SubjectCandidate:
    return SubjectCandidate(
        kind=kind,
        bounds=bounds,
        confidence=confidence,
        salience=bounds.width * bounds.height,
    )


def _context() -> AnalysisContext:
    image = np.zeros((100, 100, 3), np.uint8)
    return AnalysisContext(
        path="fixture.jpg",
        bgr=image,
        gray=np.zeros((100, 100), np.uint8),
        locate_bgr=image,
        locate_gray=np.zeros((100, 100), np.uint8),
        scale_x=1.0,
        scale_y=1.0,
    )


def _context_with_gray(gray: np.ndarray) -> AnalysisContext:
    bgr = np.repeat(gray[:, :, np.newaxis], 3, axis=2)
    return AnalysisContext(
        path="fixture.jpg",
        bgr=bgr,
        gray=gray,
        locate_bgr=bgr,
        locate_gray=gray,
        scale_x=1.0,
        scale_y=1.0,
    )


class _Backend:
    def __init__(
        self,
        *,
        faces: tuple[SubjectCandidate, ...] = (),
        face_error: Exception | None = None,
    ) -> None:
        self._faces = faces
        self._face_error = face_error

    def detect_faces(self, _image: np.ndarray) -> tuple[SubjectCandidate, ...]:
        if self._face_error is not None:
            raise self._face_error
        return self._faces

    def detect_poses(self, _image: np.ndarray) -> tuple[()]:
        return ()

    def detect_objects(self, _image: np.ndarray) -> tuple[()]:
        return ()


def test_face_is_primary_and_counted() -> None:
    backend = SimpleNamespace(
        detect_faces=lambda image: (_candidate("face", 0.9, 0.2),),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (_candidate("cat", 0.99, 0.5),),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.face_count == 1
    assert result.primary is not None
    assert result.primary.kind == "face"


def test_conflicting_candidates_have_no_primary() -> None:
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (
            _candidate("cat", 0.8, 0.2),
            _candidate("dog", 0.8, 0.2),
        ),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects[0].kind == "cat"
    assert result.primary is None


def test_same_kind_person_at_duplicate_iou_boundary_does_not_block_primary() -> None:
    primary = _bounded_candidate("person", 0.9, Rect(0.05, 0.1, 0.66, 0.5))
    duplicate = _bounded_candidate("person", 0.82, Rect(0.19, 0.1, 0.66, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (duplicate, primary),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects == (primary, duplicate)
    assert result.primary == primary
    assert result.face_count == 0


def test_duplicate_faces_keep_original_face_count_and_subject_list() -> None:
    primary = _bounded_candidate("face", 0.9, Rect(0.05, 0.1, 0.66, 0.5))
    duplicate = _bounded_candidate("face", 0.82, Rect(0.19, 0.1, 0.66, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (duplicate, primary),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects == (primary, duplicate)
    assert result.face_count == 2
    assert result.primary == primary


def test_partial_overlap_same_kind_people_remain_ambiguous() -> None:
    primary = _bounded_candidate("person", 0.9, Rect(0.05, 0.1, 0.66, 0.5))
    partly_overlapping = _bounded_candidate(
        "person", 0.82, Rect(0.191, 0.1, 0.66, 0.5)
    )
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (primary, partly_overlapping),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.primary is None


def test_separated_same_kind_people_remain_ambiguous() -> None:
    primary = _bounded_candidate("person", 0.9, Rect(0.05, 0.1, 0.35, 0.5))
    separated = _bounded_candidate("person", 0.88, Rect(0.6, 0.1, 0.35, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (primary, separated),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.primary is None


def test_overlapping_different_kind_candidates_remain_ambiguous() -> None:
    person = _bounded_candidate("person", 0.9, Rect(0.1, 0.1, 0.6, 0.6))
    bird = _bounded_candidate("bird", 0.85, Rect(0.1, 0.1, 0.6, 0.6))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (person, bird),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.primary is None


def test_overlapping_face_and_person_candidates_remain_ambiguous() -> None:
    face = _bounded_candidate("face", 0.5, Rect(0.1, 0.1, 0.6, 0.6))
    person = _bounded_candidate("person", 0.93, Rect(0.1, 0.1, 0.6, 0.6))
    backend = SimpleNamespace(
        detect_faces=lambda image: (face,),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (person,),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.primary is None


def test_real_competing_person_still_blocks_primary_after_duplicate_suppression() -> None:
    primary = _bounded_candidate("person", 0.9, Rect(0.05, 0.1, 0.5, 0.5))
    duplicate = _bounded_candidate("person", 0.82, Rect(0.1, 0.1, 0.5, 0.5))
    competitor = _bounded_candidate("person", 0.88, Rect(0.6, 0.1, 0.35, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (duplicate, competitor, primary),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects == (primary, competitor, duplicate)
    assert result.primary is None


def test_duplicate_person_focuses_sharp_subject_instead_of_soft_background() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    gray[50:150, 50:150] = (
        np.indices((100, 100)).sum(axis=0).astype(np.uint8) % 2 * 255
    )
    primary = _bounded_candidate("person", 0.9, Rect(0.25, 0.25, 0.5, 0.5))
    duplicate = _bounded_candidate("person", 0.82, Rect(0.3, 0.25, 0.5, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (primary, duplicate),
    )
    context = _context_with_gray(gray)

    subjects = SubjectLocator(backend).locate(context)
    result = FocusAnalyzer().analyze(context, subjects)

    assert subjects.primary == primary
    assert result.conclusion is FocusConclusion.CLEAR


def test_duplicate_person_with_soft_subject_and_sharp_background_is_not_clear() -> None:
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    gray[50:150, 50:150] = 128
    primary = _bounded_candidate("person", 0.9, Rect(0.25, 0.25, 0.5, 0.5))
    duplicate = _bounded_candidate("person", 0.82, Rect(0.3, 0.25, 0.5, 0.5))
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (primary, duplicate),
    )
    context = _context_with_gray(gray)

    subjects = SubjectLocator(backend).locate(context)
    result = FocusAnalyzer().analyze(context, subjects)

    assert subjects.primary == primary
    assert result.conclusion is not FocusConclusion.CLEAR


def test_failed_detector_is_recorded_without_inventing_a_subject() -> None:
    backend = SimpleNamespace(
        detect_faces=lambda image: (_ for _ in ()).throw(RuntimeError("bad model")),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects == ()
    assert result.primary is None
    assert result.diagnostics == ("face: bad model",)


def test_face_failure_is_distinct_from_successful_zero_faces() -> None:
    context = _context()

    failed = SubjectLocator(_Backend(face_error=RuntimeError("offline"))).locate(context)
    empty = SubjectLocator(_Backend(faces=())).locate(context)

    assert failed.failed_sources == frozenset({"face"})
    assert empty.failed_sources == frozenset()
    assert failed.face_count == empty.face_count == 0


def test_failed_sources_reject_unknown_names() -> None:
    with pytest.raises(ValueError, match="failed source"):
        SubjectLocationResult((), None, 0, failed_sources=frozenset({"gpu"}))


def test_failed_sources_are_exposed_as_an_immutable_frozenset() -> None:
    result = SubjectLocationResult((), None, 0, failed_sources={"face"})

    assert result.failed_sources == frozenset({"face"})
    assert isinstance(result.failed_sources, frozenset)


@pytest.mark.parametrize(
    "failed_sources",
    ("", b"", {"face": "failed"}, ["face"], ("face",)),
    ids=("str", "bytes", "mapping", "list", "tuple"),
)
def test_failed_sources_reject_non_set_like_containers(failed_sources: object) -> None:
    with pytest.raises(ValueError, match="failed source"):
        SubjectLocationResult((), None, 0, failed_sources=failed_sources)


def test_face_failure_keeps_successful_object_subject() -> None:
    object_candidate = _candidate("cat", 0.9, 0.4)
    backend = SimpleNamespace(
        detect_faces=lambda _image: (_ for _ in ()).throw(RuntimeError("offline")),
        detect_poses=lambda _image: (),
        detect_objects=lambda _image: (object_candidate,),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.subjects == (object_candidate,)
    assert result.primary == object_candidate
    assert result.face_count == 0
    assert result.diagnostics == ("face: offline",)
    assert result.failed_sources == frozenset({"face"})


def test_tasks_face_detection_is_normalized_to_a_subject_candidate() -> None:
    face_detection = SimpleNamespace(
        bounding_box=SimpleNamespace(origin_x=10, origin_y=20, width=30, height=40),
        categories=(SimpleNamespace(score=0.91),),
    )
    backend = SimpleNamespace(
        detect_faces=lambda image: (face_detection,),
        detect_poses=lambda image: (),
        detect_objects=lambda image: (),
    )

    result = SubjectLocator(backend).locate(_context())

    candidate = result.subjects[0]
    assert candidate.kind == "face"
    assert candidate.bounds.x == 0.1
    assert candidate.bounds.y == 0.2
    assert candidate.bounds.width == pytest.approx(0.3)
    assert candidate.bounds.height == pytest.approx(0.4)
    assert candidate.confidence == 0.91
    assert candidate.salience == pytest.approx(0.12)


def test_pose_landmarks_outside_frame_are_clamped_and_invalid_points_ignored() -> None:
    landmarks = (
        SimpleNamespace(x=-.02, y=.2, visibility=.9),
        SimpleNamespace(x=.8, y=1.02, visibility=.9),
        SimpleNamespace(x=.2, y=.4, visibility=.9),
        SimpleNamespace(x=.6, y=.7, visibility=.9),
        SimpleNamespace(x=float("nan"), y=.5, visibility=.9),
    )
    backend = SimpleNamespace(
        detect_faces=lambda image: (),
        detect_poses=lambda image: (landmarks,),
        detect_objects=lambda image: (),
    )

    result = SubjectLocator(backend).locate(_context())

    assert result.primary is not None
    assert result.primary.kind == "person"
    assert len(result.primary.keypoints) == 4
    assert all(0.0 <= point.x <= 1.0 for point in result.primary.keypoints)
    assert all(0.0 <= point.y <= 1.0 for point in result.primary.keypoints)
