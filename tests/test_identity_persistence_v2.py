from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.services.import_service import (
    PhotoCandidate,
    candidate_to_photo_record,
)
from src.xuanpian.store import project_store, schema
from src.xuanpian.store.project_store import PhotoReconciliation, ProjectStore


def _candidate(
    photo_id: str,
    *,
    jpeg: Path | None = None,
    raw: Path | None = None,
    superseded: tuple[str, ...] = (),
) -> PhotoCandidate:
    source = jpeg or raw
    assert source is not None
    return PhotoCandidate(
        photo_id=photo_id,
        display_name=source.name,
        jpeg_path="" if jpeg is None else str(jpeg.resolve()),
        raw_path="" if raw is None else str(raw.resolve()),
        superseded_photo_ids=superseded,
    )


def test_all_assets_missing_then_restored_reuses_identity_mark_and_invalidates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "one.jpg"
    source.write_bytes(b"original")
    database = tmp_path / "project.sqlite3"
    original = candidate_to_photo_record(_candidate("stable", jpeg=source))

    with ProjectStore.open(database, root_path=tmp_path) as store:
        store.upsert_photos((original,))
        store.save_mark("stable", "入选")
        store.save_analysis(
            "stable",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 42},
            None,
        )
        source.unlink()
        assert store.upsert_photos((replace(original, jpeg=None),)) == {
            "stable": "missing"
        }
        assert store.photo_ids_by_asset_paths((str(source),)) == {
            str(source.resolve()): "stable"
        }
        assert store.get_photo("stable") == replace(
            original,
            mark="入选",
            jpeg=None,
        )

    source.write_bytes(b"restored with a new fingerprint")
    with ProjectStore.open(database, root_path=tmp_path) as store:
        restored_id = store.photo_ids_by_asset_paths((str(source),))[
            str(source.resolve())
        ]
        restored = candidate_to_photo_record(
            _candidate(restored_id, jpeg=source)
        )
        assert store.upsert_photos((restored,)) == {
            "stable": "changed"
        }
        assert store.get_photo("stable").mark == "入选"
        assert (
            store.get_analysis("stable", "blur").status
            is AnalysisStatus.PENDING
        )


def test_project_removal_tombstone_survives_reopen_and_reinclude_is_explicit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "one.jpg"
    raw = tmp_path / "one.nef"
    source.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    database = tmp_path / "project.sqlite3"
    record = candidate_to_photo_record(
        _candidate("stable", jpeg=source, raw=raw)
    )

    with ProjectStore.open(database, root_path=tmp_path) as store:
        store.upsert_photos((record,))
        store.save_mark("stable", "入选")
        store.delete_photos(("stable",))
        assert store.get_photo("stable") is None
        assert store.removed_asset_paths((str(source), str(raw))) == {
            str(source.resolve()),
            str(raw.resolve()),
        }

    with ProjectStore.open(database, root_path=tmp_path) as store:
        assert store.removed_asset_paths((str(source), str(raw))) == {
            str(source.resolve()),
            str(raw.resolve()),
        }
        store.reinclude_photos(("stable",))
        assert store.removed_asset_paths((str(source), str(raw))) == set()
        restored = store.get_photo("stable")
        assert restored is not None
        assert restored.id == "stable"
        assert restored.mark == "入选"
        assert restored.jpeg is not None
        assert restored.raw is not None


@pytest.mark.parametrize("survivor_kind", ["jpeg", "raw"])
def test_pair_merge_preserves_unique_nonempty_mark_regardless_of_scan_order(
    tmp_path: Path,
    survivor_kind: str,
) -> None:
    jpeg = tmp_path / "pair.jpg"
    raw = tmp_path / "pair.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    jpeg_record = candidate_to_photo_record(
        _candidate("jpeg-id", jpeg=jpeg),
        mark="入选",
    )
    raw_record = candidate_to_photo_record(_candidate("raw-id", raw=raw))
    survivor_id = f"{survivor_kind}-id"
    superseded_id = "raw-id" if survivor_kind == "jpeg" else "jpeg-id"
    paired = candidate_to_photo_record(
        _candidate(
            survivor_id,
            jpeg=jpeg,
            raw=raw,
            superseded=(superseded_id,),
        )
    )

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos((jpeg_record, raw_record))
        results = store.reconcile_photos_with_marks(
            (
                PhotoReconciliation(
                    paired,
                    (superseded_id,),
                ),
            )
        )
        assert results[0].resolved_mark == "入选"
        assert store.get_photo(survivor_id).mark == "入选"
        assert store.get_photo(superseded_id) is None


def test_pair_merge_accepts_matching_marks_but_conflict_is_atomic(
    tmp_path: Path,
) -> None:
    paths: dict[str, tuple[Path, Path]] = {}
    for stem in ("same", "conflict", "earlier"):
        jpeg = tmp_path / f"{stem}.jpg"
        raw = tmp_path / f"{stem}.nef"
        jpeg.write_bytes(f"{stem}-jpeg".encode())
        raw.write_bytes(f"{stem}-raw".encode())
        paths[stem] = (jpeg, raw)

    def single(photo_id: str, path: Path, mark: str):
        return candidate_to_photo_record(
            _candidate(
                photo_id,
                jpeg=path if path.suffix == ".jpg" else None,
                raw=path if path.suffix == ".nef" else None,
            ),
            mark=mark,
        )

    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store.upsert_photos(
            (
                single("same-j", paths["same"][0], "入选"),
                single("same-r", paths["same"][1], "入选"),
                single("conflict-j", paths["conflict"][0], "入选"),
                single("conflict-r", paths["conflict"][1], "淘汰"),
                single("earlier-j", paths["earlier"][0], ""),
                single("earlier-r", paths["earlier"][1], "待定"),
            )
        )
        same_pair = candidate_to_photo_record(
            _candidate(
                "same-j",
                jpeg=paths["same"][0],
                raw=paths["same"][1],
                superseded=("same-r",),
            )
        )
        store.reconcile_photo(
            same_pair,
            superseded_photo_ids=("same-r",),
        )
        assert store.get_photo("same-j").mark == "入选"

        earlier_pair = candidate_to_photo_record(
            _candidate(
                "earlier-j",
                jpeg=paths["earlier"][0],
                raw=paths["earlier"][1],
                superseded=("earlier-r",),
            )
        )
        conflict_pair = candidate_to_photo_record(
            _candidate(
                "conflict-j",
                jpeg=paths["conflict"][0],
                raw=paths["conflict"][1],
                superseded=("conflict-r",),
            )
        )
        with pytest.raises(project_store.PhotoMarkConflictError):
            store.reconcile_photos(
                (
                    PhotoReconciliation(
                        earlier_pair,
                        ("earlier-r",),
                    ),
                    PhotoReconciliation(
                        conflict_pair,
                        ("conflict-r",),
                    ),
                )
            )

        assert store.get_photo("earlier-j").raw is None
        assert store.get_photo("earlier-r").mark == "待定"
        assert store.get_photo("conflict-j").mark == "入选"
        assert store.get_photo("conflict-r").mark == "淘汰"


def test_pair_merge_canonicalizes_equivalent_legacy_and_current_marks(
    tmp_path: Path,
) -> None:
    jpeg = tmp_path / "equivalent.jpg"
    raw = tmp_path / "equivalent.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    jpeg_record = candidate_to_photo_record(
        _candidate("jpeg-id", jpeg=jpeg),
        mark="入选",
    )
    raw_record = candidate_to_photo_record(
        _candidate("raw-id", raw=raw),
        mark="保留",
    )
    paired = candidate_to_photo_record(
        _candidate(
            "jpeg-id",
            jpeg=jpeg,
            raw=raw,
            superseded=("raw-id",),
        )
    )

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos((jpeg_record, raw_record))

        store.reconcile_photo(paired, superseded_photo_ids=("raw-id",))

        assert store.get_photo("jpeg-id").mark == "保留"
        assert store.get_photo("raw-id") is None


def test_pair_merge_rejects_equal_unknown_marks_instead_of_equating_them(
    tmp_path: Path,
) -> None:
    jpeg = tmp_path / "unknown.jpg"
    raw = tmp_path / "unknown.nef"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    records = (
        candidate_to_photo_record(_candidate("jpeg-id", jpeg=jpeg), mark="future"),
        candidate_to_photo_record(_candidate("raw-id", raw=raw), mark="future"),
    )
    paired = candidate_to_photo_record(
        _candidate(
            "jpeg-id",
            jpeg=jpeg,
            raw=raw,
            superseded=("raw-id",),
        )
    )

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos(records)

        with pytest.raises(project_store.PhotoMarkConflictError, match="unmanaged"):
            store.reconcile_photo(paired, superseded_photo_ids=("raw-id",))

        assert store.get_photo("jpeg-id").mark == "future"
        assert store.get_photo("raw-id").mark == "future"


@pytest.mark.parametrize(
    "damage_sql",
    [
        "DROP TABLE photo_assets",
        "DROP INDEX idx_photo_assets_path",
    ],
)
def test_current_version_database_damage_fails_closed_and_releases_lock(
    tmp_path: Path,
    damage_sql: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(damage_sql)
    damaged_bytes = database.read_bytes()

    with pytest.raises(schema.SchemaValidationError):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError):
        ProjectStore.open(database)
    assert database.read_bytes() == damaged_bytes


@pytest.mark.parametrize(
    ("object_kind", "create_sql"),
    [
        (
            "destructive trigger",
            """
            CREATE TRIGGER erase_after_mark
            AFTER UPDATE OF mark ON photos
            BEGIN
                DELETE FROM photos WHERE id = NEW.id;
            END
            """,
        ),
        (
            "benign trigger",
            """
            CREATE TRIGGER observe_mark
            AFTER UPDATE OF mark ON photos
            BEGIN
                SELECT NEW.mark;
            END
            """,
        ),
        (
            "view",
            "CREATE VIEW photo_names AS SELECT id, filename FROM photos",
        ),
        (
            "explicit index",
            "CREATE INDEX extra_photo_mark_index ON photos(mark)",
        ),
        (
            "extra table",
            "CREATE TABLE application_extension (value TEXT)",
        ),
    ],
)
def test_current_v2_rejects_extra_sqlite_master_objects_without_side_effects(
    tmp_path: Path,
    object_kind: str,
    create_sql: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(create_sql)
    damaged_bytes = database.read_bytes()
    damaged_mtime_ns = database.stat().st_mtime_ns
    before_names = {path.name for path in tmp_path.iterdir()}

    with pytest.raises(schema.SchemaValidationError, match="unexpected"):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError, match="unexpected"):
        ProjectStore.open(database)

    assert database.read_bytes() == damaged_bytes, object_kind
    assert database.stat().st_mtime_ns == damaged_mtime_ns, object_kind
    assert {path.name for path in tmp_path.iterdir()} == before_names


def test_current_v2_accepts_canonical_sqlite_autoindexes(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()

    with sqlite3.connect(database) as connection:
        autoindexes = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND name LIKE 'sqlite_autoindex_%'"
        ).fetchall()

    assert autoindexes
    assert all(sql is None for _name, sql in autoindexes)
    ProjectStore.open(database).close()


@pytest.mark.parametrize(
    ("object_kind", "create_sql", "old_name", "sqlite_name"),
    [
        (
            "trigger",
            """
            CREATE TRIGGER user_trigger
            AFTER UPDATE OF mark ON photos
            BEGIN
                DELETE FROM photos WHERE id = NEW.id;
            END
            """,
            "user_trigger",
            "sqlite_evil_trigger",
        ),
        (
            "view",
            "CREATE VIEW user_view AS SELECT id FROM photos",
            "user_view",
            "sqlite_evil_view",
        ),
        (
            "explicit index",
            "CREATE INDEX user_index ON photos(mark)",
            "user_index",
            "sqlite_evil_index",
        ),
        (
            "table",
            "CREATE TABLE user_table (value TEXT)",
            "user_table",
            "sqlite_evil_table",
        ),
    ],
)
def test_current_v2_rejects_user_objects_disguised_with_sqlite_prefix(
    tmp_path: Path,
    object_kind: str,
    create_sql: str,
    old_name: str,
    sqlite_name: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(create_sql)
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master "
            "SET name = ?, "
            "tbl_name = CASE WHEN tbl_name = ? THEN ? ELSE tbl_name END, "
            "sql = replace(sql, ?, ?) "
            "WHERE name = ?",
            (
                sqlite_name,
                old_name,
                sqlite_name,
                old_name,
                sqlite_name,
                old_name,
            ),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    damaged_bytes = database.read_bytes()

    with pytest.raises(schema.SchemaValidationError, match="unexpected"):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError, match="unexpected"):
        ProjectStore.open(database)
    assert database.read_bytes() == damaged_bytes, object_kind


def test_current_v2_rejects_noncanonical_sqlite_statistics_objects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute("ANALYZE")
        internal_objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'"
        ).fetchall()
    analyzed_bytes = database.read_bytes()

    assert internal_objects
    with pytest.raises(schema.SchemaValidationError, match="sqlite_stat"):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError, match="sqlite_stat"):
        ProjectStore.open(database)
    assert database.read_bytes() == analyzed_bytes


def test_current_version_database_missing_required_column_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE photo_assets RENAME TO old_assets")
        connection.execute(
            """
            CREATE TABLE photo_assets (
                photo_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                PRIMARY KEY (photo_id, kind)
            )
            """
        )

    with pytest.raises(schema.SchemaValidationError, match="photo_assets"):
        ProjectStore.open(database)


def test_current_version_database_rejects_wrong_required_index_definition(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_photo_assets_path")
        connection.execute(
            "CREATE INDEX idx_photo_assets_path ON photo_assets(kind)"
        )

    with pytest.raises(schema.SchemaValidationError, match="index"):
        ProjectStore.open(database)


def test_current_version_database_rejects_required_indexes_on_wrong_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_photo_assets_path")
        connection.execute("DROP INDEX idx_removed_photo_assets_path")
        connection.execute(
            """
            CREATE INDEX idx_photo_assets_path
            ON removed_photo_assets(path)
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_removed_photo_assets_path
            ON photo_assets(path)
            """
        )

    with pytest.raises(schema.SchemaValidationError, match="index"):
        ProjectStore.open(database)


@pytest.mark.parametrize("damage", ["unique", "partial"])
def test_current_version_database_rejects_wrong_index_flags(
    tmp_path: Path,
    damage: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_photo_assets_path")
        qualifier = "UNIQUE " if damage == "unique" else ""
        predicate = " WHERE present = 1" if damage == "partial" else ""
        connection.execute(
            f"""
            CREATE {qualifier}INDEX idx_photo_assets_path
            ON photo_assets(path){predicate}
            """
        )

    with pytest.raises(schema.SchemaValidationError, match="index"):
        ProjectStore.open(database)


@pytest.mark.parametrize("damage", ["missing-primary-key", "nullable-status"])
def test_current_version_database_rejects_wrong_column_contract(
    tmp_path: Path,
    damage: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "ALTER TABLE analysis_results RENAME TO old_analysis_results"
        )
        primary_key = (
            ""
            if damage == "missing-primary-key"
            else ", PRIMARY KEY (photo_id, analyzer)"
        )
        status_nullability = (
            "" if damage == "nullable-status" else " NOT NULL"
        )
        connection.execute(
            f"""
            CREATE TABLE analysis_results (
                photo_id TEXT NOT NULL,
                analyzer TEXT NOT NULL,
                status TEXT{status_nullability},
                result_json TEXT
                {primary_key},
                FOREIGN KEY (photo_id) REFERENCES photos(id)
                    ON DELETE CASCADE
            )
            """
        )

    with pytest.raises(schema.SchemaValidationError, match="analysis_results"):
        ProjectStore.open(database)


def _rewrite_table_declaration(
    database: Path,
    table: str,
    rewrite: Callable[[str], str],
) -> None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        assert row is not None
        original = str(row[0])
        damaged = rewrite(original)
        assert damaged != original
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = ? "
            "WHERE type = 'table' AND name = ?",
            (damaged, table),
        )
        connection.execute("PRAGMA writable_schema = OFF")


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "",
        ),
        (
            "photo_assets",
            "CHECK (present IN (0, 1))",
            "",
        ),
        (
            "removed_photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "",
        ),
        (
            "removed_photo_assets",
            "CHECK (present IN (0, 1))",
            "",
        ),
        (
            "analysis_results",
            "CHECK (\n        status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled')\n    )",
            "",
        ),
        (
            "analysis_results",
            "CHECK (\n        status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled')\n    )",
            "CHECK (status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled', 'other'))",
        ),
        (
            "photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "CHECK (+kind IN ('jpeg', 'raw'))",
        ),
    ],
)
def test_current_v2_rejects_missing_or_widened_check_constraints(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        table,
        lambda sql: sql.replace(old, new),
    )
    damaged_bytes = database.read_bytes()

    with pytest.raises(schema.SchemaValidationError, match=table):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError, match=table):
        ProjectStore.open(database)
    assert database.read_bytes() == damaged_bytes


def test_current_v2_rejects_constraint_moved_to_another_table(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    check = "CHECK (present IN (0, 1))"
    _rewrite_table_declaration(
        database,
        "photo_assets",
        lambda sql: sql.replace(check, ""),
    )
    _rewrite_table_declaration(
        database,
        "removed_photo_assets",
        lambda sql: sql.replace(check, f"{check} {check}", 1),
    )

    with pytest.raises(schema.SchemaValidationError, match="photo_assets"):
        ProjectStore.open(database)


def test_current_v2_check_contract_ignores_case_and_whitespace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        "photo_assets",
        lambda sql: sql.replace(
            "CHECK (kind IN ('jpeg', 'raw'))",
            "check( KIND in( 'jpeg' ,\n'raw' ) )",
        ),
    )

    store = ProjectStore.open(database)
    store.close()


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "CHECK (kind IN ('jpeg', 'raw') OR quote(kind) IS NOT NULL)",
        ),
        (
            "photo_assets",
            "CHECK (present IN (0, 1))",
            "CHECK (present IN (0, 1) OR +1)",
        ),
        (
            "removed_photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "CHECK (kind IN ('jpeg', 'raw') OR quote(kind) IS NOT NULL)",
        ),
        (
            "removed_photo_assets",
            "CHECK (present IN (0, 1))",
            "CHECK (present IN (0, 1) OR +1)",
        ),
        (
            "analysis_results",
            "CHECK (\n        status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled')\n    )",
            "CHECK (status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled') OR +1)",
        ),
    ],
)
def test_semantic_probe_rejects_relaxed_checks_even_without_lexical_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        table,
        lambda sql: sql.replace(old, new),
    )
    monkeypatch.setattr(
        schema,
        "_validate_table_contracts",
        lambda _connection: None,
    )

    with pytest.raises(schema.SchemaValidationError, match=table):
        ProjectStore.open(database)


def test_semantic_check_probes_leave_legal_database_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    before = database.read_bytes()
    before_mtime_ns = database.stat().st_mtime_ns
    before_files = {path.name for path in tmp_path.iterdir()}

    with sqlite3.connect(database) as connection:
        before_rows = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in (
                "projects",
                "photos",
                "photo_assets",
                "analysis_results",
                "removed_photos",
                "removed_photo_assets",
            )
        }
        schema.validate_schema(connection)
        after_rows = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in before_rows
        }

    assert after_rows == before_rows
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime_ns
    assert {path.name for path in tmp_path.iterdir()} == before_files

    # The migration preflight uses a writable connection for the same
    # rollback-only probes; reopening must not publish bytes or sidecars.
    ProjectStore.open(database).close()
    assert database.read_bytes() == before
    assert database.stat().st_mtime_ns == before_mtime_ns
    assert {path.name for path in tmp_path.iterdir()} == before_files


def test_semantic_probe_error_rolls_back_and_project_lock_is_released(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_schema_probe
            BEFORE INSERT ON projects
            WHEN NEW.id LIKE '__schema_check_probe_%'
            BEGIN
                SELECT RAISE(ABORT, 'probe blocked');
            END
            """
        )
    damaged_bytes = database.read_bytes()

    with pytest.raises(schema.SchemaValidationError, match="probe"):
        ProjectStore.open(database)
    with pytest.raises(schema.SchemaValidationError, match="probe"):
        ProjectStore.open(database)
    assert database.read_bytes() == damaged_bytes


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "photo_assets",
            "kind TEXT NOT NULL CHECK",
            "kind TEXT COLLATE RTRIM NOT NULL CHECK",
        ),
        (
            "removed_photo_assets",
            "kind TEXT NOT NULL CHECK",
            "kind TEXT COLLATE RTRIM NOT NULL CHECK",
        ),
        (
            "analysis_results",
            "status TEXT NOT NULL CHECK",
            "status TEXT COLLATE RTRIM NOT NULL CHECK",
        ),
    ],
)
def test_semantic_probe_rejects_collation_that_widens_enum_checks(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        table,
        lambda sql: sql.replace(old, new),
    )

    with pytest.raises(schema.SchemaValidationError, match=table):
        ProjectStore.open(database)


@pytest.mark.parametrize(
    ("table", "old", "new"),
    [
        (
            "photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "CHECK (kind <> 'jpeg ' AND "
            '(1 OR "CHECK"(kind IN (\'jpeg\', \'raw\'))))',
        ),
        (
            "photo_assets",
            "CHECK (present IN (0, 1))",
            "CHECK (present <> 2 AND "
            '(1 OR "CHECK"(present IN (0, 1))))',
        ),
        (
            "removed_photo_assets",
            "CHECK (kind IN ('jpeg', 'raw'))",
            "CHECK (kind <> 'jpeg ' AND "
            '(1 OR "CHECK"(kind IN (\'jpeg\', \'raw\'))))',
        ),
        (
            "removed_photo_assets",
            "CHECK (present IN (0, 1))",
            "CHECK (present <> 2 AND "
            '(1 OR "CHECK"(present IN (0, 1))))',
        ),
        (
            "analysis_results",
            "CHECK (\n        status IN "
            "('pending', 'running', 'succeeded', 'failed', 'canceled')\n    )",
            "CHECK (status <> 'pending ' AND "
            '(1 OR "CHECK"(status IN '
            "('pending', 'running', 'succeeded', 'failed', 'canceled'))))",
        ),
    ],
)
def test_full_table_contract_rejects_quoted_check_function_bypass(
    tmp_path: Path,
    table: str,
    old: str,
    new: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        table,
        lambda sql: sql.replace(old, new),
    )

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.create_function("CHECK", 1, lambda _value: 1)
        with pytest.raises(schema.SchemaValidationError, match=table):
            schema.validate_schema(connection)


@pytest.mark.parametrize(
    "table",
    [
        "metadata",
        "projects",
        "photos",
        "photo_assets",
        "analysis_results",
        "analysis_failures",
        "removed_photos",
        "removed_photo_assets",
    ],
)
def test_full_table_contract_ignores_keyword_case_and_whitespace(
    tmp_path: Path,
    table: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        table,
        lambda sql: " \n ".join(sql.lower().split()),
    )

    store = ProjectStore.open(database)
    store.close()


def test_full_table_contract_rejects_other_structural_constraints(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    ProjectStore.open(database).close()
    _rewrite_table_declaration(
        database,
        "photos",
        lambda sql: sql.replace(
            "FOREIGN KEY (project_id)",
            "CHECK (length(filename) >= 0), "
            "FOREIGN KEY (project_id)",
        ),
    )

    with pytest.raises(schema.SchemaValidationError, match="photos"):
        ProjectStore.open(database)
