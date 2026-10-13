from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import stat
from typing import Callable, Iterator
from uuid import uuid4

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    PhotoAssetRecord,
    PhotoRecord,
    natural_sort_key,
)
from src.xuanpian.engine.image_reader import RAW_EXTENSIONS
from src.xuanpian.services.fingerprints import fingerprint


logger = logging.getLogger(__name__)

KNOWN_RAW_EXTENSIONS = {
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".dng",
    ".raf", ".orf", ".rw2", ".pef", ".srw", ".crw",
}
KNOWN_HEIF_EXTENSIONS = {".heic", ".heif"}
RASTER_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
}
try:
    import pillow_heif  # noqa: F401
except ImportError:
    pass
else:
    RASTER_EXTENSIONS.update(KNOWN_HEIF_EXTENSIONS)

ALL_SUPPORTED = RASTER_EXTENSIONS | RAW_EXTENSIONS
MANAGED_STATUS_DIRECTORIES = frozenset(
    {
        PhotoMark.KEEP.value,
        PhotoMark.HOLD.value,
        PhotoMark.DISCARD.value,
    }
)


@dataclass(frozen=True, slots=True)
class PhotoCandidate:
    photo_id: str
    display_name: str
    jpeg_path: str = ""
    raw_path: str = ""
    superseded_photo_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.photo_id or not self.display_name:
            raise ValueError("photo_id and display_name are required")
        if not self.jpeg_path and not self.raw_path:
            raise ValueError("at least one source path is required")

    @property
    def analysis_path(self) -> str:
        return self.jpeg_path or self.raw_path

    @property
    def source_paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.jpeg_path, self.raw_path) if path)

    @property
    def is_raw_only(self) -> bool:
        return bool(self.raw_path and not self.jpeg_path)


def candidate_to_photo_record(
    candidate: PhotoCandidate,
    *,
    mark: str = PhotoMark.UNPROCESSED.value,
) -> PhotoRecord:
    """Snapshot the source files currently available for ``candidate``."""
    return PhotoRecord(
        id=candidate.photo_id,
        filename=candidate.display_name,
        mark=mark,
        jpeg=_available_asset(candidate.jpeg_path, AssetKind.JPEG),
        raw=_available_asset(candidate.raw_path, AssetKind.RAW),
    )


def _available_asset(
    path: str,
    kind: AssetKind,
) -> PhotoAssetRecord | None:
    if not path:
        return None
    try:
        file_fingerprint = fingerprint(Path(path))
    except FileNotFoundError:
        return None
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=file_fingerprint,
        origin_path=file_fingerprint.path,
    )


class PhotoPairer:
    def __init__(self) -> None:
        self._groups: dict[tuple[str, str], list[PhotoCandidate]] = {}
        self._order_by_photo_id: dict[str, int] = {}
        self._next_order = 0

    @staticmethod
    def _key(path: Path, stem: str | None = None) -> tuple[str, str]:
        return (str(path.parent.resolve()).casefold(), (stem or path.stem).casefold())

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        name = path.name
        return (
            name.startswith("._")
            or name == ".DS_Store"
            or name.startswith(".Bridge")
        )

    @staticmethod
    def _kind(path: Path) -> str:
        return (
            "raw"
            if path.suffix.lower() in KNOWN_RAW_EXTENSIONS
            else "jpeg"
        )

    @staticmethod
    def _stems_related(first: str, second: str) -> bool:
        first = first.casefold()
        second = second.casefold()
        if first == second:
            return True
        shorter, longer = sorted((first, second), key=len)
        return (
            longer.startswith(shorter)
            and len(longer) > len(shorter)
            and not longer[len(shorter)].isalnum()
        )

    def _add_file(self, path: Path) -> PhotoCandidate:
        resolved = path.resolve()
        kind = self._kind(resolved)
        key = self._key(resolved)
        groups = self._groups.setdefault(key, [])
        candidate = PhotoCandidate(
            photo_id=str(uuid4()),
            display_name=resolved.name,
            jpeg_path=str(resolved) if kind == "jpeg" else "",
            raw_path=str(resolved) if kind == "raw" else "",
        )
        groups.append(candidate)
        self._order_by_photo_id[candidate.photo_id] = self._next_order
        self._next_order += 1
        return candidate

    def _merge_pair(
        self,
        jpeg: PhotoCandidate,
        raw: PhotoCandidate,
    ) -> PhotoCandidate:
        survivor, superseded = sorted(
            (jpeg, raw),
            key=lambda candidate: self._order_by_photo_id[
                candidate.photo_id
            ],
        )
        return PhotoCandidate(
            photo_id=survivor.photo_id,
            display_name=Path(jpeg.jpeg_path).name,
            jpeg_path=jpeg.jpeg_path,
            raw_path=raw.raw_path,
            superseded_photo_ids=(superseded.photo_id,),
        )

    def add_supported(self, path: Path) -> PhotoCandidate | None:
        if self._is_hidden(path):
            return None
        suffix = path.suffix.lower()
        if suffix in KNOWN_RAW_EXTENSIONS and suffix not in RAW_EXTENSIONS:
            logger.warning(
                "RAW file ignored because rawpy is unavailable: %s",
                path,
            )
            return None
        if (
            suffix in KNOWN_HEIF_EXTENSIONS
            and suffix not in RASTER_EXTENSIONS
        ):
            logger.warning(
                "HEIC/HEIF file ignored because pillow-heif is unavailable: %s",
                path,
            )
            return None
        if suffix not in ALL_SUPPORTED:
            return None
        return self._add_file(path)

    def finish(self) -> tuple[PhotoCandidate, ...]:
        by_parent: dict[str, list[PhotoCandidate]] = {}
        emitted: list[PhotoCandidate] = []
        for (parent, _stem), candidates in self._groups.items():
            jpegs = [candidate for candidate in candidates if candidate.jpeg_path]
            raws = [candidate for candidate in candidates if candidate.raw_path]
            if len(jpegs) == 1 and len(raws) == 1:
                emitted.append(self._merge_pair(jpegs[0], raws[0]))
            elif len(candidates) == 1:
                by_parent.setdefault(parent, []).append(candidates[0])

        merged_ids: set[str] = set()
        for candidates in by_parent.values():
            jpegs = [candidate for candidate in candidates if candidate.jpeg_path]
            raws = [candidate for candidate in candidates if candidate.raw_path]
            jpeg_matches = {
                jpeg.photo_id: [
                    raw
                    for raw in raws
                    if self._stems_related(
                        Path(jpeg.jpeg_path).stem,
                        Path(raw.raw_path).stem,
                    )
                ]
                for jpeg in jpegs
            }
            raw_matches = {
                raw.photo_id: [
                    jpeg
                    for jpeg in jpegs
                    if self._stems_related(
                        Path(jpeg.jpeg_path).stem,
                        Path(raw.raw_path).stem,
                    )
                ]
                for raw in raws
            }

            for jpeg in jpegs:
                matches = jpeg_matches[jpeg.photo_id]
                if len(matches) != 1:
                    continue
                raw = matches[0]
                if len(raw_matches[raw.photo_id]) != 1:
                    continue
                if (
                    jpeg.photo_id in merged_ids
                    or raw.photo_id in merged_ids
                ):
                    continue

                emitted.append(self._merge_pair(jpeg, raw))
                merged_ids.update((jpeg.photo_id, raw.photo_id))

        return tuple(
            sorted(
                emitted,
                key=lambda candidate: self._order_by_photo_id[
                    candidate.photo_id
                ],
            )
        )


class ImportService:
    def __init__(
        self,
        path_source: Callable[[Path], Iterator[Path]] | None = None,
    ) -> None:
        self._path_source = path_source or self.iter_paths

    @staticmethod
    def iter_paths(root: Path) -> Iterator[Path]:
        lexical_root = Path(os.path.abspath(os.fspath(root)))
        if lexical_root.name in MANAGED_STATUS_DIRECTORIES:
            return
        try:
            root_metadata = root.lstat()
        except OSError:
            return
        if not stat.S_ISDIR(root_metadata.st_mode):
            return
        paths: list[Path] = []
        for directory, directories, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directories[:] = sorted(
                (
                    name
                    for name in directories
                    if name not in MANAGED_STATUS_DIRECTORIES
                ),
                key=natural_sort_key,
            )
            paths.extend(Path(directory) / name for name in filenames)
        for path in sorted(paths, key=lambda p: natural_sort_key(p.name)):
            try:
                metadata = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                yield path

    def scan(
        self,
        root: Path,
        batch_size: int = 50,
    ) -> Iterator[tuple[PhotoCandidate, ...]]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        pairer = PhotoPairer()
        batch: list[PhotoCandidate] = []
        for path in self._path_source(root):
            candidate = pairer.add_supported(path)
            if candidate is None:
                continue
            batch.append(candidate)
            if len(batch) == batch_size:
                yield tuple(batch)
                batch.clear()
        for candidate in pairer.finish():
            batch.append(candidate)
            if len(batch) == batch_size:
                yield tuple(batch)
                batch.clear()
        if batch:
            yield tuple(batch)


def scan_images(folder: str) -> list[str]:
    latest: dict[str, PhotoCandidate] = {}
    for batch in ImportService().scan(Path(folder)):
        for candidate in batch:
            for photo_id in candidate.superseded_photo_ids:
                latest.pop(photo_id, None)
            latest[candidate.photo_id] = candidate
    return sorted(
        (candidate.analysis_path for candidate in latest.values()),
        key=lambda path: natural_sort_key(Path(path).name),
    )
