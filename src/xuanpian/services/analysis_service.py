from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, cast

from src.xuanpian.domain.analysis import AnalysisFailure, AnalysisStatus
from src.xuanpian.engine.exposure_analyzer import ExposureAnalyzer
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SubjectLocator
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer
from src.xuanpian.engine.vision_backend import OnnxSubjectBackend


class Analyzer(Protocol):
    def analyze(self, path: str) -> object: ...


@dataclass(frozen=True, slots=True)
class PhotoAnalysisRequest:
    photo_id: str
    path: str
    analyzers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalyzerOutcome:
    name: str
    status: AnalysisStatus
    result: object | None = None
    failure: AnalysisFailure | None = None


@dataclass(frozen=True, slots=True)
class PhotoAnalysisResponse:
    photo_id: str
    outcomes: tuple[AnalyzerOutcome, ...]


_ANALYZER_FACTORIES: dict[str, Callable[[], object]] = {
    "technical": lambda: TechnicalAnalyzer(
        AnalysisContext.from_path,
        SubjectLocator(OnnxSubjectBackend()),
        FocusAnalyzer(),
        ExposureAnalyzer(),
    ),
}
_ANALYZERS: dict[str, Analyzer] = {}


def _get_analyzer(name: str) -> Analyzer:
    analyzer = _ANALYZERS.get(name)
    if analyzer is not None:
        return analyzer
    try:
        factory = _ANALYZER_FACTORIES[name]
    except KeyError as error:
        raise ValueError(f"Unknown analyzer: {name}") from error
    analyzer = cast(Analyzer, factory())
    _ANALYZERS[name] = analyzer
    return analyzer


def analyze_photo(request: PhotoAnalysisRequest) -> PhotoAnalysisResponse:
    outcomes: list[AnalyzerOutcome] = []
    for name in request.analyzers:
        try:
            result = _get_analyzer(name).analyze(request.path)
        except Exception as error:
            outcomes.append(
                AnalyzerOutcome(
                    name=name,
                    status=AnalysisStatus.FAILED,
                    failure=AnalysisFailure(
                        analyzer=name,
                        message=str(error),
                        exception_type=type(error).__name__,
                    ),
                )
            )
            continue
        outcomes.append(
            AnalyzerOutcome(
                name=name,
                status=AnalysisStatus.SUCCEEDED,
                result=result,
            )
        )
    return PhotoAnalysisResponse(
        photo_id=request.photo_id,
        outcomes=tuple(outcomes),
    )
