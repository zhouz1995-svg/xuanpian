from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyleOptionViewItem,
)

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser
from src.xuanpian.ui.thumbnail_card import ThumbnailCard
from src.xuanpian.ui.thumbnail_delegate import ThumbnailDelegate
from src.xuanpian.ui.theme import (
    ACCENT,
    BORDER_STRONG,
    ELEVATED,
    SIDEBAR,
    SUCCESS,
    WARNING,
)


def _models(count: int) -> list[ImageFileModel]:
    return [
        ImageFileModel(
            f"/fake/{row}.jpg",
            photo_id=f"photo-{row}",
        )
        for row in range(count)
    ]


def _paint_index(
    delegate: ThumbnailDelegate,
    browser: ThumbnailBrowser,
    row: int,
    *,
    selected: bool = False,
    hovered: bool = False,
) -> QImage:
    size = delegate.sizeHint(
        QStyleOptionViewItem(),
        browser._photo_list_model.index(row, 0),
    )
    canvas = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    option = QStyleOptionViewItem()
    option.rect = canvas.rect()
    if selected:
        option.state |= QStyle.StateFlag.State_Selected
    if hovered:
        option.state |= QStyle.StateFlag.State_MouseOver
    delegate.paint(painter, option, browser._photo_list_model.index(row, 0))
    painter.end()
    return canvas


def test_browser_uses_virtualized_icon_view_without_photo_widgets(qapp) -> None:
    browser = ThumbnailBrowser()

    browser.set_models(_models(1_000))

    assert isinstance(browser._view, QListView)
    assert browser._view.viewMode() is QListView.ViewMode.IconMode
    assert browser._view.resizeMode() is QListView.ResizeMode.Adjust
    assert browser._view.movement() is QListView.Movement.Static
    assert (
        browser._view.selectionMode()
        is QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert browser._view.uniformItemSizes() is True
    assert browser._photo_list_model.rowCount() == 1_000
    assert browser.findChildren(ThumbnailCard) == []
    assert not hasattr(browser, "_cards")
    assert not hasattr(browser, "_all_models")


def test_delegate_has_no_parallel_models_and_sizes_from_thumbnail_width(qapp) -> None:
    delegate = ThumbnailDelegate()

    assert not hasattr(delegate, "_models")
    assert not hasattr(delegate, "set_models")
    assert delegate.sizeHint(QStyleOptionViewItem(), None) == QSize(150, 148)

    delegate.set_thumbnail_size(240)

    assert delegate.sizeHint(QStyleOptionViewItem(), None) == QSize(240, 216)


def test_selection_and_double_click_emit_stable_photo_ids(
    qapp, monkeypatch
) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models(3))
    changes: list[list[str]] = []
    previews: list[str] = []
    browser.selection_changed.connect(
        lambda: changes.append(browser.selected_photo_ids())
    )

    class MainWindow:
        def enter_preview_photo(self, photo_id: str) -> None:
            previews.append(photo_id)

    monkeypatch.setattr(browser, "_find_main_window", lambda: MainWindow())

    browser.select_photo_id("photo-1")
    browser._view.doubleClicked.emit(browser._photo_list_model.index(2, 0))

    assert changes[-1] == ["photo-1"]
    assert browser.selected_indexes() == [1]
    assert previews == ["photo-2"]


def test_filter_model_reset_preserves_selected_stable_photo_id(qapp) -> None:
    first = ImageFileModel("/fake/first.jpg", photo_id="first")
    survivor = ImageFileModel(
        "/fake/survivor.jpg",
        photo_id="survivor",
        mark="入选",
    )
    last = ImageFileModel(
        "/fake/last.jpg",
        photo_id="last",
        mark="入选",
    )
    browser = ThumbnailBrowser()
    browser.set_models([first, survivor, last])
    browser.select_photo_id("survivor")

    browser.toggle_picks_only()

    assert browser.current_photo_ids() == ["survivor", "last"]
    assert browser.selected_photo_ids() == ["survivor"]
    assert browser.selected_indexes() == [0]


def test_zoom_updates_delegate_and_grid_size_together(qapp) -> None:
    browser = ThumbnailBrowser()

    browser.set_thumbnail_size(220)

    assert browser._delegate.sizeHint(QStyleOptionViewItem(), None) == QSize(
        220,
        201,
    )
    assert browser._view.gridSize() == QSize(220, 201)


def test_grid_cards_fill_viewport_width_when_shown(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models(40))
    browser.resize(1100, 700)
    browser.show()
    qapp.processEvents()

    base = browser._thumb_size
    card_w = browser._delegate.sizeHint(
        QStyleOptionViewItem(),
        None,
    ).width()
    assert card_w >= base
    # Measure the first row: the last column should reach the right edge
    # (apart from the vertical scrollbar strip) instead of a wide empty gap.
    model = browser._photo_list_model
    first_y = browser._view.visualRect(model.index(0, 0)).y()
    last_x = 0
    for row in range(model.rowCount()):
        rect = browser._view.visualRect(model.index(row, 0))
        if row and rect.y() != first_y:
            break
        last_x = rect.x()
    vw = browser._view.viewport().width()
    assert last_x + card_w >= vw - 20
    browser.close()


def test_thumbnail_update_emits_only_thumbnail_role_for_visible_photo(
    qapp,
) -> None:
    browser = ThumbnailBrowser()
    models = _models(2)
    browser.set_models(models)
    changes: list[tuple[int, list[int]]] = []
    browser._photo_list_model.dataChanged.connect(
        lambda top_left, _bottom_right, roles: changes.append(
            (top_left.row(), list(roles))
        )
    )

    browser.update_model_thumbnail(
        models[1],
        QImage(32, 24, QImage.Format.Format_RGB32),
    )

    assert changes == [(1, [int(PhotoRoles.THUMBNAIL)])]


def test_single_analysis_result_updates_1000_model_index_incrementally(
    qapp,
    monkeypatch,
) -> None:
    browser = ThumbnailBrowser()
    models = _models(1_000)
    browser.set_models(models)
    browser._set_group("clear")
    resets = 0
    insertions: list[tuple[int, int]] = []

    def record_reset() -> None:
        nonlocal resets
        resets += 1

    browser._photo_list_model.modelReset.connect(record_reset)
    browser._photo_list_model.rowsInserted.connect(
        lambda _parent, first, last: insertions.append((first, last))
    )
    monkeypatch.setattr(
        browser,
        "_compute_group_counts",
        lambda: (_ for _ in ()).throw(
            AssertionError("single-result path rebuilt all group counts")
        ),
    )
    monkeypatch.setattr(
        browser,
        "_get_filtered",
        lambda: (_ for _ in ()).throw(
            AssertionError("single-result path rebuilt the full filter")
        ),
    )
    monkeypatch.setattr(
        browser,
        "all_models",
        lambda: (_ for _ in ()).throw(
            AssertionError("single-result path scanned every photo")
        ),
    )
    target = models[500]
    target.technical_status = AnalysisStatus.SUCCEEDED
    target.focus_conclusion = "清晰"

    browser.refresh_photo(target.photo_id, [PhotoRoles.ANALYSIS])

    assert browser.current_photo_ids() == [target.photo_id]
    assert insertions == [(0, 0)]
    assert resets == 0
    assert browser._group_counts["clear"] == 1


def test_thousand_analysis_results_use_group_bounded_work(
    qapp,
    monkeypatch,
) -> None:
    browser = ThumbnailBrowser()
    models = _models(1_000)
    browser.set_models(models)
    original_classify = browser.classify_group
    classifications = 0

    def counted_classify(model, group_key):
        nonlocal classifications
        classifications += 1
        return original_classify(model, group_key)

    monkeypatch.setattr(browser, "classify_group", counted_classify)
    for model in models:
        model.technical_status = AnalysisStatus.SUCCEEDED
        model.focus_conclusion = "清晰"
        browser.refresh_photo(model.photo_id, [PhotoRoles.ANALYSIS])

    assert classifications == len(models) * len(browser.compute_group_counts())
    assert browser.compute_group_counts()["clear"] == 1_000


def test_incremental_filter_change_preserves_stable_selection(qapp) -> None:
    browser = ThumbnailBrowser()
    models = _models(4)
    for model in (models[1], models[3]):
        model.technical_status = AnalysisStatus.SUCCEEDED
        model.focus_conclusion = "清晰"
    browser.set_models(models)
    browser._set_group("clear")
    browser.select_photo_id(models[3].photo_id)
    inserted = models[2]
    inserted.technical_status = AnalysisStatus.SUCCEEDED
    inserted.focus_conclusion = "清晰"

    browser.refresh_photo(inserted.photo_id, [PhotoRoles.ANALYSIS])

    assert browser.current_photo_ids() == [
        models[1].photo_id,
        models[2].photo_id,
        models[3].photo_id,
    ]
    assert browser.selected_photo_ids() == [models[3].photo_id]


def test_delegate_paints_role_data_and_loading_placeholder(qapp) -> None:
    loaded = ImageFileModel(
        "/fake/loaded.nef",
        photo_id="loaded",
        raw_path="/fake/loaded.nef",
        mark="入选",
    )
    thumbnail = QPixmap(80, 40)
    thumbnail.fill(QColor("#32aa55"))
    loaded.thumbnail = thumbnail
    loading = ImageFileModel("/fake/loading.jpg", photo_id="loading")
    browser = ThumbnailBrowser()
    browser.set_models([loaded, loading])

    rendered = _paint_index(browser._delegate, browser, 0, selected=True)
    placeholder = _paint_index(browser._delegate, browser, 1)

    assert QColor(rendered.pixel(rendered.width() // 2, 60)) == QColor(
        "#32aa55"
    )
    assert any(
        QColor(rendered.pixel(x, rendered.height() // 2)) == SUCCESS
        for x in range(1, 8)
    )
    assert any(
        QColor(rendered.pixel(2, y)) == ACCENT
        for y in range(8, rendered.height() - 8)
    )
    assert any(
        QColor(placeholder.pixel(x, y)) == SIDEBAR
        for x in range(12, placeholder.width() - 12, 12)
        for y in range(12, placeholder.height() - 45, 12)
    )


def test_delegate_paints_analysis_hover_and_raw_jpeg_role_badges(qapp) -> None:
    raw_pair = ImageFileModel(
        "/fake/pair.jpg",
        photo_id="pair",
        jpeg_path="/fake/pair.jpg",
        raw_path="/fake/pair.nef",
    )
    jpeg = ImageFileModel(
        "/fake/jpeg.jpg",
        photo_id="jpeg",
        technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="清晰",
        exposure_conclusion="正常",
    )
    for model in (raw_pair, jpeg):
        model.thumbnail = QPixmap(80, 40)
        model.thumbnail.fill(QColor("#303030"))
    browser = ThumbnailBrowser()
    browser.set_models([raw_pair, jpeg])

    pair_render = _paint_index(browser._delegate, browser, 0)
    jpeg_render = _paint_index(
        browser._delegate,
        browser,
        1,
        hovered=True,
    )

    assert any(
        QColor(pair_render.pixel(x, y)) == WARNING
        for x in range(7, 70)
        for y in range(3, 16)
    )
    assert any(
        QColor(jpeg_render.pixel(x, y)) == BORDER_STRONG
        for x in range(7, 50)
        for y in range(3, 16)
    )
    assert not any(
        QColor(jpeg_render.pixel(x, y)) == SUCCESS
        for x in range(jpeg_render.width() - 20, jpeg_render.width() - 2)
        for y in range(3, 18)
    )
    assert QColor(jpeg_render.pixel(jpeg_render.width() // 2, 4)) == ELEVATED
