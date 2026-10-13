"""Crash-recovery tests for logical-photo move transactions.

Every filesystem mutation in this module is confined to pytest's disposable
``tmp_path``.  No user photo or non-temporary path is inspected or moved.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import AssetKind
from src.xuanpian.domain.photo_move import MoveItem, PhotoMoveTransaction
from src.xuanpian.services.photo_move_service import (
    PhotoMoveService,
    RecoveryDecision,
)


def _write_exact(path: Path, contents: bytes, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _transaction(tmp_path: Path) -> PhotoMoveTransaction:
    specifications = (
        (AssetKind.JPEG, "pair.jpg", b"jpeg-contents", 1_700_000_000_000_000_001),
        (AssetKind.RAW, "pair.cr3", b"raw-contents", 1_700_000_000_000_000_002),
    )
    items = []
    for ordinal, (kind, name, contents, mtime_ns) in enumerate(specifications):
        source = tmp_path / name
        target = tmp_path / PhotoMark.KEEP.value / name
        _write_exact(source, contents, mtime_ns)
        items.append(
            MoveItem(
                ordinal=ordinal,
                kind=kind,
                source_path=str(source.absolute()),
                target_path=str(target.absolute()),
                size=len(contents),
                mtime_ns=mtime_ns,
            )
        )
    return PhotoMoveTransaction(
        id="pending-move",
        photo_id="stable-photo-id",
        before_mark=PhotoMark.UNPROCESSED,
        after_mark=PhotoMark.KEEP,
        created_at_ns=123,
        items=tuple(items),
    )


def _complete_atomic_move(item: MoveItem) -> None:
    target = Path(item.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    PhotoMoveService()._move_no_clobber(
        Path(item.source_path),
        target,
        item,
    )


@pytest.mark.parametrize(
    ("crash_point", "moved", "completed", "expected"),
    (
        ("before_move", (), (), RecoveryDecision.ABORTED),
        (
            "after_first_atomic_rename_before_callback",
            (0,),
            (),
            RecoveryDecision.ROLLED_BACK,
        ),
        (
            "after_first_callback",
            (0,),
            (0,),
            RecoveryDecision.ROLLED_BACK,
        ),
        (
            "all_atomic_renames_before_callbacks",
            (0, 1),
            (),
            RecoveryDecision.READY_TO_COMMIT,
        ),
        (
            "after_all_callbacks_before_commit",
            (0, 1),
            (0, 1),
            RecoveryDecision.READY_TO_COMMIT,
        ),
    ),
)
def test_recovery_uses_filesystem_truth_at_every_precommit_crash_point(
    tmp_path: Path,
    crash_point: str,
    moved: tuple[int, ...],
    completed: tuple[int, ...],
    expected: RecoveryDecision,
) -> None:
    transaction = _transaction(tmp_path)
    for ordinal in moved:
        _complete_atomic_move(transaction.items[ordinal])

    result = PhotoMoveService().recover_pending_move(
        transaction,
        completed_ordinals=frozenset(completed),
    )

    assert result.decision is expected, crash_point
    assert result.journal_completed_ordinals == frozenset(completed)
    if expected in (RecoveryDecision.ABORTED, RecoveryDecision.ROLLED_BACK):
        assert all(Path(item.source_path).is_file() for item in transaction.items)
        assert not any(
            os.path.lexists(item.target_path) for item in transaction.items
        )
    else:
        assert result.observed_completed_ordinals == frozenset({0, 1})
        assert tuple(asset.fingerprint.path for asset in result.moved_assets) == (
            transaction.items[0].target_path,
            transaction.items[1].target_path,
        )
        assert tuple(asset.origin_path for asset in result.moved_assets) == (
            transaction.items[0].source_path,
            transaction.items[1].source_path,
        )


def test_zero_byte_target_without_persisted_ownership_is_preserved(
    tmp_path: Path,
) -> None:
    transaction = _transaction(tmp_path)
    foreign_zero = Path(transaction.items[0].target_path)
    foreign_zero.parent.mkdir()
    foreign_zero.touch()

    result = PhotoMoveService().recover_pending_move(transaction)

    assert result.decision is RecoveryDecision.NEEDS_USER
    assert result.unresolved_ordinals == (0,)
    assert set(result.recovery_paths) == {
        Path(transaction.items[0].source_path),
        foreign_zero,
    }
    assert foreign_zero.is_file()
    assert foreign_zero.stat().st_size == 0
    assert Path(transaction.items[0].source_path).read_bytes() == b"jpeg-contents"


def test_mixed_zero_target_and_conflict_reports_every_unresolved_path(
    tmp_path: Path,
) -> None:
    transaction = _transaction(tmp_path)
    zero_target = Path(transaction.items[0].target_path)
    zero_target.parent.mkdir()
    zero_target.touch()
    conflict = transaction.items[1]
    conflict_source = Path(conflict.source_path)
    _write_exact(
        Path(conflict.target_path),
        conflict_source.read_bytes(),
        conflict.mtime_ns,
    )

    result = PhotoMoveService().recover_pending_move(transaction)

    assert result.decision is RecoveryDecision.NEEDS_USER
    assert result.unresolved_ordinals == (0, 1)
    assert set(result.recovery_paths) == {
        path
        for item in transaction.items
        for path in (Path(item.source_path), Path(item.target_path))
    }
    assert zero_target.is_file()
    assert zero_target.stat().st_size == 0


def test_zero_size_asset_cannot_make_a_foreign_zero_byte_target_owned(
    tmp_path: Path,
) -> None:
    source = tmp_path / "empty.jpg"
    target = tmp_path / PhotoMark.KEEP.value / source.name
    _write_exact(source, b"", 1_700_000_000_000_000_010)
    _write_exact(target, b"", 1_700_000_000_000_000_010)
    transaction = PhotoMoveTransaction(
        id="empty-move",
        photo_id="empty-photo",
        before_mark=PhotoMark.UNPROCESSED,
        after_mark=PhotoMark.KEEP,
        created_at_ns=1,
        items=(
            MoveItem(
                ordinal=0,
                kind=AssetKind.JPEG,
                source_path=str(source.absolute()),
                target_path=str(target.absolute()),
                size=0,
                mtime_ns=1_700_000_000_000_000_010,
            ),
        ),
    )

    result = PhotoMoveService().recover_pending_move(transaction)

    assert result.decision is RecoveryDecision.NEEDS_USER
    assert result.unresolved_ordinals == (0,)
    assert set(result.recovery_paths) == {source, target}
    assert source.exists()
    assert target.exists()


@pytest.mark.parametrize("mutation", ("source_mtime", "target_size", "symlink"))
def test_mismatch_or_symlink_never_counts_as_exact_move_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    transaction = _transaction(tmp_path)
    item = transaction.items[0]
    source = Path(item.source_path)
    target = Path(item.target_path)
    if mutation == "source_mtime":
        os.utime(source, ns=(item.mtime_ns + 1, item.mtime_ns + 1))
    elif mutation == "target_size":
        source.unlink()
        _write_exact(target, b"foreign-size", item.mtime_ns)
    else:
        source.unlink()
        backing = tmp_path / "backing.jpg"
        _write_exact(backing, b"jpeg-contents", item.mtime_ns)
        target.parent.mkdir()
        target.symlink_to(backing)

    result = PhotoMoveService().recover_pending_move(transaction)

    assert result.decision is RecoveryDecision.NEEDS_USER
    assert 0 in result.unresolved_ordinals
    assert source in result.recovery_paths
    assert target in result.recovery_paths
    if mutation == "target_size":
        assert target.read_bytes() == b"foreign-size"
    elif mutation == "symlink":
        assert target.is_symlink()


def test_both_exact_files_are_preserved_as_a_conflict(tmp_path: Path) -> None:
    transaction = _transaction(tmp_path)
    for item in transaction.items:
        source = Path(item.source_path)
        _write_exact(Path(item.target_path), source.read_bytes(), item.mtime_ns)

    result = PhotoMoveService().recover_pending_move(transaction)

    assert result.decision is RecoveryDecision.NEEDS_USER
    assert result.unresolved_ordinals == (0, 1)
    assert set(result.recovery_paths) == {
        path
        for item in transaction.items
        for path in (Path(item.source_path), Path(item.target_path))
    }
    assert all(Path(item.source_path).exists() for item in transaction.items)
    assert all(Path(item.target_path).exists() for item in transaction.items)


def test_partial_rollback_collision_preserves_foreign_source_and_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transaction = _transaction(tmp_path)
    moved = transaction.items[0]
    _complete_atomic_move(moved)
    mover = PhotoMoveService()
    original_move = mover._move_no_clobber

    def collide_before_rollback(
        source: Path,
        target: Path,
        expected: MoveItem | None = None,
    ) -> None:
        target.write_bytes(b"foreign-source")
        original_move(source, target, expected)

    monkeypatch.setattr(mover, "_move_no_clobber", collide_before_rollback)

    result = mover.recover_pending_move(transaction)

    source = Path(moved.source_path)
    target = Path(moved.target_path)
    assert result.decision is RecoveryDecision.NEEDS_USER
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert set(result.recovery_paths) == {source, target}
    assert source.read_bytes() == b"foreign-source"
    assert target.read_bytes() == b"jpeg-contents"
