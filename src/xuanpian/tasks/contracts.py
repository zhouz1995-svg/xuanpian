from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import TypeAlias
from uuid import UUID


JobId: TypeAlias = UUID
ProjectId: TypeAlias = UUID


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class JobProgress:
    project_id: ProjectId
    job_id: JobId
    completed: int
    total: int
    failed: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class JobFinished:
    project_id: ProjectId
    job_id: JobId
    state: JobState
    failed: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class CancellationToken:
    _event: Event = field(default_factory=Event, init=False, repr=False, compare=False)

    def cancel(self) -> None:
        self._event.set()

    def is_canceled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class JobHandle:
    job_id: JobId
    _token: CancellationToken
    _done: Event

    def cancel(self) -> None:
        self._token.cancel()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)
