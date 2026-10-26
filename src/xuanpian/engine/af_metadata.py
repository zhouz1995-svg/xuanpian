from __future__ import annotations

import json
import math
import shutil  # Kept for existing callers that patch the development lookup.
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.xuanpian.domain.technical_analysis import Rect
from src.xuanpian.engine.metadata_runtime import RuntimeUnavailable, resolve_metadata_runtime, _is_link


@dataclass(frozen=True, slots=True)
class AFHint:
    bounds: Rect | None = None
    reason: str = "unavailable"


_SUPPORTED_CAMERA = ("NIKON CORPORATION", "NIKON Z50_2")
_SUPPORTED_AF_VERSION = "0402"
_RAW_EXTENSIONS = frozenset(
    {
        ".arw",
        ".cr2",
        ".cr3",
        ".crw",
        ".dng",
        ".nef",
        ".nrw",
        ".orf",
        ".pef",
        ".raf",
        ".rw2",
        ".srw",
    }
)
_RAW_FILE_TYPES = frozenset(extension[1:].upper() for extension in _RAW_EXTENSIONS)
_EXIFTOOL_TAGS = (
    "FileType",
    "Make",
    "Model",
    "AFInfo2Version",
    "AFCoordinatesAvailable",
    "AFImageWidth",
    "AFImageHeight",
    "AFAreaXPosition",
    "AFAreaYPosition",
    "AFAreaWidth",
    "AFAreaHeight",
    "AFPointsUsed",
    "Orientation",
    "ImageWidth",
    "ImageHeight",
)


def _positive_integer(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return value


def _positive_integer_pair(value: object) -> tuple[int, int] | None:
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    width = _positive_integer(value[0])
    height = _positive_integer(value[1])
    if width is None or height is None:
        return None
    return width, height


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _has_used_af_points(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return any(int(item, 16) != 0 for item in value.split())
    except ValueError:
        return False


def _transform_rect(rect: Rect, orientation: int) -> Rect:
    x, y, width, height = rect.x, rect.y, rect.width, rect.height
    if orientation == 1:
        values = (x, y, width, height)
    elif orientation == 2:
        values = (1.0 - (x + width), y, width, height)
    elif orientation == 3:
        values = (1.0 - (x + width), 1.0 - (y + height), width, height)
    elif orientation == 4:
        values = (x, 1.0 - (y + height), width, height)
    elif orientation == 5:
        values = (y, x, height, width)
    elif orientation == 6:
        values = (1.0 - (y + height), x, height, width)
    elif orientation == 7:
        values = (1.0 - (y + height), 1.0 - (x + width), height, width)
    else:
        values = (y, 1.0 - (x + width), height, width)
    return Rect(*values)


def parse_af_metadata(
    data: dict[str, Any], decoded_size: tuple[int, int]
) -> AFHint:
    """Validate Nikon AF metadata and map it into decoded-image coordinates."""
    decoded = _positive_integer_pair(decoded_size)
    if decoded is None:
        return AFHint(reason="invalid_decoded_dimensions")
    if not isinstance(data, dict):
        return AFHint(reason="metadata_invalid")
    file_type = data.get("FileType")
    if not isinstance(file_type, str):
        return AFHint(reason="unsupported_file_type")
    if file_type in _RAW_FILE_TYPES:
        return AFHint(reason="raw_mapping_unavailable")
    if file_type != "JPEG":
        return AFHint(reason="unsupported_file_type")
    if (data.get("Make"), data.get("Model")) != _SUPPORTED_CAMERA:
        return AFHint(reason="unsupported_camera")
    if data.get("AFInfo2Version") != _SUPPORTED_AF_VERSION:
        return AFHint(reason="unsupported_af_version")

    available = data.get("AFCoordinatesAvailable")
    if (
        not isinstance(available, int)
        or isinstance(available, bool)
        or available not in (0, 1)
    ):
        return AFHint(reason="invalid_coordinate_flag")
    if available == 0:
        reason = (
            "points_only"
            if _has_used_af_points(data.get("AFPointsUsed"))
            else "coordinates_unavailable"
        )
        return AFHint(reason=reason)

    af_width = _positive_integer(data.get("AFImageWidth"))
    af_height = _positive_integer(data.get("AFImageHeight"))
    if af_width is None or af_height is None:
        return AFHint(reason="invalid_af_dimensions")

    if "ImageWidth" in data or "ImageHeight" in data:
        image_width = _positive_integer(data.get("ImageWidth"))
        image_height = _positive_integer(data.get("ImageHeight"))
        if (image_width, image_height) != (af_width, af_height):
            return AFHint(reason="image_dimensions_mismatch")

    orientation = data.get("Orientation")
    if (
        not isinstance(orientation, int)
        or isinstance(orientation, bool)
        or orientation not in range(1, 9)
    ):
        return AFHint(reason="invalid_orientation")

    expected_size = (
        (af_width, af_height) if orientation <= 4 else (af_height, af_width)
    )
    if decoded != expected_size:
        return AFHint(reason="decoded_dimensions_mismatch")

    x = _finite_number(data.get("AFAreaXPosition"))
    y = _finite_number(data.get("AFAreaYPosition"))
    width = _finite_number(data.get("AFAreaWidth"))
    height = _finite_number(data.get("AFAreaHeight"))
    if (
        x is None
        or y is None
        or width is None
        or height is None
        or width <= 0
        or height <= 0
    ):
        return AFHint(reason="invalid_af_area")

    left = x - width / 2.0
    top = y - height / 2.0
    right = x + width / 2.0
    bottom = y + height / 2.0
    if left < 0 or top < 0 or right > af_width or bottom > af_height:
        return AFHint(reason="af_area_out_of_bounds")

    normalized = Rect(
        left / af_width,
        top / af_height,
        width / af_width,
        height / af_height,
    )
    return AFHint(_transform_rect(normalized, orientation), "available")


def _frozen_runtime_root(meipass: Path) -> Path:
    """Select only resources belonging to the running frozen application."""
    if sys.platform == "win32":
        internal = Path(meipass)
        try:
            app = Path(sys.executable).resolve(strict=True).parent
            real_internal = internal.resolve(strict=True)
        except (OSError, RuntimeError):
            raise RuntimeUnavailable("metadata_runtime_missing") from None
        if (internal.name != "_internal" or _is_link(internal)
                or not internal.is_dir() or real_internal != app / "_internal"):
            raise RuntimeUnavailable("metadata_runtime_invalid")
        return internal
    frameworks = Path(meipass)
    contents = frameworks.parent
    app = contents.parent
    if frameworks.name != "Frameworks" or contents.name != "Contents" or app.suffix != ".app":
        raise RuntimeUnavailable("metadata_runtime_missing")
    resources = contents / "Resources"
    if frameworks.is_symlink() or resources.is_symlink():
        raise RuntimeUnavailable("metadata_runtime_invalid")
    if not resources.is_dir():
        raise RuntimeUnavailable("metadata_runtime_missing")
    try:
        real_app = app.resolve(strict=True)
        real_frameworks = frameworks.resolve(strict=True)
        real_resources = resources.resolve(strict=True)
    except (OSError, RuntimeError):
        raise RuntimeUnavailable("metadata_runtime_missing") from None
    if real_frameworks != real_app / "Contents" / "Frameworks" or \
            real_resources != real_app / "Contents" / "Resources":
        raise RuntimeUnavailable("metadata_runtime_invalid")
    return resources


def read_af_hint(path: str, decoded_size: tuple[int, int]) -> AFHint:
    """Read a validated AF hint without modifying the source image."""
    try:
        source = Path(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return AFHint(reason="file_unavailable")
    if source.suffix.lower() in _RAW_EXTENSIONS:
        return AFHint(reason="raw_mapping_unavailable")
    try:
        stat = source.stat()
    except OSError:
        return AFHint(reason="file_unavailable")

    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if not meipass:
            return AFHint(reason="metadata_runtime_missing")
        try:
            bundle_root = _frozen_runtime_root(Path(meipass))
        except RuntimeUnavailable as error:
            return AFHint(reason=error.reason)
    else:
        bundle_root = None
    try:
        runtime = resolve_metadata_runtime(
            bundle_root=bundle_root, development_executable="exiftool"
        )
    except RuntimeUnavailable as error:
        return AFHint(reason=error.reason)
    return _read_af_hint_cached(
        str(source),
        decoded_size,
        stat.st_mtime_ns,
        stat.st_size,
        runtime.identity,
        runtime.command_prefix,
        runtime.environment,
    )


@lru_cache(maxsize=256)
def _read_af_hint_cached(
    source: str,
    decoded_size: tuple[int, int],
    _mtime_ns: int,
    _size: int,
    _runtime_identity: str,
    command_prefix: tuple[str, ...],
    environment: tuple[tuple[str, str], ...],
) -> AFHint:
    command = [
        *command_prefix,
        "-j",
        "-n",
        *(f"-{tag}" for tag in _EXIFTOOL_TAGS),
        source,
    ]
    try:
        windows_options = {"encoding": "utf-8", "creationflags": 0x08000000} if sys.platform == "win32" else {}
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=dict(environment),
            **windows_options,
        )
    except UnicodeError:
        return AFHint(reason="metadata_invalid_json")
    except subprocess.TimeoutExpired:
        return AFHint(reason="metadata_timeout")
    except (OSError, subprocess.SubprocessError):
        return AFHint(reason="metadata_read_error")
    if completed.returncode != 0:
        return AFHint(reason="metadata_read_error")
    try:
        records = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        return AFHint(reason="metadata_invalid_json")
    if not isinstance(records, list):
        return AFHint(reason="metadata_invalid_json")
    if not records:
        return AFHint(reason="metadata_missing")
    if len(records) != 1 or not isinstance(records[0], dict):
        return AFHint(reason="metadata_invalid_json")
    return parse_af_metadata(records[0], decoded_size)
