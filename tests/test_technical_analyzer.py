from types import SimpleNamespace

from src.xuanpian.domain.technical_analysis import (
    ExposureConclusion, ExposureResultV2, FocusConclusion, FocusResult,
)
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer
from src.xuanpian.engine.subject_locator import SUBJECT_SOURCES, SubjectLocationResult


class _Locator:
    def __init__(self, failed=frozenset()):
        self._failed = frozenset(failed)

    def locate(self, context):
        return SubjectLocationResult((), None, 0, failed_sources=self._failed)


class _RaisingLocator:
    def locate(self, context):
        raise RuntimeError("models unavailable at /private/models/subject.task")


class _Focus:
    def __init__(self):
        self.subjects = None

    def analyze(self, context, subjects):
        self.subjects = subjects
        return FocusResult(FocusConclusion.CLEAR, .8)


class _Exposure:
    def analyze_context(self, context):
        return ExposureResultV2(ExposureConclusion.NORMAL, 0, 0)


def test_pipeline_builds_one_context_for_all_dimensions():
    calls = []
    context = SimpleNamespace()
    analyzer = TechnicalAnalyzer(
        lambda path: calls.append(path) or context,
        _Locator(),
        _Focus(),
        _Exposure(),
    )

    result = analyzer.analyze("a.jpg")

    assert calls == ["a.jpg"]
    assert result.focus.conclusion is FocusConclusion.CLEAR
    assert result.portrait_assessed is True


def test_face_failure_is_persisted_as_unassessed_portrait():
    analyzer = TechnicalAnalyzer(
        lambda path: SimpleNamespace(),
        _Locator(failed={"face"}),
        _Focus(),
        _Exposure(),
    )

    result = analyzer.analyze("a.jpg")

    assert result.face_count == 0
    assert result.portrait_assessed is False


def test_subject_model_failure_keeps_exposure_and_sanitizes_diagnostics():
    focus = _Focus()
    analyzer = TechnicalAnalyzer(
        lambda path: SimpleNamespace(),
        _RaisingLocator(),
        focus,
        _Exposure(),
    )

    result = analyzer.analyze("a.jpg")

    assert result.exposure.conclusion is ExposureConclusion.NORMAL
    assert result.face_count == 0
    assert result.portrait_assessed is False
    assert focus.subjects.failed_sources == SUBJECT_SOURCES
    assert focus.subjects.diagnostics == ("subject capability unavailable",)
    assert "/private/models" not in str(result.to_dict())
