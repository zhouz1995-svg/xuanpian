"""Recoverable SQLite database migration tests."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.store import migrations as migrations_module
from src.xuanpian.store.migrations import Migration, migrate_database
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.store.schema import create_schema, validate_schema


_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _create_version_one_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
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
            INSERT INTO metadata (key, value)
            VALUES ('schema_version', '1');
            INSERT INTO projects (id, root_path)
            VALUES ('project', '/original');
            """
        )


def _create_version_two_database(path: Path) -> None:
    _create_version_one_database(path)
    migrate_database(path, target_version=2)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO photos (id, project_id, filename, mark)
            VALUES (?, ?, ?, ?)
            """,
            ("active", "project", "active.jpg", "入选"),
        )
        connection.execute(
            """
            INSERT INTO photo_assets (
                photo_id, kind, path, size, mtime_ns, present
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("active", "jpeg", "/original/active.jpg", 11, 12, 1),
        )
        connection.execute(
            """
            INSERT INTO removed_photos (id, project_id, filename, mark)
            VALUES (?, ?, ?, ?)
            """,
            ("removed", "project", "removed.nef", "淘汰"),
        )
        connection.execute(
            """
            INSERT INTO removed_photo_assets (
                photo_id, kind, path, size, mtime_ns, present
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("removed", "raw", "/original/removed.nef", 21, 22, 0),
        )


def _schema_version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("schema_version",),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _sidecars(path: Path) -> list[Path]:
    return [
        Path(f"{path}{suffix}")
        for suffix in _SQLITE_SIDECAR_SUFFIXES
        if Path(f"{path}{suffix}").exists()
    ]


def _migration_artifacts(database: Path) -> list[Path]:
    return list(database.parent.glob(f".{database.name}.migration-*"))


def test_migration_creates_pre_upgrade_backup_and_preserves_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def add_project_name(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE projects ADD COLUMN name TEXT")
        connection.execute(
            "UPDATE projects SET name = ? WHERE id = ?",
            ("Portfolio", "project"),
        )

    migrate_database(
        database,
        target_version=2,
        migrations=[Migration(version=2, apply=add_project_name)],
    )

    backup = Path(f"{database}.backup-v1")
    assert backup.is_file()
    assert _schema_version(backup) == 1
    with sqlite3.connect(backup) as connection:
        assert connection.execute(
            "SELECT root_path FROM projects WHERE id = ?",
            ("project",),
        ).fetchone() == ("/original",)
        assert "name" not in {
            row[1] for row in connection.execute("PRAGMA table_info(projects)")
        }

    assert _schema_version(database) == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT root_path, name FROM projects WHERE id = ?",
            ("project",),
        ).fetchone() == ("/original", "Portfolio")


def test_application_v1_to_current_migration_preserves_identity_mark_and_asset(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    source = tmp_path / "one.jpg"
    source.write_bytes(b"jpeg")
    _create_version_one_database(database)
    stat = source.stat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO photos (id, project_id, filename, mark)
            VALUES (?, ?, ?, ?)
            """,
            ("stable", "project", source.name, "入选"),
        )
        connection.execute(
            """
            INSERT INTO photo_assets (
                photo_id, kind, path, size, mtime_ns
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "stable",
                "jpeg",
                str(source.resolve()),
                stat.st_size,
                stat.st_mtime_ns,
            ),
        )

    with ProjectStore.open(database) as store:
        restored = store.get_photo("stable")
        assert restored is not None
        assert restored.mark == "入选"
        assert restored.jpeg is not None
        assert restored.jpeg.fingerprint.path == str(source.resolve())

    assert _schema_version(database) == 4
    assert _schema_version(Path(f"{database}.backup-v1")) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT present FROM photo_assets WHERE photo_id = ?",
            ("stable",),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT origin_path FROM photo_assets WHERE photo_id = ?",
            ("stable",),
        ).fetchone() == (str(source.resolve()),)


def test_application_v2_to_v3_migration_copies_paths_to_origins_and_matches_fresh_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_two_database(database)

    migrate_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT path, origin_path FROM photo_assets"
        ).fetchall() == [
            ("/original/active.jpg", "/original/active.jpg")
        ]
        assert connection.execute(
            "SELECT path, origin_path FROM removed_photo_assets"
        ).fetchall() == [
            ("/original/removed.nef", "/original/removed.nef")
        ]
        assert connection.execute(
            "SELECT dflt_value FROM pragma_table_info('photo_assets') "
            "WHERE name = 'origin_path'"
        ).fetchone() == (None,)
        assert connection.execute(
            "SELECT dflt_value FROM pragma_table_info('removed_photo_assets') "
            "WHERE name = 'origin_path'"
        ).fetchone() == (None,)
        validate_schema(connection)
        migrated_objects = connection.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()

    fresh = sqlite3.connect(":memory:")
    try:
        create_schema(fresh)
        fresh_objects = fresh.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
    finally:
        fresh.close()
    assert migrated_objects == fresh_objects
    assert _schema_version(Path(f"{database}.backup-v2")) == 2


def test_v2_to_v3_validation_failure_keeps_original_and_backup_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_two_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE INDEX unexpected_photo_mark ON photos(mark)"
        )
    original_bytes = database.read_bytes()

    with pytest.raises(RuntimeError, match="unexpected schema objects"):
        migrate_database(database)

    backup = Path(f"{database}.backup-v2")
    assert database.read_bytes() == original_bytes
    for candidate in (database, backup):
        assert _schema_version(candidate) == 2
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND name = 'unexpected_photo_mark'"
            ).fetchone() == ("unexpected_photo_mark",)
    assert not _migration_artifacts(database)


@pytest.mark.parametrize(
    ("table", "row_id"),
    [
        ("photo_assets", "active"),
        ("removed_photo_assets", "removed"),
    ],
)
@pytest.mark.parametrize(
    "damage",
    ["populated-column", "index", "trigger"],
)
def test_v2_to_v3_rejects_unknown_asset_schema_before_rebuild(
    tmp_path: Path,
    table: str,
    row_id: str,
    damage: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_two_database(database)
    object_name = f"external_{table}_{damage.replace('-', '_')}"
    with sqlite3.connect(database) as connection:
        before_user_version = int(
            connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if damage == "populated-column":
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN external_note TEXT"
            )
            connection.execute(
                f"UPDATE {table} SET external_note = ? WHERE photo_id = ?",
                (f"preserve-{table}", row_id),
            )
        elif damage == "index":
            connection.execute(
                f"CREATE INDEX {object_name} ON {table}(mtime_ns)"
            )
        else:
            connection.execute(
                f"""
                CREATE TRIGGER {object_name}
                AFTER UPDATE OF path ON {table}
                BEGIN
                    SELECT NEW.path;
                END
                """
            )
    original_bytes = database.read_bytes()

    with pytest.raises(RuntimeError, match="source schema"):
        migrate_database(database)

    backup = Path(f"{database}.backup-v2")
    assert database.read_bytes() == original_bytes
    for candidate in (database, backup):
        assert _schema_version(candidate) == 2
        with sqlite3.connect(candidate) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (
                before_user_version,
            )
            if damage == "populated-column":
                assert connection.execute(
                    f"SELECT external_note FROM {table} WHERE photo_id = ?",
                    (row_id,),
                ).fetchone() == (f"preserve-{table}",)
            else:
                assert connection.execute(
                    "SELECT type, name FROM sqlite_master WHERE name = ?",
                    (object_name,),
                ).fetchone() == (damage, object_name)
    assert not _migration_artifacts(database)


def test_application_migration_rejects_extra_schema_object_before_publish(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER erase_after_mark
            AFTER UPDATE OF mark ON photos
            BEGIN
                DELETE FROM photos WHERE id = NEW.id;
            END
            """
        )
    original_bytes = database.read_bytes()

    with pytest.raises(RuntimeError, match="unexpected schema objects"):
        ProjectStore.open(database)
    with pytest.raises(RuntimeError, match="unexpected schema objects"):
        ProjectStore.open(database)

    backup = Path(f"{database}.backup-v1")
    assert database.read_bytes() == original_bytes
    for candidate in (database, backup):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND name = 'erase_after_mark'"
            ).fetchone() == ("erase_after_mark",)
    assert not _migration_artifacts(database)


@pytest.mark.parametrize(
    ("object_type", "create_sql", "old_name", "sqlite_name"),
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
            "index",
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
def test_application_migration_rejects_sqlite_prefixed_disguised_objects(
    tmp_path: Path,
    object_type: str,
    create_sql: str,
    old_name: str,
    sqlite_name: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(create_sql)
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "UPDATE sqlite_master "
            "SET name = ?, "
            "tbl_name = CASE WHEN tbl_name = ? THEN ? ELSE tbl_name END, "
            "sql = replace(sql, ?, ?) "
            "WHERE type = ? AND name = ?",
            (
                sqlite_name,
                old_name,
                sqlite_name,
                old_name,
                sqlite_name,
                object_type,
                old_name,
            ),
        )
        connection.execute("PRAGMA writable_schema = OFF")
    original_bytes = database.read_bytes()

    with pytest.raises(RuntimeError, match="unexpected schema objects"):
        ProjectStore.open(database)
    with pytest.raises(RuntimeError, match="unexpected schema objects"):
        ProjectStore.open(database)

    backup = Path(f"{database}.backup-v1")
    assert database.read_bytes() == original_bytes
    for candidate in (database, backup):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = ? AND name = ?",
                (object_type, sqlite_name),
            ).fetchone() == (sqlite_name,)
    assert not _migration_artifacts(database)


def test_failed_migration_leaves_original_and_backup_openable_and_unchanged(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def fail_after_write(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE projects SET root_path = '/partial'")
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate_database(
            database,
            target_version=2,
            migrations=[Migration(version=2, apply=fail_after_write)],
        )

    backup = Path(f"{database}.backup-v1")
    for candidate in (database, backup):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                ("project",),
            ).fetchone() == ("/original",)


def test_missing_database_is_rejected_without_creating_a_file(
    tmp_path: Path,
) -> None:
    database = tmp_path / "missing.sqlite3"

    with pytest.raises(FileNotFoundError, match="does not exist"):
        migrate_database(database)

    assert not database.exists()


def test_database_without_metadata_is_rejected_without_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(RuntimeError, match="metadata"):
        migrate_database(database)

    assert not Path(f"{database}.backup-v1").exists()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'unrelated'"
        ).fetchone() == ("unrelated",)


@pytest.mark.parametrize("stored_version", ["not-an-integer", "-1", "01"])
def test_invalid_schema_version_is_rejected_clearly(
    tmp_path: Path,
    stored_version: str,
) -> None:
    database = tmp_path / "project.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            ("schema_version", stored_version),
        )

    with pytest.raises(RuntimeError, match="invalid schema_version"):
        migrate_database(database)


def test_target_older_than_database_is_rejected_before_backup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    with pytest.raises(RuntimeError, match="older than current version"):
        migrate_database(database, target_version=0)

    assert _schema_version(database) == 1
    assert not Path(f"{database}.backup-v1").exists()


@pytest.mark.parametrize(
    "migrations",
    [
        [Migration(version=3, apply=lambda _connection: None)],
        [
            Migration(version=3, apply=lambda _connection: None),
            Migration(version=2, apply=lambda _connection: None),
        ],
    ],
    ids=["gap", "out-of-order"],
)
def test_non_contiguous_migration_chain_is_rejected_before_backup(
    tmp_path: Path,
    migrations: list[Migration],
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    with pytest.raises(RuntimeError, match="continuous ascending"):
        migrate_database(database, target_version=3, migrations=migrations)

    assert _schema_version(database) == 1
    assert not Path(f"{database}.backup-v1").exists()


def test_migration_that_ends_transaction_is_rejected_without_replacing_original(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def commit_partial_write(connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE projects SET root_path = '/partial'")
        connection.commit()

    with pytest.raises(RuntimeError, match="ended the migration transaction"):
        migrate_database(
            database,
            target_version=2,
            migrations=[Migration(version=2, apply=commit_partial_write)],
        )

    for candidate in (database, Path(f"{database}.backup-v1")):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                ("project",),
            ).fetchone() == ("/original",)


def test_rollback_then_dml_cannot_replace_the_migration_transaction(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def create_version_two_table(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE version_two_data (value TEXT)")
        connection.execute(
            "INSERT INTO version_two_data (value) VALUES ('preserve me')"
        )

    def replace_transaction(connection: sqlite3.Connection) -> None:
        connection.rollback()
        connection.execute("UPDATE projects SET root_path = '/partial-v3'")

    with pytest.raises(RuntimeError, match="ended the migration transaction"):
        migrate_database(
            database,
            target_version=3,
            migrations=[
                Migration(version=2, apply=create_version_two_table),
                Migration(version=3, apply=replace_transaction),
            ],
        )

    for candidate in (database, Path(f"{database}.backup-v1")):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                ("project",),
            ).fetchone() == ("/original",)
            assert connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'version_two_data'"
            ).fetchone() is None


def test_callback_cannot_recreate_framework_savepoint_after_rollback(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def counterfeit_old_guard(connection: sqlite3.Connection) -> None:
        connection.rollback()
        connection.execute("SAVEPOINT xuanpian_migration_2_guard")
        connection.execute("UPDATE projects SET root_path = '/counterfeit'")

    with pytest.raises(RuntimeError, match="ended the migration transaction"):
        migrate_database(
            database,
            target_version=2,
            migrations=[Migration(version=2, apply=counterfeit_old_guard)],
        )

    for candidate in (database, Path(f"{database}.backup-v1")):
        assert _schema_version(candidate) == 1
        with sqlite3.connect(candidate) as connection:
            assert connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                ("project",),
            ).fetchone() == ("/original",)


def test_failed_post_replace_verification_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    writer = sqlite3.connect(database)
    assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    writer.execute(
        "UPDATE projects SET root_path = ? WHERE id = ?",
        ("/committed-in-wal", "project"),
    )
    writer.commit()
    assert Path(f"{database}-wal").is_file()
    assert Path(f"{database}-shm").is_file()
    read_schema_version = migrations_module._read_schema_version

    def fail_when_reopening_migrated_original(path: Path) -> int:
        version = read_schema_version(path)
        if path == database and version == 2:
            raise RuntimeError("injected verification failure")
        return version

    monkeypatch.setattr(
        migrations_module,
        "_read_schema_version",
        fail_when_reopening_migrated_original,
    )

    try:
        with pytest.raises(RuntimeError, match="injected verification failure"):
            migrate_database(
                database,
                target_version=2,
                migrations=[
                    Migration(
                        version=2,
                        apply=lambda connection: connection.execute(
                            "ALTER TABLE projects ADD COLUMN name TEXT"
                        ),
                    )
                ],
            )

        assert _sidecars(database) == []
        assert _migration_artifacts(database) == []
        for candidate in (database, Path(f"{database}.backup-v1")):
            assert _schema_version(candidate) == 1
            with sqlite3.connect(candidate) as connection:
                assert connection.execute("PRAGMA journal_mode").fetchone() == (
                    "delete",
                )
                assert connection.execute(
                    "SELECT root_path FROM projects WHERE id = ?",
                    ("project",),
                ).fetchone() == ("/committed-in-wal",)
    finally:
        writer.close()
    assert _sidecars(database) == []


def test_backup_includes_committed_wal_data(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    writer = sqlite3.connect(database)
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute(
            "UPDATE projects SET root_path = ? WHERE id = ?",
            ("/committed-in-wal", "project"),
        )
        writer.commit()
        assert Path(f"{database}-wal").is_file()
        assert Path(f"{database}-shm").is_file()

        migrate_database(
            database,
            target_version=2,
            migrations=[
                Migration(
                    version=2,
                    apply=lambda connection: connection.execute(
                        "ALTER TABLE projects ADD COLUMN name TEXT"
                    ),
                )
            ],
        )
        assert _sidecars(database) == []
        assert _migration_artifacts(database) == []
        assert _schema_version(database) == 2
    finally:
        writer.close()
    assert _sidecars(database) == []

    for candidate in (database, Path(f"{database}.backup-v1")):
        with sqlite3.connect(candidate) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == (
                "delete",
            )
            assert connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                ("project",),
            ).fetchone() == ("/committed-in-wal",)


def test_failed_wal_migration_removes_hidden_working_sidecars(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    writer = sqlite3.connect(database)
    held_working_connections: list[sqlite3.Connection] = []
    try:
        assert writer.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        writer.execute(
            "UPDATE projects SET root_path = ? WHERE id = ?",
            ("/committed-in-wal", "project"),
        )
        writer.commit()

        def fail_with_working_reader(connection: sqlite3.Connection) -> None:
            working_path = Path(
                connection.execute("PRAGMA database_list").fetchone()[2]
            )
            reader = sqlite3.connect(working_path)
            reader.execute("SELECT value FROM metadata").fetchone()
            held_working_connections.append(reader)
            connection.execute("UPDATE projects SET root_path = '/partial'")
            raise RuntimeError("injected WAL migration failure")

        with pytest.raises(RuntimeError, match="injected WAL migration failure"):
            migrate_database(
                database,
                target_version=2,
                migrations=[
                    Migration(version=2, apply=fail_with_working_reader)
                ],
            )

        assert _migration_artifacts(database) == []
        for candidate in (database, Path(f"{database}.backup-v1")):
            assert _schema_version(candidate) == 1
            with sqlite3.connect(candidate) as connection:
                assert connection.execute(
                    "SELECT root_path FROM projects WHERE id = ?",
                    ("project",),
                ).fetchone() == ("/committed-in-wal",)
    finally:
        for connection in held_working_connections:
            connection.close()
        writer.close()
    assert _migration_artifacts(database) == []


def test_live_store_blocks_migration_so_its_later_commit_is_not_lost(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    store = ProjectStore.open(database)
    record = PhotoRecord(
        id="one",
        filename="ONE.jpg",
        mark="",
        jpeg=PhotoAssetRecord(
            kind=AssetKind.JPEG,
            fingerprint=FileFingerprint(
                path=str((tmp_path / "ONE.jpg").resolve()),
                size=1,
                mtime_ns=1,
            ),
            origin_path=str((tmp_path / "ONE.jpg").resolve()),
        ),
        raw=None,
    )

    try:
        with pytest.raises(
            RuntimeError, match="already open.*close it and retry"
        ):
            migrate_database(
                database,
                target_version=2,
                migrations=[
                    Migration(
                        version=2,
                        apply=lambda connection: connection.execute(
                            "ALTER TABLE projects ADD COLUMN name TEXT"
                        ),
                    )
                ],
            )
        store.upsert_photos([record])
    finally:
        store.close()

    with ProjectStore.open(database) as reopened:
        assert reopened.list_photos() == [record]


def test_failed_migration_releases_database_lifecycle_lock(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)

    def fail_migration(_connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        migrate_database(
            database,
            target_version=2,
            migrations=[
                Migration(version=2, apply=fail_migration)
            ],
        )

    ProjectStore.open(database).close()


def test_migration_enables_foreign_keys_on_working_connection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    observed: list[int] = []

    def inspect_foreign_keys(connection: sqlite3.Connection) -> None:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
        assert row is not None
        observed.append(int(row[0]))
        connection.execute("ALTER TABLE projects ADD COLUMN name TEXT")

    migrate_database(
        database,
        target_version=2,
        migrations=[Migration(version=2, apply=inspect_foreign_keys)],
    )

    assert observed == [1]


def test_foreign_key_violation_prevents_migrated_database_publication(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO photos (id, project_id, filename, mark)
            VALUES ('orphan', 'missing-project', 'orphan.jpg', '')
            """
        )

    with pytest.raises(RuntimeError, match="foreign_key_check"):
        migrate_database(
            database,
            target_version=2,
            migrations=[
                Migration(
                    version=2,
                    apply=lambda connection: connection.execute(
                        "ALTER TABLE projects ADD COLUMN name TEXT"
                    ),
                )
            ],
        )

    assert _schema_version(database) == 1
    assert _schema_version(Path(f"{database}.backup-v1")) == 1


def test_failed_quick_check_prevents_migrated_database_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "project.sqlite3"
    _create_version_one_database(database)
    monkeypatch.setattr(
        migrations_module,
        "_quick_check_rows",
        lambda _connection: [("database disk image is malformed",)],
        raising=False,
    )

    with pytest.raises(RuntimeError, match="quick_check"):
        migrate_database(
            database,
            target_version=2,
            migrations=[
                Migration(
                    version=2,
                    apply=lambda connection: connection.execute(
                        "ALTER TABLE projects ADD COLUMN name TEXT"
                    ),
                )
            ],
        )

    assert _schema_version(database) == 1
    assert _schema_version(Path(f"{database}.backup-v1")) == 1


def test_hardlink_alias_cannot_migrate_and_releases_its_path_lock(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    alias = tmp_path / "project-alias.sqlite3"
    _create_version_one_database(database)
    os.link(database, alias)
    migration = Migration(
        version=2,
        apply=lambda connection: connection.execute(
            "ALTER TABLE projects ADD COLUMN name TEXT"
        ),
    )

    with pytest.raises(RuntimeError, match="multiple hard links"):
        migrate_database(alias, target_version=2, migrations=[migration])

    assert _schema_version(database) == 1
    assert _schema_version(alias) == 1
    assert not Path(f"{alias}.backup-v1").exists()

    database.unlink()
    migrate_database(alias, target_version=2, migrations=[migration])
    assert _schema_version(alias) == 2
