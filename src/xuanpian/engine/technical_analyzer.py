"""Single-decode orchestration for technical photo analysis."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.xuanpian.domain.technical_analysis import TechnicalAnalysisResult, TECHNICAL_ALGORITHM_VERSION
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.exposure_analyzer import ExposureAnalyzer
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SUBJECT_SOURCES, SubjectLocationResult


class TechnicalAnalyzer:
    def __init__(
        self,
        context_factory: Callable[[str], Any],
        subject_locator: Any,
        focus_analyzer: Any,
        exposure_analyzer: Any,
    ) -> None:
        self._context_factory = context_factory
        self._subject_locator = subject_locator
        self._focus_analyzer = focus_analyzer
        self._exposure_analyzer = exposure_analyzer

    def analyze(self, path: str) -> TechnicalAnalysisResult:
        context = self._context_factory(path)
        try:
            subjects = self._subject_locator.locate(context)
        except Exception:
            subjects = SubjectLocationResult(
                (),
                None,
                0,
                diagnostics=("subject capability unavailable",),
                failed_sources=SUBJECT_SOURCES,
            )
        focus = self._focus_analyzer.analyze(context, subjects)
        exposure = self._exposure_analyzer.analyze_context(context)
        return TechnicalAnalysisResult(
            TECHNICAL_ALGORITHM_VERSION,
            focus,
            exposure,
            subjects.face_count,
            "face" not in subjects.failed_sources,
        )
