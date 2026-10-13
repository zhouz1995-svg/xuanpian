"""Exact action inverse, persistent provenance and immutable-origin safety."""

from dataclasses import replace
from pathlib import Path
import sqlite3
import json

import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.services.photo_move_service import PhotoMoveService, RecoveryDecision
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.store.migrations import migrate_database
from tests.test_culling_move_integration import _record


def _committed_forward(qapp, tmp_path):
    record = replace(_record(tmp_path, "legacy", pair=True), mark="保留")
    store = ProjectStore.open(tmp_path / "project.sqlite3", root_path=tmp_path)
    store.upsert_photos((record,))
    service = PhotoMoveService()
    forward = service.plan(record, PhotoMark.HOLD)
    store.begin_photo_move(forward)
    result = service.execute(forward, lambda ordinal: store.mark_move_item_completed(forward.id, ordinal))
    assert result.success
    store.commit_photo_move(forward.id, result.moved_assets)
    return store, record, service, forward


@pytest.mark.parametrize("moved_count", [0, 1, 2])
def test_legacy_inverse_journal_reopens_and_recovers_exact_prior_paths(qapp, tmp_path, moved_count):
    store, before, service, forward = _committed_forward(qapp, tmp_path)
    try:
        inverse = service.prepare_inverse(forward)
        assert inverse.inverse_of == forward
        assert inverse.before_mark is PhotoMark.HOLD
        assert inverse.after_mark is PhotoMark.KEEP
        assert tuple(item.target_path for item in inverse.items) == tuple(
            asset.origin_path for asset in before.source_assets
        )
        store.begin_photo_move(inverse)
        for item in inverse.items[:moved_count]:
            service._move_no_clobber(Path(item.source_path), Path(item.target_path), item)
            store.mark_move_item_completed(inverse.id, item.ordinal)
    finally:
        store.close()

    with ProjectStore.open(tmp_path / "project.sqlite3", root_path=tmp_path) as reopened:
        pending, = reopened.list_pending_photo_moves()
        assert pending.transaction == inverse
        outcome = service.recover_pending_move(pending.transaction, completed_ordinals=pending.completed_ordinals)
        if moved_count == 2:
            assert outcome.decision is RecoveryDecision.READY_TO_COMMIT
            reopened.commit_photo_move(inverse.id, outcome.moved_assets)
            assert reopened.get_photo("legacy") == before
        else:
            expected = RecoveryDecision.ABORTED if moved_count == 0 else RecoveryDecision.ROLLED_BACK
            assert outcome.decision is expected
            reopened.abort_photo_move(inverse.id)
            assert reopened.get_photo("legacy").mark == "待定"
            assert all(Path(item.target_path).is_file() for item in forward.items)
        assert reopened.list_pending_photo_moves() == ()


def test_inverse_refuses_arbitrary_targets_and_nested_provenance(qapp, tmp_path):
    store, before, service, forward = _committed_forward(qapp, tmp_path)
    try:
        inverse = service.prepare_inverse(forward)
        with pytest.raises(ValueError):
            replace(inverse, items=(replace(inverse.items[0], target_path=str(tmp_path / "elsewhere" / "legacy.jpg")), *inverse.items[1:]))
        with pytest.raises(ValueError):
            replace(inverse, inverse_of=inverse)
        # An internally self-consistent forged snapshot still cannot change
        # the original directory persisted for this exact current photo.
        other = tmp_path / "different-root"
        fake_items = tuple(replace(item, source_path=str(other / Path(item.source_path).name)) for item in forward.items)
        fake_forward = replace(forward, items=fake_items)
        with pytest.raises((ValueError, RuntimeError)):
            forged = service.prepare_inverse(fake_forward)
            store.begin_photo_move(forged)
        assert store.list_pending_photo_moves() == ()
    finally:
        store.close()


def test_v3_migration_preserves_pending_forward_with_null_inverse(tmp_path):
    from src.xuanpian.store.schema import _VERSION_THREE_CREATE_TABLES_SQL
    database = tmp_path / "v3.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(_VERSION_THREE_CREATE_TABLES_SQL)
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '3')")
        connection.execute("INSERT INTO projects VALUES ('p', ?)", (str(tmp_path),))
        connection.execute("INSERT INTO photos VALUES ('photo', 'p', 'a.jpg', '未处理')")
        connection.execute("INSERT INTO photo_assets VALUES ('photo','jpeg',?,1,2,1,?)", (str(tmp_path / "a.jpg"), str(tmp_path / "a.jpg")))
        connection.execute("INSERT INTO photo_move_transactions VALUES ('move','p','photo','未处理','保留',3)")
        connection.execute("INSERT INTO photo_move_items VALUES ('move',0,'jpeg',?,?,1,2,0)", (str(tmp_path / "a.jpg"), str(tmp_path / "保留" / "a.jpg")))
    migrate_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("4",)
        assert connection.execute("SELECT inverse_of_json FROM photo_move_transactions").fetchone() == (None,)
        assert connection.execute("SELECT source_path,target_path FROM photo_move_items").fetchone() == (str(tmp_path / "a.jpg"), str(tmp_path / "保留" / "a.jpg"))
    assert Path(f"{database}.backup-v3").is_file()


@pytest.mark.parametrize("corruption", ["nested", "wrong_target", "wrong_fingerprint", "not_object", "boolean_ordinal"])
def test_corrupt_inverse_snapshot_fails_closed_before_recovery(qapp, tmp_path, corruption):
    store, before, service, forward = _committed_forward(qapp, tmp_path)
    try:
        inverse = service.prepare_inverse(forward)
        store.begin_photo_move(inverse)
        raw, = store._connection.execute("SELECT inverse_of_json FROM photo_move_transactions").fetchone()
        data = json.loads(raw)
        if corruption == "nested":
            data["inverse_of"] = json.loads(raw)
        elif corruption == "wrong_target":
            data["items"][0]["target_path"] = str(tmp_path / "unrelated" / "legacy.jpg")
        elif corruption == "wrong_fingerprint":
            data["items"][0]["size"] += 1
        elif corruption == "not_object":
            data = []
        elif corruption == "boolean_ordinal":
            data["items"][0]["ordinal"] = False
        store._connection.execute("UPDATE photo_move_transactions SET inverse_of_json=?", (json.dumps(data),))
        store._connection.commit()
        with pytest.raises(RuntimeError):
            store.list_pending_photo_moves()
        assert store.get_photo("legacy").mark == "待定"
        assert all(Path(item.target_path).is_file() for item in forward.items)
        assert all(not Path(asset.origin_path).exists() for asset in before.source_assets)
    finally:
        store.close()


def test_v3_unexpected_schema_is_not_published_as_v4(tmp_path):
    from src.xuanpian.store.schema import _VERSION_THREE_CREATE_TABLES_SQL, SchemaValidationError
    database = tmp_path / "v3.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(_VERSION_THREE_CREATE_TABLES_SQL)
        connection.execute("INSERT INTO metadata VALUES ('schema_version', '3')")
        connection.execute("ALTER TABLE photo_move_transactions ADD COLUMN surprise TEXT")
    with pytest.raises(SchemaValidationError):
        migrate_database(database)
    for candidate in (database, Path(f"{database}.backup-v3")):
        with sqlite3.connect(candidate) as connection:
            assert connection.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone() == ("3",)
            assert "inverse_of_json" not in {row[1] for row in connection.execute("PRAGMA table_info(photo_move_transactions)")}
