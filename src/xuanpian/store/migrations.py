"""Recoverable, file-backed SQLite database migrations."""

from __future__ import annotations

import os
import secrets
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence

from .database_lock import ProjectDatabaseLock
from .schema import (
    SCHEMA_VERSION,
    SchemaValidationError,
    _schema_tokens,
    _VERSION_THREE_CREATE_TABLES_SQL,
    validate_schema,
)


@dataclass(frozen=True, slots=True)
class Migration:
    """One schema change that advances a database to ``version``."""

    version: int
    apply: Callable[[sqlite3.Connection], None]


_VERSION_ONE_SCHEMA_SQL = """
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
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE photo_assets (
    photo_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    PRIMARY KEY (photo_id, kind),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
);

CREATE TABLE analysis_results (
    photo_id TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'canceled')
    ),
    result_json TEXT,
    PRIMARY KEY (photo_id, analyzer),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
);

CREATE TABLE analysis_failures (
    photo_id TEXT NOT NULL,
    analyzer TEXT NOT NULL,
    message TEXT NOT NULL,
    exception_type TEXT NOT NULL,
    PRIMARY KEY (photo_id, analyzer),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
);
"""


@dataclass(frozen=True, slots=True)
class _SourceSchemaContract:
    declared_objects: tuple[tuple[object, ...], ...]
    opaque_objects: tuple[tuple[str, str, str], ...]
    columns: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]
    indexes: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]
    foreign_keys: tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_schema_contract(
    connection: sqlite3.Connection,
) -> _SourceSchemaContract:
    master_rows = tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        )
    )
    declared_objects = tuple(
        sorted(
            (
                str(object_type),
                str(name),
                str(table_name),
                _schema_tokens(str(sql)),
            )
            for object_type, name, table_name, sql in master_rows
            if sql is not None
        )
    )
    opaque_objects = tuple(
        sorted(
            (str(object_type), str(name), str(table_name))
            for object_type, name, table_name, sql in master_rows
            if sql is None
            and not str(name).lower().startswith("sqlite_autoindex_")
        )
    )
    table_names = sorted(
        str(name)
        for object_type, name, _table_name, sql in master_rows
        if str(object_type) == "table" and sql is not None
    )
    columns: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    indexes: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    foreign_keys: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
    for table in table_names:
        quoted_table = _quoted_identifier(table)
        columns.append(
            (
                table,
                tuple(
                    (
                        int(cid),
                        str(name),
                        str(data_type).upper(),
                        int(not_null),
                        None if default is None else str(default),
                        int(primary_key_position),
                    )
                    for (
                        cid,
                        name,
                        data_type,
                        not_null,
                        default,
                        primary_key_position,
                    ) in connection.execute(
                        f"PRAGMA table_info({quoted_table})"
                    )
                ),
            )
        )
        table_indexes: list[tuple[object, ...]] = []
        for row in connection.execute(f"PRAGMA index_list({quoted_table})"):
            index_name = str(row[1])
            quoted_index = _quoted_identifier(index_name)
            table_indexes.append(
                (
                    index_name,
                    int(row[2]),
                    str(row[3]),
                    int(row[4]),
                    tuple(
                        (
                            int(index_column[0]),
                            int(index_column[1]),
                            None
                            if index_column[2] is None
                            else str(index_column[2]),
                            int(index_column[3]),
                            str(index_column[4]),
                            int(index_column[5]),
                        )
                        for index_column in connection.execute(
                            f"PRAGMA index_xinfo({quoted_index})"
                        )
                    ),
                )
            )
        indexes.append((table, tuple(sorted(table_indexes))))
        foreign_keys.append(
            (
                table,
                tuple(
                    sorted(
                        (
                            int(row[0]),
                            int(row[1]),
                            str(row[2]),
                            str(row[3]),
                            str(row[4]),
                            str(row[5]).upper(),
                            str(row[6]).upper(),
                            str(row[7]).upper(),
                        )
                        for row in connection.execute(
                            f"PRAGMA foreign_key_list({quoted_table})"
                        )
                    )
                ),
            )
        )
    return _SourceSchemaContract(
        declared_objects=declared_objects,
        opaque_objects=opaque_objects,
        columns=tuple(columns),
        indexes=tuple(indexes),
        foreign_keys=tuple(foreign_keys),
    )


def _apply_version_two_schema_changes(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        ALTER TABLE photo_assets
        ADD COLUMN present INTEGER NOT NULL DEFAULT 1
            CHECK (present IN (0, 1))
        """
    )
    connection.execute(
        "CREATE INDEX idx_photo_assets_path ON photo_assets(path)"
    )
    connection.execute(
        """
        CREATE TABLE removed_photos (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            mark TEXT NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE removed_photo_assets (
            photo_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            present INTEGER NOT NULL CHECK (present IN (0, 1)),
            PRIMARY KEY (photo_id, kind),
            FOREIGN KEY (photo_id) REFERENCES removed_photos(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX idx_removed_photo_assets_path
            ON removed_photo_assets(path)
        """
    )


@lru_cache(maxsize=3)
def _expected_source_schema_contract(version: int) -> _SourceSchemaContract:
    reference = sqlite3.connect(":memory:")
    try:
        if version == 3:
            reference.executescript(_VERSION_THREE_CREATE_TABLES_SQL)
            return _source_schema_contract(reference)
        reference.executescript(_VERSION_ONE_SCHEMA_SQL)
        if version == 2:
            _apply_version_two_schema_changes(reference)
        elif version != 1:
            raise ValueError(f"unsupported source schema version: {version}")
        return _source_schema_contract(reference)
    finally:
        reference.close()


def _validate_source_schema(
    connection: sqlite3.Connection,
    version: int,
) -> None:
    expected = _expected_source_schema_contract(version)
    actual = _source_schema_contract(connection)
    expected_object_keys = {
        (row[0], row[1], row[2]) for row in expected.declared_objects
    }
    actual_object_keys = {
        (row[0], row[1], row[2]) for row in actual.declared_objects
    }
    if (
        actual_object_keys != expected_object_keys
        or actual.opaque_objects != expected.opaque_objects
    ):
        raise SchemaValidationError(
            f"version {version} source schema has unexpected schema objects"
        )
    if actual.columns != expected.columns:
        raise SchemaValidationError(
            f"version {version} source schema has unexpected column contracts"
        )
    if actual.indexes != expected.indexes:
        raise SchemaValidationError(
            f"version {version} source schema has unexpected index contracts"
        )
    if actual.foreign_keys != expected.foreign_keys:
        raise SchemaValidationError(
            f"version {version} source schema has unexpected foreign keys"
        )
    if actual.declared_objects != expected.declared_objects:
        raise SchemaValidationError(
            f"version {version} source schema has unexpected declarations"
        )
    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        raise SchemaValidationError(
            "version "
            f"{version} source schema foreign_key_check failed: "
            f"{foreign_key_errors!r}"
        )
    quick_check_rows = _quick_check_rows(connection)
    if quick_check_rows != [("ok",)]:
        raise SchemaValidationError(
            f"version {version} source schema quick_check failed: "
            f"{quick_check_rows!r}"
        )


def _migrate_to_version_two(connection: sqlite3.Connection) -> None:
    _validate_source_schema(connection, 1)
    _apply_version_two_schema_changes(connection)


def _migrate_to_version_three(connection: sqlite3.Connection) -> None:
    _validate_source_schema(connection, 2)
    connection.execute(
        """
        CREATE TABLE photo_assets_v3 (
            photo_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
            origin_path TEXT NOT NULL,
            PRIMARY KEY (photo_id, kind),
            FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO photo_assets_v3 (
            photo_id, kind, path, size, mtime_ns, present, origin_path
        )
        SELECT photo_id, kind, path, size, mtime_ns, present, path
        FROM photo_assets
        """
    )
    connection.execute("DROP TABLE photo_assets")
    connection.execute(
        "ALTER TABLE photo_assets_v3 RENAME TO photo_assets"
    )
    connection.execute(
        "CREATE INDEX idx_photo_assets_path ON photo_assets(path)"
    )

    connection.execute(
        """
        CREATE TABLE removed_photo_assets_v3 (
            photo_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            present INTEGER NOT NULL CHECK (present IN (0, 1)),
            origin_path TEXT NOT NULL,
            PRIMARY KEY (photo_id, kind),
            FOREIGN KEY (photo_id) REFERENCES removed_photos(id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        INSERT INTO removed_photo_assets_v3 (
            photo_id, kind, path, size, mtime_ns, present, origin_path
        )
        SELECT photo_id, kind, path, size, mtime_ns, present, path
        FROM removed_photo_assets
        """
    )
    connection.execute("DROP TABLE removed_photo_assets")
    connection.execute(
        "ALTER TABLE removed_photo_assets_v3 RENAME TO removed_photo_assets"
    )
    connection.execute(
        """
        CREATE INDEX idx_removed_photo_assets_path
            ON removed_photo_assets(path)
        """
    )

    connection.execute(
        """
        CREATE TABLE project_settings (
            project_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (project_id, key),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE photo_move_transactions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            photo_id TEXT NOT NULL UNIQUE,
            before_mark TEXT NOT NULL,
            after_mark TEXT NOT NULL,
            created_at_ns INTEGER NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE photo_move_items (
            transaction_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
            source_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0
                CHECK (completed IN (0, 1)),
            PRIMARY KEY (transaction_id, ordinal),
            FOREIGN KEY (transaction_id)
                REFERENCES photo_move_transactions(id) ON DELETE CASCADE
        )
        """
    )


def _migrate_to_version_four(connection: sqlite3.Connection) -> None:
    _validate_source_schema(connection, 3)
    connection.execute(
        "ALTER TABLE photo_move_transactions ADD COLUMN inverse_of_json TEXT DEFAULT NULL"
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=2, apply=_migrate_to_version_two),
    Migration(version=3, apply=_migrate_to_version_three),
    Migration(version=4, apply=_migrate_to_version_four),
)
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def migrate_database(
    path: Path,
    target_version: int = SCHEMA_VERSION,
    *,
    migrations: Sequence[Migration] | None = None,
    lock: ProjectDatabaseLock | None = None,
) -> None:
    """Migrate an existing database without exposing partial changes."""
    database_path = Path(path).expanduser().resolve()
    if lock is None:
        with ProjectDatabaseLock.acquire(database_path) as owned_lock:
            _migrate_database_locked(
                database_path,
                target_version,
                migrations=migrations,
                lock=owned_lock,
            )
        return
    _migrate_database_locked(
        database_path,
        target_version,
        migrations=migrations,
        lock=lock,
    )


def _migrate_database_locked(
    database_path: Path,
    target_version: int,
    *,
    migrations: Sequence[Migration] | None,
    lock: ProjectDatabaseLock,
) -> None:
    """Run a migration while the caller owns the database lifecycle."""
    lock.assert_owns(database_path)
    lock.assert_safe_database_link()
    current_version = _read_schema_version(database_path)
    if target_version < current_version:
        raise RuntimeError(
            f"target version {target_version} is older than current version "
            f"{current_version}"
        )
    using_application_migrations = migrations is None
    if current_version == target_version:
        if using_application_migrations and target_version == SCHEMA_VERSION:
            _validate_application_database(database_path)
        return

    available = MIGRATIONS if migrations is None else migrations
    steps = [
        migration
        for migration in available
        if current_version < migration.version <= target_version
    ]
    expected_versions = list(range(current_version + 1, target_version + 1))
    if [migration.version for migration in steps] != expected_versions:
        raise RuntimeError(
            "migrations must form a continuous ascending chain from "
            f"version {current_version} to {target_version}"
        )

    backup_path = Path(f"{database_path}.backup-v{current_version}")
    _atomic_database_backup(database_path, backup_path)
    working_path = _temporary_path(database_path, "migration")
    try:
        shutil.copy2(backup_path, working_path)
        connection = sqlite3.connect(working_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise RuntimeError(
                    "failed to enable foreign keys on migration database"
                )
            connection.execute("BEGIN IMMEDIATE")
            for migration in steps:
                guard = f"xuanpian_migration_guard_{secrets.token_hex(16)}"
                connection.execute(f"SAVEPOINT {guard}")
                migration.apply(connection)
                if not connection.in_transaction:
                    raise RuntimeError(
                        f"migration to version {migration.version} ended the "
                        "migration transaction"
                    )
                try:
                    connection.execute(f"RELEASE SAVEPOINT {guard}")
                except sqlite3.OperationalError as error:
                    raise RuntimeError(
                        f"migration to version {migration.version} ended the "
                        "migration transaction"
                    ) from error
                connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = ?",
                    (str(migration.version), "schema_version"),
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        _make_database_self_contained(working_path)
        _validate_database_for_publish(
            working_path,
            validate_application_schema=(
                using_application_migrations
                and target_version == SCHEMA_VERSION
            ),
        )
        if _read_schema_version(working_path) != target_version:
            raise RuntimeError(
                f"migration did not reach target version {target_version}"
            )
        replaced_original = False
        try:
            os.replace(working_path, database_path)
            replaced_original = True
            _remove_sidecars(database_path)
            if _read_schema_version(database_path) != target_version:
                raise RuntimeError(
                    "migrated database has unexpected schema version; "
                    f"expected {target_version}"
                )
        except BaseException:
            if replaced_original:
                try:
                    _restore_database(
                        backup_path,
                        database_path,
                        expected_version=current_version,
                    )
                except BaseException as restore_error:
                    raise RuntimeError(
                        "migration verification failed and restoring the "
                        "original database also failed"
                    ) from restore_error
            raise
    finally:
        _remove_database_artifacts(working_path)


def _read_schema_version(path: Path) -> int:
    if not path.is_file():
        raise FileNotFoundError(f"database does not exist: {path}")
    connection = _connect_read_only(path)
    try:
        try:
            rows = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                ("schema_version",),
            ).fetchmany(2)
        except sqlite3.OperationalError as error:
            raise RuntimeError(
                "database metadata table is missing or unreadable"
            ) from error
    finally:
        connection.close()
    if len(rows) != 1:
        raise RuntimeError("database metadata has no schema_version")
    stored_version = rows[0][0]
    try:
        parsed_version = int(stored_version)
    except (TypeError, ValueError):
        parsed_version = -1
    if (
        not isinstance(stored_version, str)
        or stored_version != str(parsed_version)
        or parsed_version < 0
    ):
        raise RuntimeError(
            f"database has invalid schema_version: {stored_version!r}"
        )
    return parsed_version


def _atomic_database_backup(source: Path, destination: Path) -> None:
    temporary = _temporary_path(destination, "backup")
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = _connect_read_only(source)
        destination_connection = sqlite3.connect(temporary)
        source_connection.backup(destination_connection)
        destination_connection.close()
        destination_connection = None
        source_connection.close()
        source_connection = None
        _make_database_self_contained(temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, destination)
        _remove_sidecars(destination)
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        _remove_database_artifacts(temporary)


def _restore_database(
    backup: Path,
    destination: Path,
    *,
    expected_version: int,
) -> None:
    temporary = _temporary_path(destination, "restore")
    try:
        shutil.copy2(backup, temporary)
        _make_database_self_contained(temporary)
        os.replace(temporary, destination)
        _remove_sidecars(destination)
        if _read_schema_version(destination) != expected_version:
            raise RuntimeError(
                "restored database has unexpected schema version; "
                f"expected {expected_version}"
            )
    finally:
        _remove_database_artifacts(temporary)


def _make_database_self_contained(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        checkpoint = connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if checkpoint is not None and checkpoint[0] != 0:
            raise RuntimeError(
                f"failed to checkpoint database before publish: {path}"
            )
        journal_mode = connection.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()
        if journal_mode != ("delete",):
            raise RuntimeError(
                f"failed to disable WAL before publishing database: {path}"
            )
    finally:
        connection.close()
    _remove_sidecars(path)


def _validate_database_for_publish(
    path: Path,
    *,
    validate_application_schema: bool = False,
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RuntimeError(
                "failed to enable foreign keys before database publication"
            )
        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "foreign_key_check failed before database publication: "
                f"{foreign_key_errors!r}"
            )
        quick_check_rows = _quick_check_rows(connection)
        if quick_check_rows != [("ok",)]:
            raise RuntimeError(
                "quick_check failed before database publication: "
                f"{quick_check_rows!r}"
            )
        if validate_application_schema:
            validate_schema(connection)
    finally:
        connection.close()


def _validate_application_database(path: Path) -> None:
    # CHECK-contract probes use an explicit transaction and roll it back.
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise RuntimeError(
                "failed to enable foreign keys before schema validation"
            )
        validate_schema(connection)
    except sqlite3.DatabaseError as error:
        raise SchemaValidationError(
            f"database schema validation failed: {error}"
        ) from error
    finally:
        connection.close()


def _quick_check_rows(
    connection: sqlite3.Connection,
) -> list[tuple[object, ...]]:
    return connection.execute("PRAGMA quick_check").fetchall()


def _remove_database_artifacts(path: Path) -> None:
    _remove_sidecars(path)
    path.unlink(missing_ok=True)


def _remove_sidecars(path: Path) -> None:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _temporary_path(path: Path, purpose: str) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.{purpose}-",
    )
    os.close(descriptor)
    return Path(name)
