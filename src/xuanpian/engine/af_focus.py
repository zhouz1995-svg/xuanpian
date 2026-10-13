"""Conservative focus analysis for a validated autofocus region."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from src.xuanpian.domain.technical_analysis import (
    FocusConclusion,
    FocusResult,
    Rect,
    SubjectCandidate,
)
from src.xuanpian.engine.af_metadata import AFHint


_MIN_INTERIOR_SIDE = 24
_INTERIOR_MARGIN = 2
_PRIMARY_CONFIDENCE_GATE = 0.5
_MIN_AF_PRIMARY_OVERLAP = 0.5
_TEXTURE_GATE = 12.0
_LAPLACIAN_GATE = 80.0
_TENENGRAD_GATE = 1_000.0


@dataclass(frozen=True, slots=True)
class _PixelRect:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    def interior(self) -> "_PixelRect":
        return _PixelRect(
            self.x0 + _INTERIOR_MARGIN,
            self.y0 + _INTERIOR_MARGIN,
            self.x1 - _INTERIOR_MARGIN,
            self.y1 - _INTERIOR_MARGIN,
        )


@dataclass(frozen=True, slots=True)
class _ScaleMetrics:
    texture: float
    laplacian: float
    tenengrad: float

    @property
    def passes(self) -> bool:
        return (
            self.texture >= _TEXTURE_GATE
            and self.laplacian >= _LAPLACIAN_GATE
            and self.tenengrad >= _TENENGRAD_GATE
        )


def _expand(rect: Rect) -> Rect:
    center_x = rect.x + rect.width / 2.0
    center_y = rect.y + rect.height / 2.0
    left = max(0.0, center_x - rect.width)
    top = max(0.0, center_y - rect.height)
    right = min(1.0, center_x + rect.width)
    bottom = min(1.0, center_y + rect.height)
    return Rect(left, top, right - left, bottom - top)


def _contains_center(container: Rect, region: Rect) -> bool:
    center_x = region.x + region.width / 2.0
    center_y = region.y + region.height / 2.0
    return (
        container.x <= center_x <= container.x + container.width
        and container.y <= center_y <= container.y + container.height
    )


def _intersection(first: Rect, second: Rect) -> Rect | None:
    left = max(first.x, second.x)
    top = max(first.y, second.y)
    right = min(first.x + first.width, second.x + second.width)
    bottom = min(first.y + first.height, second.y + second.height)
    if right <= left or bottom <= top:
        return None
    return Rect(left, top, right - left, bottom - top)


def _coverage(region: Rect, container: Rect) -> float:
    overlap = _intersection(region, container)
    if overlap is None:
        return 0.0
    return overlap.width * overlap.height / (region.width * region.height)


def _to_pixels(rect: Rect, shape: tuple[int, ...]) -> _PixelRect:
    height, width = shape[:2]
    return _PixelRect(
        max(0, round(rect.x * width)),
        max(0, round(rect.y * height)),
        min(width, round((rect.x + rect.width) * width)),
        min(height, round((rect.y + rect.height) * height)),
    )


def _geometry_evidence(region: Rect, context: Rect) -> tuple[tuple[str, float], ...]:
    return (
        ("af_status", 0.0),
        ("af_used", 1.0),
        ("af_x", region.x),
        ("af_y", region.y),
        ("af_width", region.width),
        ("af_height", region.height),
        ("af_2x_x", context.x),
        ("af_2x_y", context.y),
        ("af_2x_width", context.width),
        ("af_2x_height", context.height),
    )


def _mean_square(values: np.ndarray) -> float:
    return float(cv2.norm(values, cv2.NORM_L2SQR) / values.size)


def _texture(gray: np.ndarray, region: _PixelRect) -> float:
    roi = gray[region.y0 : region.y1, region.x0 : region.x1]
    _mean, deviation = cv2.meanStdDev(roi)
    return float(deviation[0, 0])


def _derivative_metrics(
    gray: np.ndarray,
    regions: tuple[_PixelRect, _PixelRect],
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (Laplacian variance, Tenengrad) without retaining float frames."""
    laplacian = cv2.Laplacian(gray, cv2.CV_16S, ksize=1)
    laplacian_metrics: list[float] = []
    for region in regions:
        roi = laplacian[region.y0 : region.y1, region.x0 : region.x1]
        mean = float(cv2.mean(roi)[0])
        laplacian_metrics.append(max(0.0, _mean_square(roi) - mean * mean))
    del laplacian

    tenengrad = [0.0, 0.0]
    for dx, dy in ((1, 0), (0, 1)):
        derivative = cv2.Sobel(gray, cv2.CV_16S, dx, dy, ksize=3)
        for index, region in enumerate(regions):
            roi = derivative[region.y0 : region.y1, region.x0 : region.x1]
            tenengrad[index] += _mean_square(roi)
        del derivative
    return (
        (laplacian_metrics[0], tenengrad[0]),
        (laplacian_metrics[1], tenengrad[1]),
    )


def _metric_evidence(
    one: _ScaleMetrics,
    two: _ScaleMetrics,
) -> tuple[tuple[str, float], ...]:
    return (
        ("af_1x_texture", one.texture),
        ("af_1x_laplacian", one.laplacian),
        ("af_1x_tenengrad", one.tenengrad),
        ("af_1x_pass", float(one.passes)),
        ("af_2x_texture", two.texture),
        ("af_2x_laplacian", two.laplacian),
        ("af_2x_tenengrad", two.tenengrad),
        ("af_2x_pass", float(two.passes)),
    )


def _with_status(
    evidence: tuple[tuple[str, float], ...], status: float
) -> tuple[tuple[str, float], ...]:
    return (("af_status", status),) + evidence[1:]


def analyze_af_focus(
    gray: np.ndarray,
    hint: AFHint,
    primary: SubjectCandidate | None,
) -> FocusResult:
    """Analyze a validated AF box and its fixed two-times context."""
    region = hint.bounds
    if region is None or gray.ndim != 2 or gray.size == 0:
        return FocusResult(
            FocusConclusion.REVIEW,
            evidence=(("af_status", 0.0), ("af_review_invalid_input", 1.0)),
        )

    context = _expand(region)
    evidence = _geometry_evidence(region, context)
    confident_primary = primary is not None and primary.confidence >= _PRIMARY_CONFIDENCE_GATE
    if confident_primary:
        assert primary is not None
        if (
            not _contains_center(primary.bounds, region)
            or _coverage(region, primary.bounds) <= _MIN_AF_PRIMARY_OVERLAP
        ):
            return FocusResult(
                FocusConclusion.REVIEW,
                evidence=evidence + (("af_review_subject_conflict", 1.0),),
            )
        clipped = _intersection(context, primary.bounds)
        if clipped is None:
            return FocusResult(
                FocusConclusion.REVIEW,
                evidence=evidence + (("af_review_subject_conflict", 1.0),),
            )
        context = clipped
        evidence = _geometry_evidence(region, context) + (
            ("af_context_clipped_to_primary", 1.0),
        )

    one_pixels = _to_pixels(region, gray.shape).interior()
    two_pixels = _to_pixels(context, gray.shape).interior()
    if (
        min(one_pixels.width, one_pixels.height) < _MIN_INTERIOR_SIDE
        or min(two_pixels.width, two_pixels.height) < _MIN_INTERIOR_SIDE
    ):
        return FocusResult(
            FocusConclusion.REVIEW,
            evidence=evidence + (("af_review_region_too_small", 1.0),),
        )

    one_texture = _texture(gray, one_pixels)
    two_texture = _texture(gray, two_pixels)
    derivatives = _derivative_metrics(gray, (one_pixels, two_pixels))
    one = _ScaleMetrics(one_texture, derivatives[0][0], derivatives[0][1])
    two = _ScaleMetrics(two_texture, derivatives[1][0], derivatives[1][1])
    evidence += _metric_evidence(one, two)

    if one.passes and two.passes:
        return FocusResult(
            FocusConclusion.CLEAR,
            0.75,
            _with_status(evidence, 1.0),
        )
    if one.passes != two.passes:
        reason = "af_review_scale_disagreement"
    elif one.texture < _TEXTURE_GATE or two.texture < _TEXTURE_GATE:
        reason = "af_review_low_texture"
    else:
        reason = "af_review_insufficient_sharpness"
    return FocusResult(
        FocusConclusion.REVIEW,
        evidence=evidence + ((reason, 1.0),),
    )
