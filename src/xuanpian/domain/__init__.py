"""Domain types shared across the application."""

from src.xuanpian.domain.analysis import AnalysisFailure, AnalysisStatus
from src.xuanpian.domain.technical_analysis import (
    ExposureConclusion,
    FocusConclusion,
    TechnicalAnalysisResult,
)

__all__ = [
    "AnalysisFailure",
    "AnalysisStatus",
    "ExposureConclusion",
    "FocusConclusion",
    "TechnicalAnalysisResult",
]
