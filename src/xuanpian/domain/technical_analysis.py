from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite


TECHNICAL_ALGORITHM_VERSION = "technical-v7"
_READABLE_TECHNICAL_ALGORITHM_VERSIONS = frozenset(
    {
        "technical-v3",
        "technical-v4",
        "technical-v5",
        "technical-v6",
        TECHNICAL_ALGORITHM_VERSION,
    }
)
_LEGACY_FRAMING_CONCLUSIONS = frozenset({"主体完整", "主体可能被裁切", "不判断"})


class FocusConclusion(str, Enum):
    CLEAR = "清晰"
    REVIEW = "建议复查"
    OUT_OF_FOCUS = "明显失焦"


class ExposureConclusion(str, Enum):
    NORMAL = "正常"
    OVEREXPOSED = "过曝"
    UNDEREXPOSED = "欠曝"
    BOTH = "曝光异常"


def _finite(value: object, name: str, *, minimum: float = 0.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not isfinite(result) or result < minimum:
        raise ValueError(f"{name} must be finite and at least {minimum}")
    return result


@dataclass(frozen=True, slots=True)
class Rect:
    """A normalized image rectangle whose bounds remain inside [0, 1]."""

    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.x > 1.0 or self.y > 1.0 or self.width > 1.0 or self.height > 1.0:
            raise ValueError("rect values must be normalized to [0, 1]")
        if self.x + self.width > 1.0 or self.y + self.height > 1.0:
            raise ValueError("rect must remain inside normalized image bounds")

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Rect":
        return cls(data["x"], data["y"], data["width"], data["height"])


@dataclass(frozen=True, slots=True)
class Keypoint:
    x: float
    y: float
    confidence: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "x", _finite(self.x, "x"))
        object.__setattr__(self, "y", _finite(self.y, "y"))
        object.__setattr__(self, "confidence", _finite(self.confidence, "confidence"))
        if self.x > 1.0 or self.y > 1.0 or self.confidence > 1.0:
            raise ValueError("keypoint values must be normalized to [0, 1]")


@dataclass(frozen=True, slots=True)
class SubjectCandidate:
    kind: str
    bounds: Rect
    confidence: float
    salience: float
    keypoints: tuple[Keypoint, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("subject kind must be non-empty")
        object.__setattr__(self, "confidence", _finite(self.confidence, "confidence"))
        object.__setattr__(self, "salience", _finite(self.salience, "salience"))
        if self.confidence > 1.0 or self.salience > 1.0:
            raise ValueError("subject confidence and salience must be normalized")


@dataclass(frozen=True, slots=True)
class FocusResult:
    conclusion: FocusConclusion
    confidence: float = 0.0
    evidence: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", _finite(self.confidence, "confidence"))
        if self.confidence > 1.0:
            raise ValueError("focus confidence must be normalized")


@dataclass(frozen=True, slots=True)
class ExposureResultV2:
    conclusion: ExposureConclusion
    overexposed_pct: float
    underexposed_pct: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "overexposed_pct", _finite(self.overexposed_pct, "overexposed_pct"))
        object.__setattr__(self, "underexposed_pct", _finite(self.underexposed_pct, "underexposed_pct"))
        if self.overexposed_pct > 100.0 or self.underexposed_pct > 100.0:
            raise ValueError("exposure percentages must be within [0, 100]")


@dataclass(frozen=True, slots=True)
class TechnicalAnalysisResult:
    algorithm_version: str
    focus: FocusResult
    exposure: ExposureResultV2
    face_count: int
    portrait_assessed: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.algorithm_version, str)
            or self.algorithm_version
            not in _READABLE_TECHNICAL_ALGORITHM_VERSIONS
        ):
            raise ValueError("unsupported technical algorithm version")
        if not isinstance(self.face_count, int) or isinstance(self.face_count, bool) or self.face_count < 0:
            raise ValueError("face_count must be a non-negative integer")
        if not isinstance(self.portrait_assessed, bool):
            raise ValueError("portrait_assessed must be a boolean")

    @classmethod
    def review_only(cls) -> "TechnicalAnalysisResult":
        return cls(
            algorithm_version=TECHNICAL_ALGORITHM_VERSION,
            focus=FocusResult(FocusConclusion.REVIEW),
            exposure=ExposureResultV2(ExposureConclusion.NORMAL, 0.0, 0.0),
            face_count=0,
            portrait_assessed=False,
        )

    def to_dict(self) -> dict[str, object]:
        if self.algorithm_version != TECHNICAL_ALGORITHM_VERSION:
            raise ValueError("only current technical results can be serialized")
        return {
            "algorithm_version": self.algorithm_version,
            "focus": _focus_to_dict(self.focus),
            "exposure": _exposure_to_dict(self.exposure),
            "face_count": self.face_count,
            "portrait_assessed": self.portrait_assessed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TechnicalAnalysisResult":
        if not isinstance(data, dict):
            raise ValueError("technical result must be a mapping")
        algorithm_version = data.get("algorithm_version")
        if (
            not isinstance(algorithm_version, str)
            or algorithm_version
            not in _READABLE_TECHNICAL_ALGORITHM_VERSIONS
        ):
            raise ValueError("unsupported technical algorithm version")
        focus = _mapping(data, "focus")
        exposure = _mapping(data, "exposure")
        if algorithm_version in {"technical-v3", "technical-v4"}:
            _validate_legacy_framing(_mapping(data, "framing"))
            portrait_assessed = False
        else:
            if "framing" in data:
                raise ValueError("current-format technical result must not contain framing")
            portrait_assessed = data.get("portrait_assessed")
            if not isinstance(portrait_assessed, bool):
                raise ValueError("portrait_assessed must be a boolean")
        evidence = focus.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("focus evidence must be a list")
        evidence_items: list[tuple[str, float]] = []
        for item in evidence:
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                raise ValueError("focus evidence item is invalid")
            evidence_items.append((item[0], _finite(item[1], "evidence value")))
        return cls(
            algorithm_version=algorithm_version,
            focus=FocusResult(
                FocusConclusion(_required_value(focus, "conclusion")),
                _finite(focus.get("confidence", 0.0), "focus confidence"),
                tuple(evidence_items),
            ),
            exposure=ExposureResultV2(
                ExposureConclusion(_required_value(exposure, "conclusion")),
                _finite(
                    _required_value(exposure, "overexposed_pct"),
                    "overexposed_pct",
                ),
                _finite(
                    _required_value(exposure, "underexposed_pct"),
                    "underexposed_pct",
                ),
            ),
            face_count=_required_value(data, "face_count"),
            portrait_assessed=portrait_assessed,
        )


def _focus_to_dict(focus: FocusResult) -> dict[str, object]:
    return {
        "conclusion": focus.conclusion.value,
        "confidence": focus.confidence,
        "evidence": [[name, value] for name, value in focus.evidence],
    }


def _exposure_to_dict(exposure: ExposureResultV2) -> dict[str, object]:
    return {
        "conclusion": exposure.conclusion.value,
        "overexposed_pct": exposure.overexposed_pct,
        "underexposed_pct": exposure.underexposed_pct,
    }


def _validate_legacy_framing(framing: dict[str, object]) -> None:
    conclusion = framing.get("conclusion")
    if not isinstance(conclusion, str) or conclusion not in _LEGACY_FRAMING_CONCLUSIONS:
        raise ValueError("unsupported legacy framing conclusion")
    confidence = _finite(framing.get("confidence", 0.0), "framing confidence")
    if confidence > 1.0:
        raise ValueError("framing confidence must be normalized")
    reason = framing.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("framing reason must be a string")


def _mapping(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a mapping")
    return value


def _required_value(data: dict[str, object], key: str) -> object:
    try:
        return data[key]
    except KeyError as error:
        raise ValueError(f"{key} is required") from error
