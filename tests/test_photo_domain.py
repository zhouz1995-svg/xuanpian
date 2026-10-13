from __future__ import annotations

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    natural_sort_key,
)
from src.xuanpian.domain.photo_move import MoveItem, PhotoMoveTransaction


def test_natural_sort_orders_numbers_numerically() -> None:
    names = ["IMG_10.jpg", "IMG_2.jpg", "IMG_1.jpg", "IMG_1a.jpg"]
    assert sorted(names, key=natural_sort_key) == [
        "IMG_1.jpg",
        "IMG_1a.jpg",
        "IMG_2.jpg",
        "IMG_10.jpg",
    ]


def test_natural_sort_is_case_insensitive_and_stable_for_equal_keys() -> None:
    names = ["b.jpg", "A.jpg", "a_2.jpg", "a_10.jpg"]
    assert sorted(names, key=natural_sort_key) == [
        "A.jpg",
        "a_2.jpg",
        "a_10.jpg",
        "b.jpg",
    ]


def test_asset_record_requires_an_absolute_immutable_origin_path() -> None:
    asset = PhotoAssetRecord(
        kind=AssetKind.JPEG,
        fingerprint=FileFingerprint("/photos/current.jpg", 10, 20),
        origin_path="/photos/original.jpg",
    )

    assert asset.origin_path == "/photos/original.jpg"
    with pytest.raises(AttributeError):
        asset.origin_path = "/photos/other.jpg"  # type: ignore[misc]


@pytest.mark.parametrize("origin_path", ["relative.jpg", "", None, 1])
def test_asset_record_rejects_non_absolute_or_non_string_origin_path(
    origin_path: object,
) -> None:
    with pytest.raises(ValueError, match="origin_path must be an absolute path"):
        PhotoAssetRecord(
            kind=AssetKind.JPEG,
            fingerprint=FileFingerprint("/photos/current.jpg", 10, 20),
            origin_path=origin_path,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ordinal": -1},
        {"ordinal": True},
        {"source_path": "relative/source.jpg"},
        {"target_path": "relative/target.jpg"},
        {"size": -1},
        {"size": True},
        {"mtime_ns": -1},
        {"mtime_ns": True},
    ],
)
def test_move_item_rejects_invalid_persistence_fields(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "ordinal": 0,
        "kind": AssetKind.JPEG,
        "source_path": "/photos/source.jpg",
        "target_path": "/photos/保留/source.jpg",
        "size": 10,
        "mtime_ns": 20,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        MoveItem(**values)  # type: ignore[arg-type]


def test_move_transaction_requires_ordered_immutable_domain_values() -> None:
    item = MoveItem(
        ordinal=0,
        kind=AssetKind.JPEG,
        source_path="/photos/source.jpg",
        target_path="/photos/保留/source.jpg",
        size=10,
        mtime_ns=20,
    )
    transaction = PhotoMoveTransaction(
        id="move-1",
        photo_id="photo-1",
        before_mark=PhotoMark.UNPROCESSED,
        after_mark=PhotoMark.KEEP,
        created_at_ns=30,
        items=(item,),
    )

    assert transaction.items == (item,)
    with pytest.raises(AttributeError):
        transaction.photo_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"id": ""},
        {"photo_id": ""},
        {"before_mark": "未处理"},
        {"after_mark": "保留"},
        {"created_at_ns": -1},
        {"created_at_ns": True},
        {"items": ()},
        {
            "items": (
                MoveItem(1, AssetKind.JPEG, "/source.jpg", "/target.jpg", 1, 1),
            )
        },
        {"items": []},
    ],
)
def test_move_transaction_rejects_invalid_journal_fields(
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "id": "move-1",
        "photo_id": "photo-1",
        "before_mark": PhotoMark.UNPROCESSED,
        "after_mark": PhotoMark.KEEP,
        "created_at_ns": 30,
        "items": (
            MoveItem(0, AssetKind.JPEG, "/source.jpg", "/target.jpg", 1, 1),
        ),
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        PhotoMoveTransaction(**values)  # type: ignore[arg-type]
