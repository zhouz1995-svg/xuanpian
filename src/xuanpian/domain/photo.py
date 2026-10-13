"""Persistent-domain records for logical photos and their source assets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from os.path import isabs


class AssetKind(str, Enum):
    """The two source resource kinds that can belong to one logical photo."""

    JPEG = "jpeg"
    RAW = "raw"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Filesystem identity data used to validate a persisted asset."""

    path: str
    size: int
    mtime_ns: int

    def __post_init__(self) -> None:
        if not isabs(self.path):
            raise ValueError("path must be absolute")
        if not isinstance(self.size, int) or isinstance(self.size, bool):
            raise ValueError("size must be a non-negative integer")
        if self.size < 0:
            raise ValueError("size must be a non-negative integer")
        if not isinstance(self.mtime_ns, int) or isinstance(self.mtime_ns, bool):
            raise ValueError("mtime_ns must be a non-negative integer")
        if self.mtime_ns < 0:
            raise ValueError("mtime_ns must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PhotoAssetRecord:
    """One persisted source resource for a logical photo."""

    kind: AssetKind
    fingerprint: FileFingerprint
    origin_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AssetKind):
            raise ValueError("kind must be an AssetKind")
        if not isinstance(self.fingerprint, FileFingerprint):
            raise ValueError("fingerprint must be a FileFingerprint")
        if not isinstance(self.origin_path, str) or not isabs(self.origin_path):
            raise ValueError("origin_path must be an absolute path")


@dataclass(frozen=True, slots=True)
class PhotoRecord:
    """A logical photo and its optional JPEG and RAW source resources."""

    id: str
    filename: str
    mark: str
    jpeg: PhotoAssetRecord | None
    raw: PhotoAssetRecord | None

    def __post_init__(self) -> None:
        if self.jpeg is not None and self.jpeg.kind is not AssetKind.JPEG:
            raise ValueError("jpeg asset must have JPEG kind")
        if self.raw is not None and self.raw.kind is not AssetKind.RAW:
            raise ValueError("raw asset must have RAW kind")

    @property
    def analysis_asset(self) -> PhotoAssetRecord | None:
        """Return the preferred resource for analysis, preferring JPEG."""
        return self.jpeg or self.raw

    @property
    def source_assets(self) -> tuple[PhotoAssetRecord, ...]:
        """Return present resources in stable JPEG-then-RAW order."""
        return tuple(asset for asset in (self.jpeg, self.raw) if asset is not None)


_NUMBER_CHUNK = re.compile(r"(\d+)")


def natural_sort_key(value: str) -> tuple[object, ...]:
    """Human-friendly filename ordering: ``IMG_2.jpg`` sorts before ``IMG_10.jpg``."""
    def _chunk(part: str) -> tuple[int, object]:
        return (0, int(part)) if part.isdigit() else (1, part.casefold())

    return tuple(_chunk(part) for part in _NUMBER_CHUNK.split(value))
