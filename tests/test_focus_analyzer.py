"""Tests for subject-aware focus conclusions."""

from __future__ import annotations

import numpy as np

from src.xuanpian.domain.technical_analysis import (
    FocusConclusion,
    Rect,
    SubjectCandidate,
    Keypoint,
)
from src.xuanpian.engine.af_metadata import AFHint
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SubjectLocationResult


def _context(gray: np.ndarray) -> AnalysisContext:
    bgr = np.repeat(gray[:, :, None], 3, axis=2)
    return AnalysisContext(
        path="fixture.jpg",
        bgr=bgr,
        gray=gray,
        locate_bgr=bgr,
        locate_gray=gray,
        scale_x=1.0,
        scale_y=1.0,
    )


def _subjects() -> SubjectLocationResult:
    face = SubjectCandidate("face", Rect(0.25, 0.25, 0.5, 0.5), 0.95, 0.25)
    return SubjectLocationResult((face,), 0, 1)


def test_blurry_face_with_sharp_background_is_not_clear() -> None:
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    gray[50:150, 50:150] = 128

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is not FocusConclusion.CLEAR


def test_sharp_face_with_blurred_background_is_clear() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    gray[50:150, 50:150] = np.indices((100, 100)).sum(axis=0).astype(np.uint8) % 2 * 255

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is FocusConclusion.CLEAR


def test_low_texture_subject_requests_review() -> None:
    result = FocusAnalyzer().analyze(
        _context(np.full((200, 200), 128, np.uint8)), _subjects()
    )

    assert result.conclusion is FocusConclusion.REVIEW


def test_padded_object_box_cannot_borrow_sharp_background_edges():
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    gray[60:140, 60:140] = 128
    subject = SubjectCandidate("cat", Rect(0.25, 0.25, 0.5, 0.5), 0.95, 0.25)
    result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult((subject,), 0, 0)
    )
    assert result.conclusion is FocusConclusion.REVIEW


def test_ambiguous_faces_do_not_fall_back_to_a_sharp_background():
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    subjects = (
        _subjects().subjects[0],
        SubjectCandidate("face", Rect(0.05, 0.05, 0.2, 0.2), 0.95, 0.04),
    )
    result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult(subjects, None, 2)
    )
    assert result.conclusion is FocusConclusion.REVIEW


def test_face_landmarks_focus_on_eyes_and_nose_not_hair_edges():
    gray = np.indices((400, 400)).sum(axis=0).astype(np.uint8) % 2 * 255
    gray[150:240, 150:250] = 128
    points = tuple(
        Keypoint(x, y, 0.95)
        for x, y in ((0.42, 0.42), (0.58, 0.42), (0.5, 0.5), (0.45, 0.6), (0.55, 0.6))
    )
    face = SubjectCandidate("face", Rect(0.25, 0.25, 0.5, 0.5), 0.95, 0.25, points)
    result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult((face,), 0, 1)
    )
    assert result.conclusion is FocusConclusion.REVIEW


def test_medium_sharpness_confirmed_subject_is_clear() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    checker = (np.indices((100, 100)).sum(axis=0) // 16 % 2 * 80 + 88).astype(np.uint8)
    gray[50:150, 50:150] = checker

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is FocusConclusion.CLEAR


def test_low_laplacian_confirmed_subject_requests_review() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    y, x = np.mgrid[0:100, 0:100]
    # This region has strong gradient energy but a low Laplacian variance
    # (about 48): it must not be declared clear from a detected subject alone.
    subject = (50 * np.sin(2 * np.pi * x / 16) + 128).astype(np.uint8)
    gray[50:150, 50:150] = subject

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is FocusConclusion.REVIEW


def test_soft_confirmed_subject_is_out_of_focus() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    # Smooth linear ramp: enough texture (std >= 12) but near-zero gradients.
    face = (np.indices((100, 100)).sum(axis=0) * (100 / 198) + 80).astype(np.uint8)
    gray[50:150, 50:150] = face

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is FocusConclusion.OUT_OF_FOCUS


def test_smooth_region_with_noise_texture_is_review_not_out_of_focus() -> None:
    # Smooth shading plus sensor-like noise: textured (std >= 12) but with no
    # real edge gradient (tenengrad low). This is the "smooth but in-focus"
    # case that must stay 建议复查, never 明显失焦.
    rng = np.random.default_rng(11)
    ramp = np.indices((100, 100)).sum(axis=0) * (60 / 198) + 120
    face = np.clip(ramp + rng.normal(0, 3.0, (100, 100)), 0, 255).astype(np.uint8)
    gray = np.full((200, 200), 128, np.uint8)
    gray[50:150, 50:150] = face

    result = FocusAnalyzer().analyze(_context(gray), _subjects())

    assert result.conclusion is FocusConclusion.REVIEW


def test_low_confidence_subject_keeps_conservative_review() -> None:
    low_confidence = SubjectCandidate("face", Rect(0.25, 0.25, 0.5, 0.5), 0.3, 0.25)
    gray = np.full((200, 200), 128, np.uint8)
    # Medium-frequency wave: passes the metric gates (lap>=40, ten>=700)
    # but the low subject confidence must keep the conservative review.
    y, x = np.mgrid[0:100, 0:100]
    wave = (80 * np.sin(2 * np.pi * x / 24) * np.cos(2 * np.pi * y / 24) + 128).astype(
        np.uint8
    )
    gray[50:150, 50:150] = wave

    result = FocusAnalyzer().analyze(
        _context(gray),
        SubjectLocationResult((low_confidence,), 0, 1),
    )

    assert result.conclusion is FocusConclusion.REVIEW


def test_no_primary_and_face_object_failed_requires_review() -> None:
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    subjects = SubjectLocationResult(
        (), None, 0, failed_sources=frozenset({"face", "object"})
    )

    result = FocusAnalyzer().analyze(_context(gray), subjects)

    assert result.conclusion is FocusConclusion.REVIEW
    assert result.evidence[0][0] == "subject_capability_unavailable"


def test_object_primary_is_analyzed_when_face_detector_failed() -> None:
    gray = np.full((200, 200), 128, np.uint8)
    y, x = np.mgrid[0:100, 0:100]
    gray[50:150, 50:150] = (
        50 * np.sin(2 * np.pi * x / 16) * np.cos(2 * np.pi * y / 16) + 128
    ).astype(np.uint8)
    object_ = SubjectCandidate("object", Rect(0.25, 0.25, 0.5, 0.5), 0.9, 0.25)
    subjects = SubjectLocationResult(
        (object_,),
        0,
        0,
        failed_sources=frozenset({"face"}),
    )

    object_result = FocusAnalyzer().analyze(_context(gray), subjects)
    whole_frame_result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult((), None, 0)
    )

    assert object_result.conclusion is FocusConclusion.CLEAR
    assert whole_frame_result.conclusion is FocusConclusion.REVIEW


def test_available_af_region_never_falls_through_to_sharp_whole_frame() -> None:
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    gray[80:120, 80:120] = 128
    context = _context(gray)
    context = AnalysisContext(
        path=context.path,
        bgr=context.bgr,
        gray=context.gray,
        locate_bgr=context.locate_bgr,
        locate_gray=context.locate_gray,
        scale_x=context.scale_x,
        scale_y=context.scale_y,
        af_hint=AFHint(Rect(0.4, 0.4, 0.2, 0.2), "available"),
    )

    result = FocusAnalyzer().analyze(
        context,
        SubjectLocationResult((), None, 0),
    )

    assert result.conclusion is FocusConclusion.REVIEW
    assert dict(result.evidence)["af_status"] == 0.0


def test_af_box_on_same_person_is_not_rejected_by_a_tighter_face_box():
    from dataclasses import replace

    gray = ((np.indices((400, 400)).sum(axis=0) // 4) % 2 * 255).astype(np.uint8)
    face = SubjectCandidate("face", Rect(0.4, 0.15, 0.2, 0.2), 0.95, 0.04)
    person = SubjectCandidate("person", Rect(0.25, 0.1, 0.5, 0.8), 0.9, 0.4)
    context = replace(
        _context(gray), af_hint=AFHint(Rect(0.4, 0.4, 0.2, 0.2), "available")
    )
    result = FocusAnalyzer().analyze(
        context, SubjectLocationResult((face, person), 0, 1)
    )
    assert result.conclusion is FocusConclusion.CLEAR
    assert dict(result.evidence)["af_primary_envelope"] == 1


def test_unrelated_person_does_not_override_af_face_conflict():
    from dataclasses import replace

    gray = ((np.indices((400, 400)).sum(axis=0) // 4) % 2 * 255).astype(np.uint8)
    face = SubjectCandidate("face", Rect(0.7, 0.15, 0.2, 0.2), 0.95, 0.04)
    person = SubjectCandidate("person", Rect(0.1, 0.1, 0.4, 0.8), 0.9, 0.32)
    context = replace(
        _context(gray), af_hint=AFHint(Rect(0.2, 0.4, 0.2, 0.2), "available")
    )
    result = FocusAnalyzer().analyze(
        context, SubjectLocationResult((face, person), 0, 1)
    )
    assert result.conclusion is FocusConclusion.REVIEW


def test_tiny_distant_people_do_not_prevent_scene_analysis():
    gray = ((np.indices((200, 200)).sum(axis=0) // 4) % 2 * 255).astype(np.uint8)
    distant = SubjectCandidate("person", Rect(0.1, 0.8, 0.03, 0.1), 0.6, 0.003)
    result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult((distant,), None, 0)
    )
    assert result.conclusion is FocusConclusion.CLEAR


def test_landmarks_outside_face_fall_back_to_central_face_region():
    gray = np.full((200, 200), 128, np.uint8)
    points = tuple(Keypoint(x, y, 0.95) for x, y in ((0.8,0.8),(0.9,0.85),(0.85,0.9)))
    face = SubjectCandidate("face", Rect(0.25, 0.25, 0.2, 0.2), 0.95, 0.04, points)
    result = FocusAnalyzer().analyze(
        _context(gray), SubjectLocationResult((face,), 0, 1)
    )
    assert result.conclusion is FocusConclusion.REVIEW


def test_unavailable_af_reason_is_appended_without_reordering_legacy_evidence() -> None:
    gray = np.indices((200, 200)).sum(axis=0).astype(np.uint8) % 2 * 255
    subjects = SubjectLocationResult(
        (), None, 0, failed_sources=frozenset({"face", "object"})
    )

    result = FocusAnalyzer().analyze(_context(gray), subjects)

    assert result.evidence[0] == ("subject_capability_unavailable", 1.0)
    assert result.evidence[-1] == ("af_unavailable_unavailable", 1.0)
