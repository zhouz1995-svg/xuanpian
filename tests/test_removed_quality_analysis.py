from __future__ import annotations

from src.xuanpian.ui.thumbnail_browser import GROUPS


def test_gallery_has_no_composition_or_quality_group() -> None:
    assert "excellent" not in GROUPS
    assert all("构图" not in label and "优秀" not in label for label in GROUPS.values())
