from types import SimpleNamespace

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer
from src.xuanpian.services import analysis_service
from src.xuanpian.services.analysis_service import (
    PhotoAnalysisRequest,
    analyze_photo,
)


class StubAnalyzer:
    def __init__(self, name: str, calls: list[str], error: Exception | None = None):
        self._name = name
        self._calls = calls
        self._error = error

    def analyze(self, path: str) -> object:
        self._calls.append(self._name)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(name=self._name, path=path)


def test_one_analyzer_failure_does_not_stop_later_analyzers(monkeypatch) -> None:
    calls: list[str] = []
    analyzers = {
        "first": StubAnalyzer("first", calls),
        "second": StubAnalyzer(
            "second", calls, ValueError("decode failed")
        ),
        "third": StubAnalyzer("third", calls),
        "fourth": StubAnalyzer("fourth", calls),
    }
    monkeypatch.setattr(
        analysis_service,
        "_ANALYZER_FACTORIES",
        {name: (lambda analyzer=analyzer: analyzer) for name, analyzer in analyzers.items()},
    )
    analysis_service._ANALYZERS.clear()
    request = PhotoAnalysisRequest(
        photo_id="photo-1",
        path="/fake/photo.jpg",
        analyzers=("first", "second", "third", "fourth"),
    )

    response = analyze_photo(request)

    assert response.photo_id == "photo-1"
    assert calls == ["first", "second", "third", "fourth"]
    assert tuple(outcome.name for outcome in response.outcomes) == request.analyzers
    assert tuple(outcome.status for outcome in response.outcomes) == (
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.FAILED,
        AnalysisStatus.SUCCEEDED,
        AnalysisStatus.SUCCEEDED,
    )
    failed = response.outcomes[1]
    assert failed.result is None
    assert failed.failure is not None
    assert failed.failure.analyzer == "second"
    assert failed.failure.exception_type == "ValueError"
    assert failed.failure.message == "decode failed"


def test_technical_analyzer_is_a_registered_single_pipeline() -> None:
    assert "technical" in analysis_service._ANALYZER_FACTORIES


def test_only_technical_analyzer_is_registered() -> None:
    assert set(analysis_service._ANALYZER_FACTORIES) == {"technical"}


def test_technical_factory_builds_pipeline_without_crop_analyzer() -> None:
    analyzer = analysis_service._ANALYZER_FACTORIES["technical"]()

    assert isinstance(analyzer, TechnicalAnalyzer)
