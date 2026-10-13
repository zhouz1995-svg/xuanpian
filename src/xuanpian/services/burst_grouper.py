"""Burst grouping for consecutive similar photos.

Photos captured within a small time window of each other are treated as one
"similar group" (连拍组).  Groups are derived from EXIF capture time in the
current display order, so the same photo can belong to at most one group.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


_EXIF_DATETIME = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$"
)


@dataclass(frozen=True, slots=True)
class BurstGroup:
    """One group of photos that were captured together."""

    group_id: str
    photo_ids: tuple[str, ...]

    @property
    def size(self) -> int:
        return len(self.photo_ids)


def parse_capture_time(date_taken: str) -> float | None:
    """Parse EXIF ``DateTimeOriginal`` into epoch seconds, or ``None``."""
    match = _EXIF_DATETIME.match(date_taken.strip())
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    try:
        naive = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    return naive.timestamp()


def group_photos(
    photo_ids: list[str],
    capture_time_by_id: Mapping[str, float | None],
    *,
    gap_seconds: float = 3.0,
    min_members: int = 2,
) -> dict[str, BurstGroup]:
    """Group consecutive photos whose capture gaps stay within the threshold.

    Photos with unknown capture time never join a group and break the current
    run.  The returned mapping contains every grouped photo id; photos without
    a group are absent.
    """
    if gap_seconds < 0:
        raise ValueError("gap_seconds must be non-negative")
    if min_members < 2:
        raise ValueError("min_members must be at least 2")

    groups: dict[str, BurstGroup] = {}
    run_start = 0
    run_has_time = True

    def close_run(start: int, end: int) -> None:
        member_ids = tuple(photo_ids[start:end])
        if len(member_ids) >= min_members:
            group = BurstGroup(group_id=member_ids[0], photo_ids=member_ids)
            groups.update({photo_id: group for photo_id in member_ids})

    for index in range(1, len(photo_ids)):
        previous_time = capture_time_by_id.get(photo_ids[index - 1])
        current_time = capture_time_by_id.get(photo_ids[index])
        gap = (
            None
            if previous_time is None or current_time is None
            else current_time - previous_time
        )
        if (
            run_has_time
            and gap is not None
            and 0.0 <= gap <= gap_seconds
        ):
            continue
        close_run(run_start, index)
        run_start = index
        run_has_time = current_time is not None
    close_run(run_start, len(photo_ids))
    return groups
