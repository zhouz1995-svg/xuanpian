"""Safety tests for logical-photo filesystem move transactions.

Every test uses pytest's disposable ``tmp_path`` fixture.  No user photo or
non-temporary path is ever moved by this module.
"""

from __future__ import annotations

import errno
import hashlib
import os
import sqlite3
import ctypes
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.domain.photo_move import MoveItem, PhotoMoveTransaction
from src.xuanpian.services import photo_move_service as move_module
from src.xuanpian.services.photo_move_service import PhotoMoveService
from src.xuanpian.store.migrations import migrate_database
from src.xuanpian.store.project_store import ProjectStore


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _asset(
    kind: AssetKind,
    path: Path,
    *,
    origin: Path | None = None,
) -> PhotoAssetRecord:
    metadata = path.stat()
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=FileFingerprint(
            path=str(path.absolute()),
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
        ),
        origin_path=str((origin or path).absolute()),
    )


def _pair(
    tmp_path: Path,
    *,
    jpeg_parent: Path | None = None,
    raw_parent: Path | None = None,
    mark: str = PhotoMark.UNPROCESSED.value,
) -> PhotoRecord:
    jpeg = _write((jpeg_parent or tmp_path) / "pair.jpg", b"jpeg-content")
    raw = _write((raw_parent or tmp_path) / "pair.cr3", b"raw-content")
    return PhotoRecord(
        id="stable-photo-id",
        filename=jpeg.name,
        mark=mark,
        jpeg=_asset(AssetKind.JPEG, jpeg),
        raw=_asset(AssetKind.RAW, raw),
    )


def _moved_photo(
    photo: PhotoRecord,
    mark: PhotoMark,
) -> PhotoRecord:
    assets: dict[AssetKind, PhotoAssetRecord] = {}
    for asset in photo.source_assets:
        origin = Path(asset.origin_path)
        current = origin.parent / mark.value / origin.name
        _write(current, Path(asset.fingerprint.path).read_bytes())
        Path(asset.fingerprint.path).unlink()
        assets[asset.kind] = _asset(asset.kind, current, origin=origin)
    return PhotoRecord(
        id=photo.id,
        filename=photo.filename,
        mark=mark.value,
        jpeg=assets.get(AssetKind.JPEG),
        raw=assets.get(AssetKind.RAW),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_v2_legacy_project(
    database: Path,
    root: Path,
    mark: str,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL
            );
            CREATE TABLE photos (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                mark TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE photo_assets (
                photo_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                PRIMARY KEY (photo_id, kind),
                FOREIGN KEY (photo_id) REFERENCES photos(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE analysis_results (
                photo_id TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'pending', 'running', 'succeeded',
                        'failed', 'canceled'
                    )
                ),
                result_json TEXT,
                PRIMARY KEY (photo_id, analyzer),
                FOREIGN KEY (photo_id) REFERENCES photos(id)
                    ON DELETE CASCADE
            );
            CREATE TABLE analysis_failures (
                photo_id TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                message TEXT NOT NULL,
                exception_type TEXT NOT NULL,
                PRIMARY KEY (photo_id, analyzer),
                FOREIGN KEY (photo_id) REFERENCES photos(id)
                    ON DELETE CASCADE
            );
            INSERT INTO metadata (key, value) VALUES ('schema_version', '1');
            """
        )
        connection.execute(
            "INSERT INTO projects (id, root_path) VALUES (?, ?)",
            ("project", str(root)),
        )
    migrate_database(database, target_version=2)

    jpeg = _write(root / "legacy.jpg", b"legacy-jpeg")
    raw = _write(root / "legacy.cr3", b"legacy-raw")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO photos (id, project_id, filename, mark)
            VALUES ('legacy', 'project', 'legacy.jpg', ?)
            """,
            (mark,),
        )
        for kind, path in (("jpeg", jpeg), ("raw", raw)):
            metadata = path.stat()
            connection.execute(
                """
                INSERT INTO photo_assets (
                    photo_id, kind, path, size, mtime_ns, present
                ) VALUES ('legacy', ?, ?, ?, ?, 1)
                """,
                (
                    kind,
                    str(path.absolute()),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ),
            )


def test_pair_moves_together_and_reports_each_step(tmp_path: Path) -> None:
    photo = _pair(tmp_path)
    before = {
        asset.kind: _digest(Path(asset.fingerprint.path))
        for asset in photo.source_assets
    }
    completed: list[int] = []
    mover = PhotoMoveService()

    plan = mover.plan(photo, PhotoMark.KEEP)
    result = mover.execute(plan, completed.append)

    assert result.success
    assert result.error == ""
    assert completed == [0, 1]
    assert result.completed_ordinals == (0, 1)
    assert result.rollback_complete is None
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == ()
    assert result.recovery_paths == ()
    assert result.moved_assets == tuple(
        PhotoAssetRecord(
            kind=item.kind,
            fingerprint=FileFingerprint(
                path=item.target_path,
                size=item.size,
                mtime_ns=item.mtime_ns,
            ),
            origin_path=photo.source_assets[item.ordinal].origin_path,
        )
        for item in plan.items
    )
    assert all(Path(item.target_path).is_file() for item in plan.items)
    assert not any(Path(item.source_path).exists() for item in plan.items)
    assert {
        item.kind: _digest(Path(item.target_path)) for item in plan.items
    } == before

    with pytest.raises(FrozenInstanceError):
        result.success = False  # type: ignore[misc]


def test_cancellation_gate_is_checked_only_before_the_first_move(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    gate_calls = 0
    completed: list[int] = []

    def should_cancel() -> bool:
        nonlocal gate_calls
        gate_calls += 1
        return False

    result = mover.execute(
        plan,
        completed.append,
        should_cancel_before_first_move=should_cancel,
    )

    assert result.success
    assert completed == [0, 1]
    assert gate_calls == 1


def test_plan_uses_each_assets_immutable_origin_parent(tmp_path: Path) -> None:
    jpeg_parent = tmp_path / "jpeg-library"
    raw_parent = tmp_path / "raw-library"
    photo = _pair(
        tmp_path,
        jpeg_parent=jpeg_parent,
        raw_parent=raw_parent,
    )

    plan = PhotoMoveService().plan(photo, PhotoMark.DISCARD)

    assert tuple(Path(item.target_path) for item in plan.items) == (
        jpeg_parent / PhotoMark.DISCARD.value / "pair.jpg",
        raw_parent / PhotoMark.DISCARD.value / "pair.cr3",
    )
    assert plan.photo_id == "stable-photo-id"


def test_cross_state_move_still_targets_the_origin_parent(tmp_path: Path) -> None:
    original = _pair(tmp_path / "original")
    photo = _moved_photo(original, PhotoMark.KEEP)

    plan = PhotoMoveService().plan(photo, PhotoMark.HOLD)
    result = PhotoMoveService().execute(plan, lambda _ordinal: None)

    assert result.success
    assert tuple(Path(item.target_path) for item in plan.items) == (
        tmp_path / "original" / PhotoMark.HOLD.value / "pair.jpg",
        tmp_path / "original" / PhotoMark.HOLD.value / "pair.cr3",
    )
    old_status_directory = tmp_path / "original" / PhotoMark.KEEP.value
    assert old_status_directory.is_dir()
    assert not any(old_status_directory.iterdir())


def test_unprocessed_undo_targets_the_exact_origin_paths(tmp_path: Path) -> None:
    original = _pair(tmp_path / "original")
    photo = _moved_photo(original, PhotoMark.DISCARD)

    plan = PhotoMoveService().plan(photo, PhotoMark.UNPROCESSED)
    result = PhotoMoveService().execute(plan, lambda _ordinal: None)

    assert result.success
    assert tuple(item.target_path for item in plan.items) == tuple(
        asset.origin_path for asset in photo.source_assets
    )
    assert all(Path(asset.origin_path).is_file() for asset in photo.source_assets)


def test_plan_normalizes_known_legacy_before_mark_without_guessing_unknowns(
    tmp_path: Path,
) -> None:
    legacy = _pair(tmp_path, mark="入选")

    plan = PhotoMoveService().plan(legacy, PhotoMark.HOLD)

    assert plan.before_mark is PhotoMark.KEEP
    assert plan.after_mark is PhotoMark.HOLD

    unknown = replace(legacy, mark="已移出")
    with pytest.raises(ValueError, match="managed"):
        PhotoMoveService().plan(unknown, PhotoMark.HOLD)


@pytest.mark.parametrize(
    ("legacy_mark", "normalized", "target_mark"),
    [
        ("入选", PhotoMark.KEEP, PhotoMark.HOLD),
        ("淘汰", PhotoMark.DISCARD, PhotoMark.KEEP),
        ("待定", PhotoMark.HOLD, PhotoMark.DISCARD),
    ],
)
def test_real_v2_pair_migrates_then_plans_executes_and_commits_from_origin(
    tmp_path: Path,
    legacy_mark: str,
    normalized: PhotoMark,
    target_mark: PhotoMark,
) -> None:
    root = tmp_path / "photos"
    database = tmp_path / "project.sqlite3"
    _create_v2_legacy_project(database, root, legacy_mark)

    with ProjectStore.open(database) as store:
        photo = store.get_photo("legacy")
        assert photo is not None
        assert photo.mark == legacy_mark
        assert all(
            asset.fingerprint.path == asset.origin_path
            for asset in photo.source_assets
        )

        mover = PhotoMoveService()
        plan = mover.plan(photo, target_mark)
        assert plan.before_mark is normalized
        store.begin_photo_move(plan)
        result = mover.execute(
            plan,
            lambda ordinal: store.mark_move_item_completed(plan.id, ordinal),
        )
        assert result.success
        store.commit_photo_move(plan.id, result.moved_assets)

        committed = store.get_photo("legacy")
        assert committed is not None
        assert committed.id == "legacy"
        assert committed.mark == target_mark.value
        assert tuple(asset.fingerprint.path for asset in committed.source_assets) == (
            str(root / target_mark.value / "legacy.jpg"),
            str(root / target_mark.value / "legacy.cr3"),
        )
        assert tuple(asset.origin_path for asset in committed.source_assets) == (
            str(root / "legacy.jpg"),
            str(root / "legacy.cr3"),
        )


@pytest.mark.parametrize("target", ["保留", None, object()])
def test_plan_requires_an_exact_managed_target_mark(
    tmp_path: Path,
    target: object,
) -> None:
    photo = _pair(tmp_path)

    with pytest.raises(ValueError, match="target_mark"):
        PhotoMoveService().plan(photo, target)  # type: ignore[arg-type]


def test_plan_rejects_empty_identity_noop_and_silent_rename(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    with pytest.raises(ValueError, match="photo id"):
        PhotoMoveService().plan(replace(photo, id=""), PhotoMark.KEEP)

    kept = _moved_photo(photo, PhotoMark.KEEP)
    with pytest.raises(ValueError, match="already"):
        PhotoMoveService().plan(kept, PhotoMark.KEEP)

    jpeg = kept.jpeg
    assert jpeg is not None
    renamed = replace(
        kept,
        mark=PhotoMark.KEEP.value,
        jpeg=replace(jpeg, origin_path=str(tmp_path / "different-name.jpg")),
    )
    with pytest.raises(ValueError, match="rename"):
        PhotoMoveService().plan(renamed, PhotoMark.HOLD)


@pytest.mark.parametrize("breakage", ["missing", "directory", "size", "mtime"])
def test_plan_strictly_validates_current_fingerprints(
    tmp_path: Path,
    breakage: str,
) -> None:
    photo = _pair(tmp_path)
    jpeg = photo.jpeg
    assert jpeg is not None
    path = Path(jpeg.fingerprint.path)
    if breakage == "missing":
        path.unlink()
    elif breakage == "directory":
        path.unlink()
        path.mkdir()
    elif breakage == "size":
        path.write_bytes(b"different-size")
    else:
        os.utime(path, ns=(jpeg.fingerprint.mtime_ns + 1, jpeg.fingerprint.mtime_ns + 1))

    with pytest.raises((FileNotFoundError, IsADirectoryError, OSError), match="source"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)


def test_plan_rejects_relative_origin_without_touching_files(tmp_path: Path) -> None:
    photo = _pair(tmp_path)
    jpeg = photo.jpeg
    assert jpeg is not None
    object.__setattr__(jpeg, "origin_path", "relative/pair.jpg")

    with pytest.raises(ValueError, match="origin"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)

    assert Path(jpeg.fingerprint.path).is_file()
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_plan_rejects_current_path_unrelated_to_immutable_origin(
    tmp_path: Path,
) -> None:
    elsewhere = _write(tmp_path / "elsewhere" / "pair.jpg", b"jpeg")
    (tmp_path / "origin").mkdir()
    photo = PhotoRecord(
        id="stable",
        filename="pair.jpg",
        mark=PhotoMark.UNPROCESSED.value,
        jpeg=_asset(
            AssetKind.JPEG,
            elsewhere,
            origin=tmp_path / "origin" / "pair.jpg",
        ),
        raw=None,
    )

    with pytest.raises(ValueError, match="immutable origin"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)

    assert elsewhere.read_bytes() == b"jpeg"
    assert not (tmp_path / "origin" / PhotoMark.KEEP.value).exists()


def test_execute_rejects_a_forged_target_outside_the_origin_relationship(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path / "origin")
    valid = PhotoMoveService().plan(photo, PhotoMark.KEEP)
    (tmp_path / "other").mkdir()
    forged = replace(
        valid,
        items=tuple(
            replace(
                item,
                target_path=str(
                    tmp_path / "other" / PhotoMark.KEEP.value / Path(item.target_path).name
                ),
            )
            for item in valid.items
        ),
    )

    result = PhotoMoveService().execute(forged, lambda _ordinal: None)

    assert not result.success
    assert "immutable origin" in result.error
    assert all(Path(item.source_path).is_file() for item in valid.items)
    assert not (tmp_path / "other" / PhotoMark.KEEP.value).exists()


def test_execute_allows_normalized_legacy_source_at_target_derived_origin(
    tmp_path: Path,
) -> None:
    legacy = _pair(tmp_path / "origin", mark="入选")
    plan = PhotoMoveService().plan(legacy, PhotoMark.HOLD)

    result = PhotoMoveService().execute(plan, lambda _ordinal: None)

    assert result.success
    assert all(Path(item.target_path).is_file() for item in plan.items)


def test_target_collision_is_rejected_without_creating_or_moving_anything(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    collision = _write(
        tmp_path / PhotoMark.KEEP.value / "pair.jpg",
        b"someone-elses-file",
    )
    untouched = collision.read_bytes()

    with pytest.raises(FileExistsError, match="target"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)

    assert collision.read_bytes() == untouched
    assert all(Path(asset.fingerprint.path).is_file() for asset in photo.source_assets)


def test_execute_repeats_preflight_before_creating_directories(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    Path(plan.items[1].source_path).unlink()

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == ()
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (1,)
    assert not (tmp_path / PhotoMark.KEEP.value).exists()
    assert Path(plan.items[0].source_path).is_file()


def test_execute_reports_target_that_appears_after_plan_as_unresolved(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    collision = _write(
        Path(plan.items[0].target_path),
        b"appeared-after-plan",
    )

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert result.recovery_paths == (collision,)
    assert collision.read_bytes() == b"appeared-after-plan"
    assert all(Path(item.source_path).is_file() for item in plan.items)


def test_read_only_target_parent_fails_before_status_directory_creation(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    photo = _pair(origin)
    origin.chmod(0o500)
    try:
        with pytest.raises(PermissionError, match="writable"):
            PhotoMoveService().plan(photo, PhotoMark.KEEP)
    finally:
        origin.chmod(0o700)

    assert not (origin / PhotoMark.KEEP.value).exists()
    assert all(Path(asset.fingerprint.path).is_file() for asset in photo.source_assets)


def test_cross_device_target_is_rejected_as_a_controlled_preflight_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    source_paths = {
        Path(asset.fingerprint.path) for asset in photo.source_assets
    }
    monkeypatch.setattr(
        mover,
        "_device_id",
        lambda path: 1 if path in source_paths else 2,
    )

    with pytest.raises(OSError) as raised:
        mover.plan(photo, PhotoMark.KEEP)

    assert raised.value.errno == errno.EXDEV
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_execute_rejects_internal_path_and_kind_collisions_before_moves(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    valid = PhotoMoveService().plan(photo, PhotoMark.KEEP)
    first, second = valid.items
    colliding = replace(
        valid,
        items=(
            first,
            replace(
                second,
                kind=first.kind,
                target_path=first.target_path,
            ),
        ),
    )

    result = PhotoMoveService().execute(colliding, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == ()
    assert all(Path(item.source_path).is_file() for item in valid.items)
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_execute_rejects_source_target_overlap_before_moves(tmp_path: Path) -> None:
    photo = _pair(tmp_path)
    valid = PhotoMoveService().plan(photo, PhotoMark.KEEP)
    first, second = valid.items
    overlap = replace(
        valid,
        items=(
            replace(first, target_path=second.source_path),
            second,
        ),
    )

    result = PhotoMoveService().execute(overlap, lambda _ordinal: None)

    assert not result.success
    assert all(Path(item.source_path).is_file() for item in valid.items)
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_second_failure_rolls_first_back_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    original_move = mover._move_no_clobber
    calls = 0

    def fail_second_call(
        source: Path,
        target: Path,
        expected: MoveItem | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("injected second move failure")
        original_move(source, target, expected)

    monkeypatch.setattr(mover, "_move_no_clobber", fail_second_call)

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == (0,)
    assert result.recovery_paths == ()
    assert result.rollback_complete
    assert result.rolled_back_ordinals == (0,)
    assert result.unresolved_ordinals == ()
    assert all(Path(item.source_path).is_file() for item in plan.items)
    assert not any(Path(item.target_path).exists() for item in plan.items)
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_callback_failure_is_an_execution_failure_and_rolls_the_move_back(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    callbacks: list[int] = []

    def fail_callback(ordinal: int) -> None:
        callbacks.append(ordinal)
        raise RuntimeError("journal unavailable")

    result = mover.execute(plan, fail_callback)

    assert not result.success
    assert callbacks == [0]
    assert result.completed_ordinals == ()
    assert result.rollback_complete
    assert result.rolled_back_ordinals == (0,)
    assert result.unresolved_ordinals == ()
    assert "journal unavailable" in result.error
    assert all(Path(item.source_path).is_file() for item in plan.items)
    assert not any(Path(item.target_path).exists() for item in plan.items)


def test_post_atomic_rename_fingerprint_mismatch_reports_target_for_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    item = plan.items[0]
    source = Path(item.source_path)
    target = Path(item.target_path)
    original_move = move_module._native_rename_no_replace
    corrupted = False

    def corrupt_after_move(moved_source: Path, moved_target: Path) -> None:
        nonlocal corrupted
        original_move(moved_source, moved_target)
        if not corrupted and moved_target == target:
            corrupted = True
            os.utime(target, ns=(item.mtime_ns + 1, item.mtime_ns + 1))

    monkeypatch.setattr(move_module, "_native_rename_no_replace", corrupt_after_move)

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == ()
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert "mtime mismatch" in result.error
    assert result.recovery_paths == (source,)
    assert source.is_file() and not target.exists()
    assert Path(plan.items[1].source_path).is_file()


def test_rollback_conflict_preserves_both_files_and_reports_recovery_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    original_move = mover._move_no_clobber
    calls = 0

    def conflict_during_second(
        source: Path,
        target: Path,
        expected: MoveItem | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            Path(plan.items[0].source_path).write_bytes(b"conflicting-file")
            raise OSError("second move failed")
        original_move(source, target, expected)

    monkeypatch.setattr(mover, "_move_no_clobber", conflict_during_second)

    result = mover.execute(plan, lambda _ordinal: None)

    conflict = Path(plan.items[0].source_path)
    stranded = Path(plan.items[0].target_path)
    assert not result.success
    assert result.completed_ordinals == (0,)
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert set(result.recovery_paths) == {conflict, stranded}
    assert conflict.read_bytes() == b"conflicting-file"
    assert stranded.read_bytes() == b"jpeg-content"
    assert Path(plan.items[1].source_path).read_bytes() == b"raw-content"


def test_source_disappearing_before_atomic_rename_does_not_create_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    vanished = Path(plan.items[0].source_path)

    original_move = move_module._native_rename_no_replace

    def disappear_before_move(source: Path, target: Path) -> None:
        Path(source).unlink()
        original_move(source, target)

    monkeypatch.setattr(move_module, "_native_rename_no_replace", disappear_before_move)

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == ()
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert not vanished.exists()
    assert not Path(plan.items[0].target_path).exists()
    assert Path(plan.items[1].source_path).read_bytes() == b"raw-content"
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [(AssetKind.JPEG, ".jpg"), (AssetKind.RAW, ".cr3")],
)
@pytest.mark.parametrize(
    "target_mark",
    [PhotoMark.KEEP, PhotoMark.HOLD, PhotoMark.DISCARD],
)
def test_single_asset_status_matrix(
    tmp_path: Path,
    kind: AssetKind,
    suffix: str,
    target_mark: PhotoMark,
) -> None:
    source = _write(tmp_path / f"single{suffix}", kind.value.encode())
    asset = _asset(kind, source)
    photo = PhotoRecord(
        id=f"single-{kind.value}",
        filename=source.name,
        mark=PhotoMark.UNPROCESSED.value,
        jpeg=asset if kind is AssetKind.JPEG else None,
        raw=asset if kind is AssetKind.RAW else None,
    )

    mover = PhotoMoveService()
    plan = mover.plan(photo, target_mark)
    result = mover.execute(plan, lambda _ordinal: None)

    assert result.success
    assert result.completed_ordinals == (0,)
    assert result.rollback_complete is None
    assert Path(plan.items[0].target_path).read_bytes() == kind.value.encode()
    assert not source.exists()


def test_second_callback_failure_rolls_both_verified_moves_back_in_reverse(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    callbacks: list[int] = []

    def fail_after_second_callback(ordinal: int) -> None:
        callbacks.append(ordinal)
        if ordinal == 1:
            raise RuntimeError("second durable callback uncertain")

    result = mover.execute(plan, fail_after_second_callback)

    assert not result.success
    assert callbacks == [0, 1]
    assert result.completed_ordinals == (0,)
    assert result.rollback_complete
    assert result.rolled_back_ordinals == (1, 0)
    assert result.unresolved_ordinals == ()
    assert result.recovery_paths == ()
    assert all(Path(item.source_path).is_file() for item in plan.items)


def test_second_verification_failure_rolls_both_physical_moves_back_in_reverse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    original_verify = mover._verify_fingerprint

    def fail_second_target_verification(path: Path, item: MoveItem) -> None:
        if item.ordinal == 1 and path == Path(item.target_path):
            raise OSError("injected second verification failure")
        original_verify(path, item)

    monkeypatch.setattr(mover, "_verify_fingerprint", fail_second_target_verification)

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == (0,)
    assert result.rollback_complete
    assert result.rolled_back_ordinals == (1, 0)
    assert result.unresolved_ordinals == ()
    assert all(Path(item.source_path).is_file() for item in plan.items)


def test_second_rollback_failure_continues_and_reports_only_unresolved_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    original_move = mover._move_no_clobber
    calls = 0

    def fail_first_rollback(
        source: Path,
        target: Path,
        expected: MoveItem | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise PermissionError("injected RAW rollback failure")
        original_move(source, target, expected)

    def fail_after_pair(ordinal: int) -> None:
        if ordinal == 1:
            raise RuntimeError("trigger pair rollback")

    monkeypatch.setattr(mover, "_move_no_clobber", fail_first_rollback)

    result = mover.execute(plan, fail_after_pair)

    assert not result.success
    assert result.completed_ordinals == (0,)
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == (0,)
    assert result.unresolved_ordinals == (1,)
    assert result.recovery_paths == (Path(plan.items[1].target_path),)
    assert Path(plan.items[0].source_path).is_file()
    assert Path(plan.items[1].target_path).is_file()


def test_multiple_rollback_conflicts_preserve_and_report_every_path(
    tmp_path: Path,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)

    def create_both_conflicts_then_fail(ordinal: int) -> None:
        if ordinal == 1:
            for item in plan.items:
                Path(item.source_path).write_bytes(
                    f"conflict-{item.ordinal}".encode()
                )
            raise RuntimeError("trigger conflicting rollback")

    result = mover.execute(plan, create_both_conflicts_then_fail)

    assert not result.success
    assert result.completed_ordinals == (0,)
    assert not result.rollback_complete
    assert result.rolled_back_ordinals == ()
    assert result.unresolved_ordinals == (0, 1)
    assert set(result.recovery_paths) == {
        *(Path(item.source_path) for item in plan.items),
        *(Path(item.target_path) for item in plan.items),
    }
    assert all(Path(item.source_path).is_file() for item in plan.items)
    assert all(Path(item.target_path).is_file() for item in plan.items)


def test_static_source_symlink_is_rejected_without_creating_target(
    tmp_path: Path,
) -> None:
    real = _write(tmp_path / "real.jpg", b"jpeg")
    linked = tmp_path / "linked.jpg"
    try:
        linked.symlink_to(real)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    photo = PhotoRecord(
        id="symlink-source",
        filename=linked.name,
        mark=PhotoMark.UNPROCESSED.value,
        jpeg=_asset(AssetKind.JPEG, linked),
        raw=None,
    )

    with pytest.raises(IsADirectoryError, match="regular file"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)

    assert linked.is_symlink()
    assert real.read_bytes() == b"jpeg"
    assert not (tmp_path / PhotoMark.KEEP.value).exists()


def test_static_target_status_directory_symlink_is_rejected(
    tmp_path: Path,
) -> None:
    origin = tmp_path / "origin"
    photo = _pair(origin)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    status = origin / PhotoMark.KEEP.value
    try:
        status.symlink_to(foreign, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")

    with pytest.raises(NotADirectoryError, match="not a directory"):
        PhotoMoveService().plan(photo, PhotoMark.KEEP)

    assert status.is_symlink()
    assert not any(foreign.iterdir())
    assert all(Path(asset.fingerprint.path).is_file() for asset in photo.source_assets)


def test_real_late_target_collision_during_atomic_rename_never_overwrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    target = Path(plan.items[0].target_path)
    original_create_directories = mover._create_target_directories

    def install_foreign_after_preflight(items):
        created = original_create_directories(items)
        target.write_bytes(b"late-foreign")
        return created

    monkeypatch.setattr(
        mover,
        "_create_target_directories",
        install_foreign_after_preflight,
    )

    result = mover.execute(plan, lambda _ordinal: None)

    assert not result.success
    assert result.completed_ordinals == ()
    assert result.unresolved_ordinals == (0,)
    assert result.recovery_paths == (target,)
    assert target.read_bytes() == b"late-foreign"
    assert all(Path(item.source_path).is_file() for item in plan.items)


def test_atomic_native_move_never_uses_path_unlink_or_generic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    def forbidden_path_operation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsafe path operation must not run")

    monkeypatch.setattr(Path, "unlink", forbidden_path_operation)
    monkeypatch.setattr(move_module.os, "rename", forbidden_path_operation)

    result = mover.execute(plan, lambda _ordinal: None)

    assert result.success
    assert all(Path(item.target_path).is_file() for item in plan.items)
    assert not any(Path(item.source_path).exists() for item in plan.items)


def test_atomic_native_move_eliminates_identity_check_then_unlink_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = _pair(tmp_path)
    mover = PhotoMoveService()
    plan = mover.plan(photo, PhotoMark.KEEP)
    source = Path(plan.items[0].source_path)
    target = Path(plan.items[0].target_path)
    called = False

    def legacy_unlink(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("identity-check then unlink race reintroduced")

    monkeypatch.setattr(Path, "unlink", legacy_unlink)

    result = mover.execute(plan, lambda _ordinal: None)

    assert result.success
    assert not called
    assert target.read_bytes() == b"jpeg-content"
    assert not source.exists()


def test_macos_no_replace_wrapper_passes_exclusive_flag_and_maps_collision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jpg"
    target = tmp_path / "target.jpg"
    calls: list[tuple[bytes, bytes, int]] = []

    def fail_existing(old: bytes, new: bytes, flags: int) -> int:
        calls.append((old, new, flags))
        ctypes.set_errno(errno.EEXIST)
        return -1

    with pytest.raises(FileExistsError):
        move_module._rename_no_replace_macos(
            source,
            target,
            operation=fail_existing,
        )

    assert calls == [(os.fsencode(source), os.fsencode(target), 0x00000004)]


def test_linux_no_replace_wrapper_passes_noreplace_flag_and_preserves_errno(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jpg"
    target = tmp_path / "target.jpg"
    calls: list[tuple[int, bytes, int, bytes, int]] = []

    def deny(
        old_directory: int,
        old: bytes,
        new_directory: int,
        new: bytes,
        flags: int,
    ) -> int:
        calls.append((old_directory, old, new_directory, new, flags))
        ctypes.set_errno(errno.EACCES)
        return -1

    with pytest.raises(PermissionError):
        move_module._rename_no_replace_linux(
            source,
            target,
            operation=deny,
        )

    assert calls == [
        (-100, os.fsencode(source), -100, os.fsencode(target), 1)
    ]


def test_linux_missing_noreplace_support_maps_to_explicit_unsupported(
    tmp_path: Path,
) -> None:
    def unavailable(*_args: object) -> int:
        ctypes.set_errno(errno.ENOSYS)
        return -1

    with pytest.raises(OSError) as error:
        move_module._rename_no_replace_linux(
            tmp_path / "source.jpg",
            tmp_path / "target.jpg",
            operation=unavailable,
        )

    assert error.value.errno == errno.ENOTSUP


def test_windows_no_replace_wrapper_uses_movefile_without_replace_and_maps_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jpg"
    target = tmp_path / "target.jpg"
    calls: list[tuple[str, str]] = []

    def existing(old: str, new: str) -> int:
        calls.append((old, new))
        return 0

    with pytest.raises(FileExistsError):
        move_module._rename_no_replace_windows(
            source,
            target,
            operation=existing,
            get_last_error=lambda: 183,
        )

    assert calls == [(str(source), str(target))]


def test_windows_no_replace_wrapper_maps_cross_device_without_fallback(
    tmp_path: Path,
) -> None:
    with pytest.raises(OSError) as error:
        move_module._rename_no_replace_windows(
            tmp_path / "source.jpg",
            tmp_path / "target.jpg",
            operation=lambda _old, _new: 0,
            get_last_error=lambda: 17,
        )

    assert error.value.errno == errno.EXDEV


def test_unsupported_platform_fails_closed_without_generic_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.jpg"
    target = tmp_path / "target.jpg"

    def unsafe_fallback(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("generic rename fallback must not run")

    monkeypatch.setattr(move_module.sys, "platform", "unsupported-os")
    monkeypatch.setattr(move_module.os, "rename", unsafe_fallback)

    with pytest.raises(OSError) as error:
        move_module._native_rename_no_replace(source, target)

    assert error.value.errno == errno.ENOTSUP


@pytest.mark.parametrize(
    ("platform", "backend_name"),
    (
        ("darwin", "_rename_no_replace_macos"),
        ("linux", "_rename_no_replace_linux"),
        ("win32", "_rename_no_replace_windows"),
    ),
)
def test_native_no_replace_dispatches_only_to_the_platform_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    backend_name: str,
) -> None:
    source = tmp_path / "source.jpg"
    target = tmp_path / "target.jpg"
    calls: list[tuple[Path, Path]] = []

    def controlled_failure(old: Path, new: Path) -> None:
        calls.append((old, new))
        raise OSError(errno.EACCES, "controlled backend failure")

    monkeypatch.setattr(move_module.sys, "platform", platform)
    monkeypatch.setattr(move_module, backend_name, controlled_failure)

    with pytest.raises(PermissionError):
        move_module._native_rename_no_replace(source, target)

    assert calls == [(source, target)]
