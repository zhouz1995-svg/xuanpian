from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class AnalysisStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True)
class AnalysisFailure:
    analyzer: str
    message: str
    exception_type: str
