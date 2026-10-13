from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image

from src.xuanpian.engine.image_reader import RAW_EXTENSIONS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExifData:
    path: str
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
    error: str = ""


def _rational_to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _fmt_aperture(value: object) -> str:
    number = _rational_to_float(value)
    return "" if number is None else f"f/{number:.1f}"


def _fmt_shutter(value: object) -> str:
    number = _rational_to_float(value)
    if number is None or number <= 0:
        return ""
    return f"{number:.0f}s" if number >= 1 else f"1/{1 / number:.0f}s"


def _fmt_focal(value: object) -> str:
    number = _rational_to_float(value)
    return "" if number is None else f"{number:.0f}mm"


def _fmt_iso(value: object) -> str:
    if value is None:
        return ""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return ""


def _fmt_flash(value: object) -> str:
    try:
        return "闪光" if int(value) & 1 else ""
    except (TypeError, ValueError):
        return ""


def _failed(data: ExifData, error: Exception) -> ExifData:
    message = f"{data.path}: {type(error).__name__}: {error}"
    logger.warning("EXIF read failed for %s: %s", data.path, error, exc_info=True)
    return replace(data, error=message)


def _tags(image: Image.Image) -> dict[str | int, object]:
    exif = image.getexif()
    values = dict(exif.items())
    values.update(exif.get_ifd(ExifTags.IFD.Exif))
    return {
        ExifTags.TAGS.get(key, key): value for key, value in values.items()
    }


def _with_pillow_metadata(data: ExifData, image: Image.Image) -> ExifData:
    tags = _tags(image)
    width, height = image.size
    return replace(
        data,
        image_width=width,
        image_height=height,
        camera_make=str(tags.get("Make", "")).strip(),
        camera_model=str(tags.get("Model", "")).strip(),
        lens_model=str(tags.get("LensModel", "")).strip(),
        aperture=_fmt_aperture(tags.get("FNumber", tags.get("ApertureValue"))),
        shutter_speed=_fmt_shutter(tags.get("ExposureTime")),
        iso=_fmt_iso(tags.get("ISOSpeedRatings", tags.get("ISO"))),
        focal_length=_fmt_focal(tags.get("FocalLength")),
        date_taken=str(
            tags.get("DateTimeOriginal", tags.get("DateTime", ""))
        ).strip(),
        flash=_fmt_flash(tags.get("Flash", 0)),
    )


def _read_raw_exif(data: ExifData) -> ExifData:
    try:
        import rawpy

        with rawpy.imread(data.path) as raw:
            sizes = raw.sizes
            result = replace(
                data,
                image_width=int(getattr(sizes, "width", 0) or 0),
                image_height=int(getattr(sizes, "height", 0) or 0),
            )
            try:
                thumb = raw.extract_thumb()
                if thumb is None or not hasattr(thumb, "data"):
                    return result
                with Image.open(BytesIO(thumb.data)) as preview:
                    preview_data = _with_pillow_metadata(result, preview)
                    return replace(
                        preview_data,
                        image_width=result.image_width or preview_data.image_width,
                        image_height=result.image_height or preview_data.image_height,
                    )
            except Exception as error:
                logger.debug(
                    "Embedded RAW EXIF unavailable for %s: %s",
                    data.path,
                    error,
                    exc_info=True,
                )
                return result
    except Exception as error:
        return _failed(data, error)


def read_exif(path: str) -> ExifData:
    data = ExifData(path=path)
    if Path(path).suffix.lower() in RAW_EXTENSIONS:
        return _read_raw_exif(data)
    try:
        with Image.open(path) as image:
            return _with_pillow_metadata(data, image)
    except Exception as error:
        return _failed(data, error)
