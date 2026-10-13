"""Behavior tests for ProjectStore's recoverable move journal."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import Sequence

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.domain.photo_move import MoveItem, PhotoMoveTransaction
from src.xuanpian.store.project_store import ProjectStore


def _asset(
    kind: AssetKind,
    path: Path,
    *,
    size: int,
    mtime_ns: int,
    origin_path: Path | None = None,
) -> PhotoAssetRecord:
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=FileFingerprint(
            path=str(path.absolute()),
            size=size,
            mtime_ns=mtime_ns,
        ),
        origin_path=str((origin_path or path).absolute()),
    )


def _pair(photo_id: str, directory: Path) -> PhotoRecord:
    return PhotoRecord(
        id=photo_id,
        filename=f"{photo_id}.jpg",
        mark=PhotoMark.UNPROCESSED.value,
        jpeg=_asset(
            AssetKind.JPEG,
            directory / f"{photo_id}.jpg",
            size=101,
            mtime_ns=1001,
        ),
        raw=_asset(
            AssetKind.RAW,
            directory / f"{photo_id}.cr3",
            size=202,
            mtime_ns=2002,
        ),
    )


def _transaction(
    photo: PhotoRecord,
    after_mark: PhotoMark = PhotoMark.KEEP,
    *,
    transaction_id: str | None = None,
    created_at_ns: int = 10,
) -> PhotoMoveTransaction:
    return PhotoMoveTransaction(
        id=transaction_id or f"move-{photo.id}",
        photo_id=photo.id,
        before_mark=PhotoMark(photo.mark),
        after_mark=after_mark,
        created_at_ns=created_at_ns,
        items=tuple(
            MoveItem(
                ordinal=ordinal,
                kind=asset.kind,
                source_path=asset.fingerprint.path,
                target_path=str(
                    (
                        Path(asset.origin_path).parent
                        / after_mark.value
                        / Path(asset.fingerprint.path).name
                    ).absolute()
                ),
                size=asset.fingerprint.size,
                mtime_ns=asset.fingerprint.mtime_ns,
            )
            for ordinal, asset in enumerate(photo.source_assets)
        ),
    )


def _moved_assets(transaction: PhotoMoveTransaction) -> tuple[PhotoAssetRecord, ...]:
    origins = {item.kind: item.source_path for item in transaction.items}
    return tuple(
        PhotoAssetRecord(
            kind=item.kind,
            fingerprint=FileFingerprint(
                path=item.target_path,
                size=item.size,
                mtime_ns=item.mtime_ns,
            ),
            origin_path=origins[item.kind],
        )
        for item in transaction.items
    )


@pytest.fixture
def store(tmp_path: Path):
    opened = ProjectStore.open(tmp_path / "project.sqlite3")
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def pair(store: ProjectStore, tmp_path: Path) -> PhotoRecord:
    photo = _pair("pair", tmp_path)
    store.upsert_photos((photo,))
    return photo


def test_begin_and_list_round_trip_ordered_transactions_and_completion(
    store: ProjectStore,
    tmp_path: Path,
) -> None:
    later_photo = _pair("later", tmp_path)
    earlier_photo = _pair("earlier", tmp_path)
    store.upsert_photos((later_photo, earlier_photo))
    later = _transaction(later_photo, transaction_id="z", created_at_ns=20)
    earlier = _transaction(earlier_photo, transaction_id="a", created_at_ns=10)

    store.begin_photo_move(later)
    store.begin_photo_move(earlier)
    store.mark_move_item_completed(later.id, 1)

    pending = store.list_pending_photo_moves()
    assert tuple(item.transaction for item in pending) == (earlier, later)
    assert tuple(item.completed_ordinals for item in pending) == (
        frozenset(),
        frozenset({1}),
    )
    with pytest.raises(FrozenInstanceError):
        pending[0].transaction = later  # type: ignore[misc]


def test_begin_rejects_second_pending_move_for_same_photo_without_partial_rows(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    first = _transaction(pair, transaction_id="first")
    second = _transaction(pair, PhotoMark.HOLD, transaction_id="second")
    store.begin_photo_move(first)

    with pytest.raises(RuntimeError, match="pending"):
        store.begin_photo_move(second)

    assert tuple(item.transaction for item in store.list_pending_photo_moves()) == (
        first,
    )


def test_begin_rejects_duplicate_transaction_id_without_partial_rows(
    store: ProjectStore,
    tmp_path: Path,
) -> None:
    one = _pair("one", tmp_path)
    two = _pair("two", tmp_path)
    store.upsert_photos((one, two))
    first = _transaction(one, transaction_id="same")
    duplicate = _transaction(two, transaction_id="same")
    store.begin_photo_move(first)

    with pytest.raises(RuntimeError, match="already exists"):
        store.begin_photo_move(duplicate)

    assert tuple(item.transaction for item in store.list_pending_photo_moves()) == (
        first,
    )


def test_begin_rejects_a_pending_row_for_the_photo_even_if_project_is_corrupt(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    connection = store._connection  # type: ignore[attr-defined]
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES ('foreign', '/foreign')"
    )
    connection.execute(
        """
        INSERT INTO photo_move_transactions (
            id, project_id, photo_id, before_mark, after_mark, created_at_ns
        ) VALUES ('foreign-move', 'foreign', ?, ?, ?, 1)
        """,
        (pair.id, PhotoMark.UNPROCESSED.value, PhotoMark.KEEP.value),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="pending"):
        store.begin_photo_move(_transaction(pair))

    assert store.list_pending_photo_moves() == ()


def test_begin_rejects_missing_photo_without_journal_rows(
    store: ProjectStore,
    tmp_path: Path,
) -> None:
    transaction = _transaction(_pair("missing", tmp_path))

    with pytest.raises(KeyError, match="missing"):
        store.begin_photo_move(transaction)

    assert store.list_pending_photo_moves() == ()


def test_begin_rejects_unmanaged_or_stale_before_mark_without_journal_rows(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.save_mark(pair.id, "historical-unknown")

    with pytest.raises(RuntimeError, match="not managed"):
        store.begin_photo_move(transaction)
    assert store.list_pending_photo_moves() == ()

    store.save_mark(pair.id, PhotoMark.HOLD.value)
    with pytest.raises(RuntimeError, match="before mark"):
        store.begin_photo_move(transaction)
    assert store.list_pending_photo_moves() == ()


@pytest.mark.parametrize(
    "changed_item",
    (
        lambda item: replace(item, source_path=item.target_path),
        lambda item: replace(item, size=item.size + 1),
        lambda item: replace(item, mtime_ns=item.mtime_ns + 1),
        lambda item: replace(item, kind=AssetKind.RAW),
    ),
)
def test_begin_rejects_asset_mismatch_without_journal_rows(
    store: ProjectStore,
    pair: PhotoRecord,
    changed_item,
) -> None:
    transaction = _transaction(pair)
    transaction = replace(
        transaction,
        items=(changed_item(transaction.items[0]), transaction.items[1]),
    )

    with pytest.raises(RuntimeError, match="assets"):
        store.begin_photo_move(transaction)

    assert store.list_pending_photo_moves() == ()


def test_begin_rolls_back_transaction_row_when_item_insert_fails(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    store._connection.execute(  # type: ignore[attr-defined]
        """
        CREATE TRIGGER fail_second_move_item
        BEFORE INSERT ON photo_move_items
        WHEN NEW.ordinal = 1
        BEGIN
            SELECT RAISE(ABORT, 'injected item failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected item failure"):
        store.begin_photo_move(_transaction(pair))

    assert store.list_pending_photo_moves() == ()


def test_mark_completed_is_idempotent_and_rejects_unknown_item(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)

    store.mark_move_item_completed(transaction.id, 0)
    store.mark_move_item_completed(transaction.id, 0)
    assert store.list_pending_photo_moves()[0].completed_ordinals == frozenset({0})

    with pytest.raises(KeyError):
        store.mark_move_item_completed(transaction.id, 9)
    with pytest.raises(KeyError):
        store.mark_move_item_completed("missing", 0)


@pytest.mark.parametrize("ordinal", (False, "0", -1))
def test_mark_completed_rejects_non_integer_or_negative_ordinals(
    store: ProjectStore,
    pair: PhotoRecord,
    ordinal: object,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)

    with pytest.raises(ValueError, match="non-negative integer"):
        store.mark_move_item_completed(transaction.id, ordinal)  # type: ignore[arg-type]

    assert store.list_pending_photo_moves()[0].completed_ordinals == frozenset()


def test_commit_move_updates_pair_paths_mark_and_clears_journal(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    store.mark_move_item_completed(transaction.id, 0)
    store.mark_move_item_completed(transaction.id, 1)

    store.commit_photo_move(transaction.id, _moved_assets(transaction))

    photo = store.get_photo(pair.id)
    assert photo is not None
    assert photo.id == pair.id
    assert photo.mark == PhotoMark.KEEP.value
    assert {asset.fingerprint.path for asset in photo.source_assets} == {
        item.target_path for item in transaction.items
    }
    assert {asset.origin_path for asset in photo.source_assets} == {
        item.source_path for item in transaction.items
    }
    assert store.list_pending_photo_moves() == ()


def test_commit_accepts_a_known_legacy_before_mark_and_canonicalizes_it(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    store.save_mark(pair.id, "")
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)

    store.commit_photo_move(transaction.id, _moved_assets(transaction))

    photo = store.get_photo(pair.id)
    assert photo is not None
    assert photo.mark == PhotoMark.KEEP.value


def test_commit_preserves_origins_that_already_differ_from_current_paths(
    store: ProjectStore,
    tmp_path: Path,
) -> None:
    current = _pair("moved-before", tmp_path / "camera" / "待定")
    photo = replace(
        current,
        mark="待定",
        jpeg=replace(
            current.jpeg,
            origin_path=str((tmp_path / "camera" / "moved-before.jpg").absolute()),
        ),
        raw=replace(
            current.raw,
            fingerprint=replace(current.raw.fingerprint, path=str(tmp_path / "cards" / "待定" / "moved-before.cr3")),
            origin_path=str((tmp_path / "cards" / "moved-before.cr3").absolute()),
        ),
    )
    store.upsert_photos((photo,))
    transaction = _transaction(photo)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)
    origins = {asset.kind: asset.origin_path for asset in photo.source_assets}
    committed = tuple(
        replace(asset, origin_path=origins[asset.kind])
        for asset in _moved_assets(transaction)
    )

    store.commit_photo_move(transaction.id, committed)

    persisted = store.get_photo(photo.id)
    assert persisted is not None
    assert {
        asset.kind: asset.origin_path for asset in persisted.source_assets
    } == origins


def test_commit_rejects_incomplete_journal_without_changing_photo(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)

    with pytest.raises(RuntimeError, match="incomplete"):
        store.commit_photo_move(transaction.id, _moved_assets(transaction))

    assert store.get_photo(pair.id) == pair
    assert store.list_pending_photo_moves()[0].transaction == transaction


@pytest.mark.parametrize(
    "change",
    (
        lambda assets: assets[:-1],
        lambda assets: (
            replace(
                assets[0],
                fingerprint=replace(
                    assets[0].fingerprint,
                    path=assets[0].origin_path,
                ),
            ),
            assets[1],
        ),
        lambda assets: (
            replace(
                assets[0],
                fingerprint=replace(
                    assets[0].fingerprint,
                    size=assets[0].fingerprint.size + 1,
                ),
            ),
            assets[1],
        ),
        lambda assets: (
            replace(
                assets[0],
                fingerprint=replace(
                    assets[0].fingerprint,
                    mtime_ns=assets[0].fingerprint.mtime_ns + 1,
                ),
            ),
            assets[1],
        ),
        lambda assets: (
            replace(assets[0], origin_path=assets[0].fingerprint.path),
            assets[1],
        ),
        lambda assets: (replace(assets[0], kind=AssetKind.RAW), assets[1]),
    ),
)
def test_commit_rejects_assets_not_exactly_matching_the_journal(
    store: ProjectStore,
    pair: PhotoRecord,
    change,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)

    with pytest.raises(RuntimeError, match="assets"):
        store.commit_photo_move(transaction.id, change(_moved_assets(transaction)))

    assert store.get_photo(pair.id) == pair
    assert store.list_pending_photo_moves()[0].transaction == transaction


def test_commit_snapshots_stateful_asset_sequence_before_validation(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)
    expected = _moved_assets(transaction)
    changed = (
        replace(
            expected[0],
            fingerprint=replace(
                expected[0].fingerprint,
                path=expected[0].origin_path,
            ),
        ),
        expected[1],
    )

    class StatefulAssets(Sequence[PhotoAssetRecord]):
        def __init__(self) -> None:
            self._iterations = 0

        def __len__(self) -> int:
            return len(expected)

        def __getitem__(self, index: int) -> PhotoAssetRecord:
            if index == 0:
                self._iterations += 1
            values = expected if self._iterations < 3 else changed
            return values[index]

    store.commit_photo_move(transaction.id, StatefulAssets())

    photo = store.get_photo(pair.id)
    assert photo is not None
    assert photo.source_assets == expected


def test_commit_rejects_changed_source_state_or_mark(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)
    store._connection.execute(  # type: ignore[attr-defined]
        "UPDATE photo_assets SET size = size + 1 WHERE photo_id = ? AND kind = ?",
        (pair.id, AssetKind.JPEG.value),
    )
    store._connection.commit()  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="source assets"):
        store.commit_photo_move(transaction.id, _moved_assets(transaction))

    store._connection.execute(  # type: ignore[attr-defined]
        "UPDATE photo_assets SET size = size - 1 WHERE photo_id = ? AND kind = ?",
        (pair.id, AssetKind.JPEG.value),
    )
    store._connection.commit()  # type: ignore[attr-defined]
    store.save_mark(pair.id, PhotoMark.HOLD.value)
    with pytest.raises(RuntimeError, match="before mark"):
        store.commit_photo_move(transaction.id, _moved_assets(transaction))


@pytest.mark.parametrize("reader", ("list", "commit"))
@pytest.mark.parametrize(
    "corruption",
    (
        "source_path",
        "target_path",
        "duplicate_kind",
        "ordinal_real",
        "ordinal_text",
        "ordinal_gap",
        "size_mismatch",
        "size_real",
        "size_text",
        "mtime_mismatch",
        "mtime_real",
        "mtime_text",
        "created_real",
        "created_text",
        "completed_real",
        "completed_text",
        "origin_path",
        "before_mark",
    ),
)
def test_pending_readers_fail_closed_without_repairing_corrupt_journal(
    store: ProjectStore,
    pair: PhotoRecord,
    reader: str,
    corruption: str,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)
    connection = store._connection  # type: ignore[attr-defined]
    connection.execute("PRAGMA ignore_check_constraints = ON")
    if corruption == "source_path":
        connection.execute(
            "UPDATE photo_move_items SET source_path = '/tampered.jpg' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "target_path":
        connection.execute(
            "UPDATE photo_move_items SET target_path = 'relative' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "duplicate_kind":
        connection.execute(
            "UPDATE photo_move_items SET kind = 'raw' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "ordinal_real":
        connection.execute(
            "UPDATE photo_move_items SET ordinal = 0.5 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "ordinal_text":
        connection.execute(
            "UPDATE photo_move_items SET ordinal = 'bad' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "ordinal_gap":
        connection.execute(
            "UPDATE photo_move_items SET ordinal = 2 "
            "WHERE transaction_id = ? AND ordinal = 1",
            (transaction.id,),
        )
    elif corruption == "size_mismatch":
        connection.execute(
            "UPDATE photo_move_items SET size = size + 1 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "size_real":
        connection.execute(
            "UPDATE photo_move_items SET size = size + 0.5 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "size_text":
        connection.execute(
            "UPDATE photo_move_items SET size = 'bad' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "mtime_mismatch":
        connection.execute(
            "UPDATE photo_move_items SET mtime_ns = mtime_ns + 1 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "mtime_real":
        connection.execute(
            "UPDATE photo_move_items SET mtime_ns = mtime_ns + 0.5 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "mtime_text":
        connection.execute(
            "UPDATE photo_move_items SET mtime_ns = 'bad' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "created_real":
        connection.execute(
            "UPDATE photo_move_transactions SET created_at_ns = 10.5 "
            "WHERE id = ?",
            (transaction.id,),
        )
    elif corruption == "created_text":
        connection.execute(
            "UPDATE photo_move_transactions SET created_at_ns = 'bad' "
            "WHERE id = ?",
            (transaction.id,),
        )
    elif corruption == "completed_real":
        connection.execute(
            "UPDATE photo_move_items SET completed = 0.5 "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "completed_text":
        connection.execute(
            "UPDATE photo_move_items SET completed = 'bad' "
            "WHERE transaction_id = ? AND ordinal = 0",
            (transaction.id,),
        )
    elif corruption == "origin_path":
        connection.execute(
            "UPDATE photo_assets SET origin_path = 'relative' "
            "WHERE photo_id = ? AND kind = 'jpeg'",
            (pair.id,),
        )
    elif corruption == "before_mark":
        connection.execute(
            "UPDATE photos SET mark = ? WHERE id = ?",
            (PhotoMark.HOLD.value, pair.id),
        )
    else:  # pragma: no cover - parametrization is closed above
        raise AssertionError(corruption)
    connection.commit()
    transaction_before = connection.execute(
        "SELECT * FROM photo_move_transactions WHERE id = ?",
        (transaction.id,),
    ).fetchall()
    items_before = connection.execute(
        "SELECT * FROM photo_move_items WHERE transaction_id = ? ORDER BY ordinal",
        (transaction.id,),
    ).fetchall()
    photo_before = connection.execute(
        "SELECT * FROM photos WHERE id = ?",
        (pair.id,),
    ).fetchall()
    assets_before = connection.execute(
        "SELECT * FROM photo_assets WHERE photo_id = ? ORDER BY kind",
        (pair.id,),
    ).fetchall()

    with pytest.raises(
        RuntimeError,
        match="invalid persisted|source assets|before mark|ownership",
    ):
        if reader == "list":
            store.list_pending_photo_moves()
        else:
            store.commit_photo_move(
                transaction.id,
                _moved_assets(transaction),
            )

    assert connection.execute(
        "SELECT * FROM photo_move_transactions WHERE id = ?",
        (transaction.id,),
    ).fetchall() == transaction_before
    assert connection.execute(
        "SELECT * FROM photo_move_items WHERE transaction_id = ? ORDER BY ordinal",
        (transaction.id,),
    ).fetchall() == items_before
    assert connection.execute(
        "SELECT * FROM photos WHERE id = ?",
        (pair.id,),
    ).fetchall() == photo_before
    assert connection.execute(
        "SELECT * FROM photo_assets WHERE photo_id = ? ORDER BY kind",
        (pair.id,),
    ).fetchall() == assets_before


def test_commit_rolls_back_assets_mark_and_journal_on_sqlite_failure(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    for item in transaction.items:
        store.mark_move_item_completed(transaction.id, item.ordinal)
    store._connection.execute(  # type: ignore[attr-defined]
        """
        CREATE TRIGGER fail_move_commit
        BEFORE DELETE ON photo_move_transactions
        BEGIN
            SELECT RAISE(ABORT, 'injected commit failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected commit failure"):
        store.commit_photo_move(transaction.id, _moved_assets(transaction))

    assert store.get_photo(pair.id) == pair
    assert store.list_pending_photo_moves()[0].transaction == transaction


def test_abort_clears_only_owned_journal_and_leaves_photo_unchanged(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    store.begin_photo_move(transaction)
    store.mark_move_item_completed(transaction.id, 0)

    store.abort_photo_move(transaction.id)

    assert store.get_photo(pair.id) == pair
    assert store.list_pending_photo_moves() == ()
    with pytest.raises(KeyError):
        store.abort_photo_move(transaction.id)


def test_move_apis_never_access_another_projects_transaction(
    store: ProjectStore,
    tmp_path: Path,
) -> None:
    foreign_photo = _pair("foreign-photo", tmp_path)
    foreign = _transaction(foreign_photo, transaction_id="foreign-move")
    connection = store._connection  # type: ignore[attr-defined]
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES ('foreign', '/foreign')"
    )
    connection.execute(
        "INSERT INTO photos (id, project_id, filename, mark) VALUES (?, ?, ?, ?)",
        (
            foreign_photo.id,
            "foreign",
            foreign_photo.filename,
            foreign_photo.mark,
        ),
    )
    connection.execute(
        """
        INSERT INTO photo_move_transactions (
            id, project_id, photo_id, before_mark, after_mark, created_at_ns
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            foreign.id,
            "foreign",
            foreign.photo_id,
            foreign.before_mark.value,
            foreign.after_mark.value,
            foreign.created_at_ns,
        ),
    )
    for item in foreign.items:
        connection.execute(
            """
            INSERT INTO photo_move_items (
                transaction_id, ordinal, kind, source_path, target_path,
                size, mtime_ns, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                foreign.id,
                item.ordinal,
                item.kind.value,
                item.source_path,
                item.target_path,
                item.size,
                item.mtime_ns,
            ),
        )
    connection.execute(
        "UPDATE photo_move_transactions SET project_id = ? WHERE id = ?",
        ("project", foreign.id),
    )
    connection.commit()

    with pytest.raises(RuntimeError, match="ownership"):
        store.list_pending_photo_moves()
    with pytest.raises(KeyError):
        store.mark_move_item_completed(foreign.id, 0)
    with pytest.raises(RuntimeError, match="ownership"):
        store.commit_photo_move(foreign.id, _moved_assets(foreign))
    with pytest.raises(KeyError):
        store.abort_photo_move(foreign.id)
    assert connection.execute(
        "SELECT completed FROM photo_move_items WHERE transaction_id = ?",
        (foreign.id,),
    ).fetchall() == [(0,), (0,)]


def test_move_and_setting_apis_enforce_owner_thread_and_open_state(
    store: ProjectStore,
    pair: PhotoRecord,
) -> None:
    transaction = _transaction(pair)
    calls = (
        lambda: store.begin_photo_move(transaction),
        lambda: store.mark_move_item_completed(transaction.id, 0),
        lambda: store.commit_photo_move(transaction.id, _moved_assets(transaction)),
        lambda: store.abort_photo_move(transaction.id),
        store.list_pending_photo_moves,
        store.culling_notice_confirmed,
        lambda: store.set_culling_notice_confirmed(True),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        errors = [executor.submit(call).exception() for call in calls]
    assert all(isinstance(error, RuntimeError) for error in errors)
    assert all("owning thread" in str(error) for error in errors)

    store.close()
    for call in calls:
        with pytest.raises(RuntimeError, match="closed"):
            call()


def test_culling_notice_setting_defaults_false_and_round_trips_strict_booleans(
    store: ProjectStore,
) -> None:
    assert store.culling_notice_confirmed() is False

    store.set_culling_notice_confirmed(True)
    assert store.culling_notice_confirmed() is True
    assert store._connection.execute(  # type: ignore[attr-defined]
        "SELECT value FROM project_settings WHERE project_id = ? AND key = ?",
        ("project", "culling_notice_confirmed"),
    ).fetchone() == ("true",)

    store.set_culling_notice_confirmed(False)
    assert store.culling_notice_confirmed() is False
    with pytest.raises(ValueError, match="boolean"):
        store.set_culling_notice_confirmed(1)  # type: ignore[arg-type]

    store._connection.execute(  # type: ignore[attr-defined]
        "UPDATE project_settings SET value = '1' WHERE key = ?",
        ("culling_notice_confirmed",),
    )
    with pytest.raises(RuntimeError, match="invalid boolean"):
        store.culling_notice_confirmed()
