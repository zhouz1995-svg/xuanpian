from dataclasses import FrozenInstanceError
from uuid import UUID, uuid4

import pytest

from src.xuanpian.tasks.contracts import (
    CancellationToken,
    JobFinished,
    JobProgress,
    JobState,
)


def test_job_state_has_complete_lifecycle() -> None:
    assert [state.value for state in JobState] == [
        "pending",
        "running",
        "canceling",
        "succeeded",
        "failed",
        "canceled",
    ]


def test_job_events_are_immutable_and_keep_identity() -> None:
    project_id = uuid4()
    job_id = uuid4()
    progress = JobProgress(
        project_id=project_id,
        job_id=job_id,
        completed=3,
        total=10,
        failed=1,
        message="正在分析",
    )
    finished = JobFinished(
        project_id=project_id,
        job_id=job_id,
        state=JobState.SUCCEEDED,
        failed=1,
    )

    assert isinstance(progress.project_id, UUID)
    assert progress.job_id == job_id
    assert finished.state is JobState.SUCCEEDED
    with pytest.raises(FrozenInstanceError):
        progress.completed = 4


def test_cancellation_token_is_idempotent_and_thread_safe() -> None:
    token = CancellationToken()

    assert token.is_canceled() is False
    token.cancel()
    token.cancel()

    assert token.is_canceled() is True
