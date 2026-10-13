"""Controlled persisted marks for the culling workflow."""

from __future__ import annotations

from enum import Enum


class PhotoMark(str, Enum):
    """The complete set of marks managed by recoverable culling."""

    UNPROCESSED = "未处理"
    KEEP = "保留"
    HOLD = "待定"
    DISCARD = "舍弃"


_LEGACY_MARKS = {
    "": PhotoMark.UNPROCESSED,
    "入选": PhotoMark.KEEP,
    "待定": PhotoMark.HOLD,
    "淘汰": PhotoMark.DISCARD,
    **{mark.value: mark for mark in PhotoMark},
}


def normalize_legacy_mark(value: str) -> PhotoMark | None:
    """Map known historical values without assigning a meaning to unknowns."""
    return _LEGACY_MARKS.get(value)
