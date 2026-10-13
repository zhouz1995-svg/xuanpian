"""Filesystem fingerprints for persisted source assets."""

from __future__ import annotations

import stat
from pathlib import Path

from ..domain.photo import FileFingerprint


def fingerprint(path: Path) -> FileFingerprint:
    """Return the resolved path and one stat snapshot for a regular file."""
    resolved = Path(path).resolve(strict=True)
    snapshot = resolved.stat()
    if not stat.S_ISREG(snapshot.st_mode):
        raise ValueError(f"path is not a regular file: {resolved}")
    return FileFingerprint(
        path=str(resolved),
        size=snapshot.st_size,
        mtime_ns=snapshot.st_mtime_ns,
    )
