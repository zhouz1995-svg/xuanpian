from __future__ import annotations

import pytest
from PyQt6.QtCore import QModelIndex, Qt

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles


def _models() -> dict[str, ImageFileModel]:
    return {
        "one": ImageFileModel(
            "/photos/one.jpg",
            photo_id="one",
            mark="入选",
        ),
        "two": ImageFileModel(
            "/photos/two.cr2",
            photo_id="two",
            raw_path="/photos/two.cr2",
        ),
        "pair": ImageFileModel(
            "/photos/pair.jpg",
            photo_id="pair",
            jpeg_path="/photos/pair.jpg",
            raw_path="/photos/pair.cr2",
        ),
    }


def test_set_photo_ids_emits_precise_tail_insert_and_remove_ranges(qapp) -> None:
    models = _models()
    model = PhotoListModel(models.get)
    inserted: list[tuple[int, int]] = []
    removed: list[tuple[int, int]] = []
    model.rowsInserted.connect(
        lambda _parent, first, last: inserted.append((first, last))
    )
    model.rowsRemoved.connect(
        lambda _parent, first, last: removed.append((first, last))
    )

    model.set_photo_ids(["one", "two"])
    model.set_photo_ids(["one", "two", "pair"])
    model.set_photo_ids(["one"])

    assert inserted == [(0, 1), (2, 2)]
    assert removed == [(1, 2)]
    assert model.rowCount(QModelIndex()) == 1


@pytest.mark.parametrize("photo_ids", [["one", ""], ["one", "one"]])
def test_set_photo_ids_rejects_empty_or_duplicate_ids(
    qapp, photo_ids: list[str]
) -> None:
    model = PhotoListModel(_models().get)
    model.set_photo_ids(["two"])

    with pytest.raises(ValueError):
        model.set_photo_ids(photo_ids)

    assert model.photo_id_at(0) == "two"


def test_reorder_resets_and_rebuilds_id_row_mapping(qapp) -> None:
    model = PhotoListModel(_models().get)
    reset_count = 0

    def count_reset() -> None:
        nonlocal reset_count
        reset_count += 1

    model.modelReset.connect(count_reset)
    model.set_photo_ids(["one", "two", "pair"])

    model.set_photo_ids(["pair", "one"])

    assert reset_count == 1
    assert [model.photo_id_at(row) for row in range(2)] == ["pair", "one"]
    assert model.photo_id_at(-1) is None
    assert model.photo_id_at(2) is None
    assert model.row_for_id("pair") == 0
    assert model.row_for_id("one") == 1
    assert model.row_for_id("two") == -1


def test_middle_deletions_emit_precise_ranges_without_reset(qapp) -> None:
    models = _models()
    model = PhotoListModel(models.get)
    model.set_photo_ids(["one", "two", "pair"])
    removed: list[tuple[int, int]] = []
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    model.rowsRemoved.connect(
        lambda _parent, first, last: removed.append((first, last))
    )
    model.modelReset.connect(count_reset)

    model.set_photo_ids(["one", "pair"])

    assert removed == [(1, 1)]
    assert resets == 0
    assert model.photo_ids() == ["one", "pair"]


def test_update_photo_only_emits_visible_row_and_exact_roles(qapp) -> None:
    model = PhotoListModel(_models().get)
    model.set_photo_ids(["pair", "one"])
    changes: list[tuple[int, int, list[int]]] = []
    model.dataChanged.connect(
        lambda top_left, bottom_right, roles: changes.append(
            (top_left.row(), bottom_right.row(), list(roles))
        )
    )

    model.update_photo("one", [PhotoRoles.THUMBNAIL, PhotoRoles.MARK])
    model.update_photo("two", [PhotoRoles.MARK])

    assert changes == [
        (
            1,
            1,
            [int(PhotoRoles.THUMBNAIL), int(PhotoRoles.MARK)],
        )
    ]


def test_roles_resolve_existing_provider_model_without_copying_it(qapp) -> None:
    models = _models()
    model = PhotoListModel(models.get)
    model.set_photo_ids(["one", "two", "pair"])

    first = model.index(0, 0)
    raw = model.index(1, 0)
    pair = model.index(2, 0)

    assert model.data(first, PhotoRoles.ID) == "one"
    assert model.data(first, PhotoRoles.PATH) == "/photos/one.jpg"
    assert model.data(first, PhotoRoles.FILENAME) == "one.jpg"
    assert model.data(first, PhotoRoles.THUMBNAIL) is models["one"].thumbnail
    assert model.data(first, PhotoRoles.MARK) == "入选"
    assert model.data(first, PhotoRoles.ANALYSIS) is models["one"]
    assert model.data(raw, PhotoRoles.ASSET_KIND) == "RAW"
    assert model.data(pair, PhotoRoles.ASSET_KIND) == "RAW+JPEG"

    models["one"].mark = "待定"

    assert model.data(first, PhotoRoles.MARK) == "待定"


def test_missing_provider_record_returns_empty_role_values(qapp) -> None:
    model = PhotoListModel(lambda _photo_id: None)
    model.set_photo_ids(["missing"])
    index = model.index(0, 0)

    assert model.data(index, PhotoRoles.ID) == "missing"
    assert model.data(index, PhotoRoles.PATH) == ""
    assert model.data(index, PhotoRoles.THUMBNAIL) is None


def test_role_queries_fail_closed_when_provider_record_disappears(qapp) -> None:
    models = _models()
    model = PhotoListModel(models.__getitem__)
    model.set_photo_ids(["one"])
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "one.jpg"

    del models["one"]

    assert model.data(index, PhotoRoles.ID) == "one"
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(index, PhotoRoles.PATH) == ""
    assert model.data(index, PhotoRoles.THUMBNAIL) is None
    assert model.data(index, PhotoRoles.ANALYSIS) is None


def test_provider_exception_during_role_query_fails_closed(qapp) -> None:
    def unavailable(_photo_id: str) -> ImageFileModel:
        raise RuntimeError("provider temporarily unavailable")

    model = PhotoListModel(unavailable)
    model.set_photo_ids(["one"])
    index = model.index(0, 0)

    assert model.data(index, PhotoRoles.ID) == "one"
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(index, PhotoRoles.THUMBNAIL) is None


@pytest.mark.parametrize(
    "invalid_record",
    [
        object(),
        ImageFileModel("/photos/wrong.jpg", photo_id="wrong"),
    ],
)
def test_malformed_or_mismatched_provider_record_fails_closed(
    qapp,
    invalid_record: object,
) -> None:
    model = PhotoListModel(lambda _photo_id: invalid_record)  # type: ignore[arg-type]
    model.set_photo_ids(["one"])
    index = model.index(0, 0)

    assert model.data(index, PhotoRoles.ID) == "one"
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == ""
    assert model.data(index, PhotoRoles.ANALYSIS) is None
