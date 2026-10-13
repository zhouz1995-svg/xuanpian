from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtGui import QPixmap

from src.xuanpian.domain.analysis import AnalysisFailure, AnalysisStatus

if TYPE_CHECKING:
    from src.xuanpian.engine.exif_reader import ExifData
    from src.xuanpian.services.import_service import PhotoCandidate

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    SUPPORTED_EXTENSIONS.update([".heic", ".heif"])
except ImportError:
    pass

# RAW extensions (conditional on rawpy)
RAW_EXTENSIONS: set[str] = set()
try:
    from src.xuanpian.engine.image_reader import RAW_EXTENSIONS as _RAW_EXTS

    RAW_EXTENSIONS = _RAW_EXTS
except ImportError:
    pass

ALL_SUPPORTED = SUPPORTED_EXTENSIONS | RAW_EXTENSIONS


@dataclass
class ImageFileModel:
    """Represents an image file loaded into the application."""

    path: str
    filename: str = ""
    thumbnail: QPixmap | None = None
    is_raw: bool = False
    photo_id: str = ""
    jpeg_path: str = ""
    raw_path: str = ""

    technical_status: AnalysisStatus = AnalysisStatus.PENDING
    analysis_failures: dict[str, AnalysisFailure] = field(default_factory=dict)

    # Versioned technical analysis conclusions (technical-v7).
    focus_conclusion: str = ""
    exposure_conclusion: str = ""
    face_count: int = 0
    portrait_assessed: bool = False

    # EXIF metadata
    camera_make: str = ""
    camera_model: str = ""
    lens_model: str = ""
    aperture: str = ""
    shutter_speed: str = ""
    iso: str = ""
    focal_length: str = ""
    date_taken: str = ""
    image_width: int = 0
    image_height: int = 0
    flash: str = ""
    exif_loaded: bool = False
    exif_error: str = ""

    # Derived burst / similar-photo grouping (recomputed on load, not persisted).
    burst_group_id: str = ""
    burst_group_size: int = 0

    # user marking
    mark: str = ""

    # View-only rotation in degrees.
    rotation: int = 0

    def __post_init__(self) -> None:
        if not self.filename:
            self.filename = Path(self.path).name
        ext = Path(self.path).suffix.lower()
        if not self.jpeg_path and not self.raw_path:
            if ext in RAW_EXTENSIONS:
                self.raw_path = self.path
            else:
                self.jpeg_path = self.path
        self.path = self.analysis_path
        self.photo_id = self.photo_id or self.path
        self.is_raw = bool(self.raw_path and not self.jpeg_path)
        self.rotation %= 360

    @property
    def analysis_path(self) -> str:
        return self.jpeg_path or self.raw_path or self.path

    @property
    def source_paths(self) -> tuple[str, ...]:
        return tuple(path for path in (self.jpeg_path, self.raw_path) if path)

    @property
    def has_terminal_technical_status(self) -> bool:
        """Return whether culling may consume this model's analysis state."""
        status = self.technical_status
        return isinstance(status, AnalysisStatus) and status in {
            AnalysisStatus.SUCCEEDED,
            AnalysisStatus.FAILED,
            AnalysisStatus.CANCELED,
        }

    @classmethod
    def from_candidate(cls, candidate: PhotoCandidate) -> ImageFileModel:
        return cls(
            path=candidate.analysis_path,
            filename=candidate.display_name,
            photo_id=candidate.photo_id,
            jpeg_path=candidate.jpeg_path,
            raw_path=candidate.raw_path,
            is_raw=candidate.is_raw_only,
        )

    def upsert_candidate(self, candidate: PhotoCandidate) -> None:
        if self.photo_id and self.photo_id != candidate.photo_id:
            raise ValueError("candidate photo_id does not match model")
        self.photo_id = candidate.photo_id
        self.jpeg_path = candidate.jpeg_path
        self.raw_path = candidate.raw_path
        self.path = candidate.analysis_path
        self.filename = candidate.display_name
        self.is_raw = candidate.is_raw_only

    def apply_exif(self, data: ExifData) -> None:
        for field_name in (
            "camera_make",
            "camera_model",
            "lens_model",
            "aperture",
            "shutter_speed",
            "iso",
            "focal_length",
            "date_taken",
            "image_width",
            "image_height",
            "flash",
        ):
            setattr(self, field_name, getattr(data, field_name))
        self.exif_loaded = True
        self.exif_error = data.error


def _stems_related(a: str, b: str) -> bool:
    """Check if two stems refer to the same photo.

    Handles cases where cameras add suffixes to RAW filenames:
    - DSC_1234  matches  DSC_1234_01  (Nikon suffix pattern)
    - IMG_0001  matches  IMG_0001_v2
    Does NOT match DSC_1234 with DSC_12345 (different number).
    """
    if a == b:
        return True
    shorter = min(a, b, key=len)
    longer = max(a, b, key=len)
    if len(shorter) < len(longer) and longer.startswith(shorter):
        # Only match if the next char after the shorter stem is a non-alphanumeric separator
        sep = longer[len(shorter)]
        if not sep.isalnum():
            return True
    return False


def _build_stem_groups(files: list[tuple[str, str, str]]) -> list[list[tuple[str, str, str]]]:
    """Group files by related stems. Each file is (stem, suffix, resolved_path)."""
    groups: list[list[tuple[str, str, str]]] = []

    for item in files:
        stem, suffix, path = item
        matched = False
        for group in groups:
            if any(_stems_related(stem, g[0]) for g in group):
                group.append(item)
                matched = True
                break
        if not matched:
            groups.append([item])

    return groups


def _legacy_scan_images(folder: str) -> list[str]:
    """Recursively scan a folder for supported image files.

    When the same photo exists in both RAW and JPEG/HEIC formats (same stem),
    only the RAW version is kept to avoid duplicate entries.

    macOS hidden files (._* and .DS_Store) are skipped.

    Returns a sorted list of absolute paths.
    """
    root = Path(folder)
    if not root.is_dir():
        return []

    # Collect candidates: (stem, suffix, resolved_path)
    all_files: list[tuple[str, str, str]] = []
    skipped_hidden = 0
    heic_found = False
    raw_found_no_lib = False
    total_files = 0

    for p in root.rglob("*"):
        if not p.is_file():
            continue

        name = p.name
        # Skip macOS resource-fork and hidden files
        if name.startswith("._") or name == ".DS_Store" or name.startswith(".Bridge"):
            skipped_hidden += 1
            continue

        suffix = p.suffix.lower()
        if suffix in ALL_SUPPORTED:
            total_files += 1
            stem = p.stem
            resolved = str(p.resolve())
            all_files.append((stem, suffix, resolved))
        elif suffix in {".heic", ".heif"} and suffix not in SUPPORTED_EXTENSIONS:
            heic_found = True
        elif suffix in {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf",
                        ".rw2", ".pef", ".srw", ".crw", ".nrw"} and suffix not in RAW_EXTENSIONS:
            raw_found_no_lib = True

    if heic_found:
        logger.warning(
            "HEIC/HEIF files found but pillow-heif is not installed. "
            "Install with: pip install pillow-heif"
        )

    if raw_found_no_lib:
        logger.warning(
            "RAW files found but rawpy is not installed. "
            "Install with: pip install rawpy"
        )

    # Group by related stems (handles NEF suffix patterns like DSC_1234 vs DSC_1234_01)
    stem_groups = _build_stem_groups(all_files)

    # Deduplicate: for each group, prefer RAW over JPEG/HEIC
    paths: list[str] = []
    jpeg_skipped = 0
    for group in stem_groups:
        if len(group) == 1:
            paths.append(group[0][2])
        else:
            raw_path = None
            jpeg_path = None
            for stem, suffix, fpath in group:
                if suffix in RAW_EXTENSIONS:
                    raw_path = fpath
                else:
                    jpeg_path = fpath

            if raw_path is not None:
                paths.append(raw_path)
                if jpeg_path is not None:
                    jpeg_skipped += 1
            elif jpeg_path is not None:
                paths.append(jpeg_path)
            else:
                paths.append(group[0][2])

    paths.sort()

    if skipped_hidden:
        logger.info(f"跳过 {skipped_hidden} 个隐藏文件")
    if jpeg_skipped:
        logger.info(
            f"去重: {jpeg_skipped} 张 JPEG 被跳过（保留同名 RAW）"
        )
    logger.info(
        f"扫描完成: {total_files} 个文件 → {len(stem_groups)} 组 → {len(paths)} 张照片导入"
    )

    return paths


def scan_images(folder: str) -> list[str]:
    """Compatibility wrapper for the P1 streaming import service."""
    from src.xuanpian.services.import_service import scan_images as scan

    return scan(folder)
