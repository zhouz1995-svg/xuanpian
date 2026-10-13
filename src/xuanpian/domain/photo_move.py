"""Storage-independent values describing one recoverable photo move."""

from __future__ import annotations

from dataclasses import dataclass, replace
from os.path import isabs

from .culling import PhotoMark
from .photo import AssetKind


def _require_absolute_path(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not isabs(value):
        raise ValueError(f"{field_name} must be an absolute path")


def _require_non_negative_integer(value: object, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MoveItem:
    """One validated asset move within an ordered logical-photo move."""

    ordinal: int
    kind: AssetKind
    source_path: str
    target_path: str
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        _require_non_negative_integer(self.ordinal, "ordinal")
        if not isinstance(self.kind, AssetKind):
            raise ValueError("kind must be an AssetKind")
        _require_absolute_path(self.source_path, "source_path")
        _require_absolute_path(self.target_path, "target_path")
        _require_non_negative_integer(self.size, "size")
        _require_non_negative_integer(self.mtime_ns, "mtime_ns")


@dataclass(frozen=True, slots=True)
class PhotoMoveTransaction:
    """A complete, ordered, persistable move for one logical photo."""

    id: str
    photo_id: str
    before_mark: PhotoMark
    after_mark: PhotoMark
    created_at_ns: int
    items: tuple[MoveItem, ...]
    inverse_of: PhotoMoveTransaction | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("id is required")
        if not isinstance(self.photo_id, str) or not self.photo_id:
            raise ValueError("photo_id is required")
        if not isinstance(self.before_mark, PhotoMark):
            raise ValueError("before_mark must be a PhotoMark")
        if not isinstance(self.after_mark, PhotoMark):
            raise ValueError("after_mark must be a PhotoMark")
        _require_non_negative_integer(self.created_at_ns, "created_at_ns")
        if not isinstance(self.items, tuple) or not self.items:
            raise ValueError("items must be a non-empty tuple")
        if not all(isinstance(item, MoveItem) for item in self.items):
            raise ValueError("items must contain MoveItem values")
        if tuple(item.ordinal for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("items must have contiguous ordinals starting at zero")
        self.validate_inverse()

    def validate_inverse(self) -> None:
        """An inverse is exactly one reversal of a non-inverse snapshot."""
        original = self.inverse_of
        if original is None:
            return
        if not isinstance(original, PhotoMoveTransaction) or original.inverse_of is not None:
            raise ValueError("inverse provenance must be one forward transaction")
        if (
            self.id == original.id
            or self.photo_id != original.photo_id
            or self.before_mark is not original.after_mark
            or self.after_mark is not original.before_mark
            or self.items != tuple(
                replace(item, source_path=item.target_path, target_path=item.source_path)
                for item in original.items
            )
        ):
            raise ValueError("inverse must exactly reverse the recorded action")
