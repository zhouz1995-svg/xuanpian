from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.thumbnail_card import ThumbnailCard

import pytest


def test_failed_stale_values_do_not_produce_analysis_badge(qapp) -> None:
    model = ImageFileModel(
        "/fake/stale.jpg",
        technical_status=AnalysisStatus.FAILED,
        focus_conclusion="明显失焦",
    )

    assert ThumbnailCard(model, 0, 180).analysis_badge() is None


def test_user_mark_takes_precedence_over_analysis(qapp) -> None:
    model = ImageFileModel("/fake/pick.jpg", mark="入选")

    assert ThumbnailCard(model, 0, 180).analysis_badge().text == "✓"


def test_card_exif_summary_uses_em_dash_for_missing_values(qapp) -> None:
    model = ImageFileModel("/fake/plain.jpg", iso="400", aperture="f/2.8")

    assert ThumbnailCard(model, 0, 180).exif_summary() == "ISO 400  f/2.8"
    assert (
        ThumbnailCard(ImageFileModel("/fake/none.jpg"), 0, 180).exif_summary()
        == "—"
    )


@pytest.mark.parametrize(
    ("mark", "symbol"),
    (("入选", "✓"), ("保留", "✓"), ("淘汰", "✗"), ("舍弃", "✗")),
)
def test_card_badges_use_semantic_managed_marks(
    qapp,
    mark: str,
    symbol: str,
) -> None:
    card = ThumbnailCard(ImageFileModel(f"/fake/{mark}.jpg", mark=mark), 0, 180)

    badge = card.analysis_badge()

    assert badge is not None
    assert badge.text == symbol
