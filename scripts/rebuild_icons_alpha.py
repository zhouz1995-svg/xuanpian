"""Rebuild app icon assets with transparent rounded corners.

The original icon assets were flattened with opaque black corners. This
script derives the alpha channel from the high-resolution design: the
pure-black corner regions (outside the rounded rect) become transparent,
the design stays opaque, and the rounded edge gets a ~1px feather. Every
smaller size reuses the same master mask via downscaling so downsampled
corner pixels never stay opaque.

Usage: python scripts/rebuild_icons_alpha.py
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent

_PNG_PATHS = [
    ROOT / "src/xuanpian/resources/icons/app-icon.png",
    ROOT / "assets/icons/app-icon-1024.png",
    *sorted((ROOT / "assets/icons/png").glob("app-icon-*.png")),
    *sorted((ROOT / "assets/icons/AppIcon.iconset").glob("icon_*.png")),
]

_BLACK_MAX = 8  # max(r, g, b) below this counts as the corner region


def _master_alpha() -> Image.Image:
    """Rounded-rect alpha mask derived from the 1024px design."""
    image = Image.open(ROOT / "assets/icons/app-icon-1024.png")
    if image.mode != "RGB":
        image = image.convert("RGB")
    rgb = np.asarray(image).astype(np.uint16)
    mask = (rgb.max(axis=2) > _BLACK_MAX).astype(np.uint8) * 255
    return Image.fromarray(mask, "L").filter(ImageFilter.GaussianBlur(1.2))


def fix_alpha(path: Path, master_alpha: Image.Image) -> None:
    image = Image.open(path)
    if image.mode != "RGB":
        image = image.convert("RGB")
    rgb = np.asarray(image).astype(np.uint16)
    size = rgb.shape[1], rgb.shape[0]
    if size == master_alpha.size:
        alpha = np.asarray(master_alpha).astype(np.uint8)
    else:
        scaled = master_alpha.resize(size, Image.Resampling.BILINEAR)
        alpha = np.asarray(scaled).astype(np.uint8)
    rgba = np.dstack([rgb.astype(np.uint8), alpha])
    Image.fromarray(rgba, "RGBA").save(path)


def rebuild_ico() -> None:
    source = Image.open(ROOT / "assets/icons/png/app-icon-256.png")
    source = source.convert("RGBA")
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    source.save(
        ROOT / "assets/icons/app-icon.ico",
        format="ICO",
        sizes=sizes,
    )


_ICNS_ENTRIES = [
    (b"icp4", "icon_16x16.png"),      # 16x16
    (b"icp5", "icon_32x32.png"),      # 32x32
    (b"icp6", "icon_32x32@2x.png"),   # 64x64
    (b"ic07", "icon_128x128.png"),    # 128x128
    (b"ic08", "icon_256x256.png"),    # 256x256
    (b"ic09", "icon_512x512.png"),    # 512x512
    (b"ic10", "icon_512x512@2x.png"), # 1024x1024
    (b"ic11", "icon_16x16@2x.png"),   # 32x32 (retina 16)
    (b"ic12", "icon_32x32@2x.png"),   # 64x64 (retina 32)
    (b"ic13", "icon_128x128@2x.png"), # 256x256 (retina 128)
    (b"ic14", "icon_256x256@2x.png"), # 512x512 (retina 256)
]


def build_icns(iconset: Path, output: Path) -> None:
    body = bytearray()
    toc: list[tuple[bytes, int]] = []
    for chunk_type, file_name in _ICNS_ENTRIES:
        path = iconset / file_name
        if not path.exists():
            continue
        payload = path.read_bytes()
        size = len(payload) + 8
        body += chunk_type + struct.pack(">I", size) + payload
        toc.append((chunk_type, size))
    toc_size = 8 + 8 * (len(toc) + 1)
    toc_payload = b"".join(
        chunk_type + struct.pack(">I", size)
        for chunk_type, size in [(b"TOC ", toc_size)] + toc
    )
    total = 8 + toc_size + len(body)
    output.write_bytes(
        b"icns" + struct.pack(">I", total) + b"TOC "
        + struct.pack(">I", toc_size) + toc_payload + body
    )


def main() -> int:
    master_alpha = _master_alpha()
    for path in _PNG_PATHS:
        if path.exists():
            fix_alpha(path, master_alpha)
            print(f"fixed {path.relative_to(ROOT)}")
    rebuild_ico()
    print("rebuilt assets/icons/app-icon.ico")
    build_icns(
        ROOT / "assets/icons/AppIcon.iconset",
        ROOT / "assets/icons/app-icon.icns",
    )
    print("rebuilt assets/icons/app-icon.icns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
