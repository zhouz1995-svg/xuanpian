"""Behavioral tests for conservative AF-region focus scoring."""

from __future__ import annotations

from PIL import Image, ImageFilter
import numpy as np

from src.xuanpian.domain.technical_analysis import (
    FocusConclusion,
    Rect,
    SubjectCandidate,
)
from src.xuanpian.engine.af_focus import analyze_af_focus
from src.xuanpian.engine.af_metadata import AFHint


_CENTER_AF = Rect(0.375, 0.375, 0.25, 0.25)


def _checker(height: int, width: int, *, cell: int = 4, contrast: int = 255) -> np.ndarray:
    y, x = np.indices((height, width))
    low = (255 - contrast) // 2
    return ((x // cell + y // cell) % 2 * contrast + low).astype(np.uint8)


def _centered_frame(center: np.ndarray, *, size: int = 240, value: int = 128) -> np.ndarray:
    gray = np.full((size, size), value, np.uint8)
    side = center.shape[0]
    start = (size - side) // 2
    gray[start : start + side, start : start + side] = center
    return gray


def test_sharp_af_region_with_soft_surroundings_is_clear() -> None:
    gray = _centered_frame(_checker(60, 60))

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.CLEAR
    evidence = dict(result.evidence)
    assert evidence["af_status"] == 1.0
    assert evidence["af_x"] == 0.375
    assert evidence["af_y"] == 0.375
    assert evidence["af_width"] == 0.25
    assert evidence["af_height"] == 0.25
    assert evidence["af_1x_texture"] >= 12.0
    assert evidence["af_1x_laplacian"] >= 80.0
    assert evidence["af_1x_tenengrad"] >= 1_000.0
    assert evidence["af_2x_texture"] >= 12.0
    assert evidence["af_2x_laplacian"] >= 80.0
    assert evidence["af_2x_tenengrad"] >= 1_000.0


def test_blurred_af_region_with_sharp_surroundings_requests_review() -> None:
    gray = _checker(240, 240)
    gray[90:150, 90:150] = 128

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_status"] == 0.0
    assert evidence["af_1x_laplacian"] < 80.0
    assert evidence["af_2x_laplacian"] >= 80.0


def test_textured_gaussian_blurred_af_with_sharp_surroundings_requests_review() -> None:
    gray = _checker(240, 240, cell=8)
    blurred_af = np.array(Image.fromarray(_checker(60, 60, cell=8)).filter(ImageFilter.GaussianBlur(3.0)))
    gray[90:150, 90:150] = blurred_af

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_1x_texture"] > 12.0
    assert evidence["af_1x_laplacian"] < 80.0
    assert evidence["af_1x_tenengrad"] > 1_000.0
    assert evidence["af_2x_laplacian"] > 80.0
    assert evidence["af_2x_tenengrad"] > 1_000.0


def test_low_texture_af_region_requests_review() -> None:
    _y, x = np.mgrid[0:240, 0:240]
    low_texture_ramp = (118 + x * (20 / 239)).astype(np.uint8)
    result = analyze_af_focus(
        low_texture_ramp,
        AFHint(_CENTER_AF, "available"),
        None,
    )

    assert result.conclusion is FocusConclusion.REVIEW
    assert dict(result.evidence)["af_review_low_texture"] == 1.0


def test_bright_image_and_roi_edges_do_not_create_false_sharpness() -> None:
    gray = np.full((240, 240), 128, np.uint8)
    gray[:, 0] = 255
    gray[72, :60] = 255
    gray[131, :60] = 255
    gray[72:132, 59] = 255
    edge_hint = AFHint(Rect(0.0, 0.3, 0.25, 0.25), "available")

    result = analyze_af_focus(gray, edge_hint, None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_1x_laplacian"] < 80.0
    assert evidence["af_1x_tenengrad"] < 1_000.0


def test_af_region_conflicting_with_confident_primary_requests_review() -> None:
    gray = _checker(240, 240)
    primary = SubjectCandidate(
        "face",
        Rect(0.65, 0.25, 0.3, 0.5),
        0.95,
        0.15,
    )

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), primary)

    assert result.conclusion is FocusConclusion.REVIEW
    assert dict(result.evidence)["af_review_subject_conflict"] == 1.0


def test_af_region_mostly_outside_confident_primary_requests_review() -> None:
    gray = _checker(240, 240)
    # The AF center touches the subject edge, but only half the AF area overlaps.
    # Treating mere center contact as compatibility would upgrade unrelated detail.
    primary = SubjectCandidate("face", Rect(0.5, 0.25, 0.4, 0.5), 0.95, 0.2)

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), primary)

    assert result.conclusion is FocusConclusion.REVIEW
    assert dict(result.evidence)["af_review_subject_conflict"] == 1.0


def test_too_small_af_region_requests_review() -> None:
    hint = AFHint(Rect(0.45, 0.45, 0.1, 0.1), "available")

    result = analyze_af_focus(_checker(120, 120), hint, None)

    assert result.conclusion is FocusConclusion.REVIEW
    assert dict(result.evidence)["af_review_region_too_small"] == 1.0


def test_two_scale_disagreement_requests_review() -> None:
    # The 1x box has literal 30-level checker contrast: enough local texture and
    # edge energy to pass alone, but the flat 2x neighborhood dilutes that evidence.
    gray = _centered_frame(_checker(60, 60, cell=4, contrast=30))

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_1x_texture"] >= 12.0
    assert evidence["af_1x_laplacian"] >= 80.0
    assert evidence["af_1x_tenengrad"] >= 1_000.0
    assert (
        evidence["af_2x_texture"] < 12.0
        or evidence["af_2x_laplacian"] < 80.0
        or evidence["af_2x_tenengrad"] < 1_000.0
    )
    assert evidence["af_review_scale_disagreement"] == 1.0


def test_strong_gradient_without_laplacian_evidence_requests_review() -> None:
    _y, x = np.mgrid[0:240, 0:240]
    gray = (50 * np.sin(2 * np.pi * x / 16) + 128).astype(np.uint8)

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_1x_laplacian"] < 80.0
    assert evidence["af_1x_tenengrad"] >= 1_000.0
    assert evidence["af_2x_laplacian"] < 80.0
    assert evidence["af_2x_tenengrad"] >= 1_000.0


def test_laplacian_without_gradient_evidence_requests_review() -> None:
    gray = _checker(240, 240, cell=1)

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), None)

    assert result.conclusion is FocusConclusion.REVIEW
    evidence = dict(result.evidence)
    assert evidence["af_1x_laplacian"] >= 80.0
    assert evidence["af_1x_tenengrad"] < 1_000.0
    assert evidence["af_2x_laplacian"] >= 80.0
    assert evidence["af_2x_tenengrad"] < 1_000.0


def test_camera_focus_flag_is_not_an_input_to_pixel_classification() -> None:
    gray = _centered_frame(_checker(60, 60))

    reported_focused = analyze_af_focus(
        gray,
        AFHint(_CENTER_AF, "available"),
        None,
    )
    reported_not_focused = analyze_af_focus(
        gray,
        AFHint(_CENTER_AF, "camera_reported_not_focused"),
        None,
    )

    assert reported_focused.conclusion is FocusConclusion.CLEAR
    assert reported_not_focused.conclusion is FocusConclusion.CLEAR
    assert reported_not_focused.evidence == reported_focused.evidence


def test_context_is_clipped_to_a_compatible_confident_primary() -> None:
    gray = _checker(240, 240)
    primary = SubjectCandidate("face", _CENTER_AF, 0.95, 0.0625)

    result = analyze_af_focus(gray, AFHint(_CENTER_AF, "available"), primary)

    assert result.conclusion is FocusConclusion.CLEAR
    evidence = dict(result.evidence)
    assert evidence["af_context_clipped_to_primary"] == 1.0
    assert evidence["af_2x_width"] == 0.25
    assert evidence["af_2x_height"] == 0.25
