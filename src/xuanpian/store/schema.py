"""Versioned SQLite schema and fail-closed structural validation."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from uuid import uuid4


SCHEMA_VERSION = 4


class SchemaValidationError(RuntimeError):
    """The database claims a supported version but its schema is unsafe."""


_VERSION_THREE_CREATE_TABLES_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL
);

CREATE TABLE project_settings (
    project_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (project_id, key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE photos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mark TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE "photo_assets" (
    photo_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    present INTEGER NOT NULL DEFAULT 1 CHECK (present IN (0, 1)),
    origin_path TEXT NOT NULL,
    PRIMARY KEY (photo_id, kind),
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
);

CREATE INDEX idx_photo_assets_path ON photo_assets(path);

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

CREATE TABLE photo_move_transactions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    photo_id TEXT NOT NULL UNIQUE,
    before_mark TEXT NOT NULL,
    after_mark TEXT NOT NULL,
    created_at_ns INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (photo_id) REFERENCES photos(id) ON DELETE CASCADE
);

CREATE TABLE photo_move_items (
    transaction_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    PRIMARY KEY (transaction_id, ordinal),
    FOREIGN KEY (transaction_id) REFERENCES photo_move_transactions(id)
        ON DELETE CASCADE
);

CREATE TABLE removed_photos (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    mark TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE "removed_photo_assets" (
    photo_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('jpeg', 'raw')),
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    origin_path TEXT NOT NULL,
    PRIMARY KEY (photo_id, kind),
    FOREIGN KEY (photo_id) REFERENCES removed_photos(id) ON DELETE CASCADE
);

CREATE INDEX idx_removed_photo_assets_path ON removed_photo_assets(path);
"""

_CREATE_TABLES_SQL = _VERSION_THREE_CREATE_TABLES_SQL + """
ALTER TABLE photo_move_transactions ADD COLUMN inverse_of_json TEXT DEFAULT NULL;
"""


_REQUIRED_COLUMNS = {
    "metadata": (
        (0, "key", "TEXT", 0, None, 1),
        (1, "value", "TEXT", 1, None, 0),
    ),
    "projects": (
        (0, "id", "TEXT", 0, None, 1),
        (1, "root_path", "TEXT", 1, None, 0),
    ),
    "project_settings": (
        (0, "project_id", "TEXT", 1, None, 1),
        (1, "key", "TEXT", 1, None, 2),
        (2, "value", "TEXT", 1, None, 0),
    ),
    "photos": (
        (0, "id", "TEXT", 0, None, 1),
        (1, "project_id", "TEXT", 1, None, 0),
        (2, "filename", "TEXT", 1, None, 0),
        (3, "mark", "TEXT", 1, None, 0),
    ),
    "photo_assets": (
        (0, "photo_id", "TEXT", 1, None, 1),
        (1, "kind", "TEXT", 1, None, 2),
        (2, "path", "TEXT", 1, None, 0),
        (3, "size", "INTEGER", 1, None, 0),
        (4, "mtime_ns", "INTEGER", 1, None, 0),
        (5, "present", "INTEGER", 1, "1", 0),
        (6, "origin_path", "TEXT", 1, None, 0),
    ),
    "analysis_results": (
        (0, "photo_id", "TEXT", 1, None, 1),
        (1, "analyzer", "TEXT", 1, None, 2),
        (2, "status", "TEXT", 1, None, 0),
        (3, "result_json", "TEXT", 0, None, 0),
    ),
    "analysis_failures": (
        (0, "photo_id", "TEXT", 1, None, 1),
        (1, "analyzer", "TEXT", 1, None, 2),
        (2, "message", "TEXT", 1, None, 0),
        (3, "exception_type", "TEXT", 1, None, 0),
    ),
    "photo_move_transactions": (
        (0, "id", "TEXT", 0, None, 1),
        (1, "project_id", "TEXT", 1, None, 0),
        (2, "photo_id", "TEXT", 1, None, 0),
        (3, "before_mark", "TEXT", 1, None, 0),
        (4, "after_mark", "TEXT", 1, None, 0),
        (5, "created_at_ns", "INTEGER", 1, None, 0),
        (6, "inverse_of_json", "TEXT", 0, "NULL", 0),
    ),
    "photo_move_items": (
        (0, "transaction_id", "TEXT", 1, None, 1),
        (1, "ordinal", "INTEGER", 1, None, 2),
        (2, "kind", "TEXT", 1, None, 0),
        (3, "source_path", "TEXT", 1, None, 0),
        (4, "target_path", "TEXT", 1, None, 0),
        (5, "size", "INTEGER", 1, None, 0),
        (6, "mtime_ns", "INTEGER", 1, None, 0),
        (7, "completed", "INTEGER", 1, "0", 0),
    ),
    "removed_photos": (
        (0, "id", "TEXT", 0, None, 1),
        (1, "project_id", "TEXT", 1, None, 0),
        (2, "filename", "TEXT", 1, None, 0),
        (3, "mark", "TEXT", 1, None, 0),
    ),
    "removed_photo_assets": (
        (0, "photo_id", "TEXT", 1, None, 1),
        (1, "kind", "TEXT", 1, None, 2),
        (2, "path", "TEXT", 1, None, 0),
        (3, "size", "INTEGER", 1, None, 0),
        (4, "mtime_ns", "INTEGER", 1, None, 0),
        (5, "present", "INTEGER", 1, None, 0),
        (6, "origin_path", "TEXT", 1, None, 0),
    ),
}
_REQUIRED_INDEXES = {
    "idx_photo_assets_path": ("photo_assets", ("path",), 0, 0),
    "idx_removed_photo_assets_path": (
        "removed_photo_assets",
        ("path",),
        0,
        0,
    ),
}
_REQUIRED_FOREIGN_KEYS = {
    ("project_settings", "projects", "project_id", "id", "CASCADE"),
    ("photos", "projects", "project_id", "id", "CASCADE"),
    ("photo_assets", "photos", "photo_id", "id", "CASCADE"),
    ("analysis_results", "photos", "photo_id", "id", "CASCADE"),
    ("analysis_failures", "photos", "photo_id", "id", "CASCADE"),
    (
        "photo_move_transactions",
        "projects",
        "project_id",
        "id",
        "CASCADE",
    ),
    (
        "photo_move_transactions",
        "photos",
        "photo_id",
        "id",
        "CASCADE",
    ),
    (
        "photo_move_items",
        "photo_move_transactions",
        "transaction_id",
        "id",
        "CASCADE",
    ),
    ("removed_photos", "projects", "project_id", "id", "CASCADE"),
    (
        "removed_photo_assets",
        "removed_photos",
        "photo_id",
        "id",
        "CASCADE",
    ),
}
_SQL_LEXER_PATTERN = re.compile(
    r"""
    (?P<whitespace>\s+)
    | (?P<line_comment>--[^\r\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<string>'(?:''|[^'])*')
    | (?P<quoted_identifier>"(?:""|[^"])*")
    | (?P<backtick_identifier>`(?:``|[^`])*`)
    | (?P<bracket_identifier>\[[^\]]*\])
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<operator><=|>=|<>|!=|==|\|\||<<|>>|[-+*/%<>=~&|])
    | (?P<punctuation>[(),.;])
    """,
    re.DOTALL | re.VERBOSE,
)


def _schema_tokens(sql: str) -> tuple[tuple[str, str], ...]:
    """Return a type-preserving, whitespace-insensitive SQL token stream."""
    tokens: list[tuple[str, str]] = []
    offset = 0
    while offset < len(sql):
        match = _SQL_LEXER_PATTERN.match(sql, offset)
        if match is None:
            raise SchemaValidationError(
                "database table declaration contains unsupported SQL syntax "
                f"at offset {offset}"
            )
        kind = str(match.lastgroup)
        value = match.group(0)
        offset = match.end()
        if kind in {"whitespace", "line_comment", "block_comment"}:
            continue
        if kind == "word":
            value = value.lower()
        tokens.append((kind, value))
    return tuple(tokens)


@lru_cache(maxsize=1)
def _canonical_schema_contracts() -> dict[
    tuple[str, str], tuple[str, tuple[tuple[str, str], ...]]
]:
    """Return every application-owned sqlite_master object contract."""
    reference = sqlite3.connect(":memory:")
    try:
        reference.executescript(_CREATE_TABLES_SQL)
        declarations = tuple(
            reference.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master"
            )
        )
    finally:
        reference.close()
    return {
        (str(object_type), str(name)): (
            str(table_name),
            _schema_tokens(str(sql)),
        )
        for object_type, name, table_name, sql in declarations
        if not str(name).lower().startswith("sqlite_") and sql is not None
    }


def _validate_sqlite_autoindex(
    connection: sqlite3.Connection,
    name: str,
    table_name: str,
    sql: object,
) -> None:
    if sql is not None or table_name not in _REQUIRED_COLUMNS:
        raise SchemaValidationError(
            f"database has unexpected SQLite autoindex {name}"
        )
    quoted_table = table_name.replace('"', '""')
    matching_indexes = [
        row
        for row in connection.execute(f'PRAGMA index_list("{quoted_table}")')
        if str(row[1]) == name
    ]
    if len(matching_indexes) != 1 or str(matching_indexes[0][3]) not in {
        "pk",
        "u",
    }:
        raise SchemaValidationError(
            f"database has unexpected SQLite autoindex {name}"
        )


def _validate_table_contracts(connection: sqlite3.Connection) -> None:
    """Validate the closed set of application-owned sqlite_master objects."""
    expected_contracts = _canonical_schema_contracts()
    actual_contracts: dict[
        tuple[str, str], tuple[str, tuple[tuple[str, str], ...]]
    ] = {}
    autoindexes: list[tuple[str, str, object]] = []
    for object_type, name, table_name, sql in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ):
        object_type = str(object_type)
        name = str(name)
        table_name = str(table_name)
        if name.lower().startswith("sqlite_autoindex_"):
            autoindexes.append((name, table_name, sql))
            continue
        if sql is None:
            raise SchemaValidationError(
                f"database {object_type} {name} has no auditable declaration"
            )
        actual_contracts[(object_type, name)] = (
            table_name,
            _schema_tokens(str(sql)),
        )

    missing = set(expected_contracts) - set(actual_contracts)
    if missing:
        raise SchemaValidationError(
            f"database is missing required schema objects: {sorted(missing)!r}"
        )
    for (object_type, name), expected in expected_contracts.items():
        if actual_contracts[(object_type, name)] != expected:
            raise SchemaValidationError(
                f"database {object_type} {name} has an unexpected schema "
                "contract"
            )
    unexpected = set(actual_contracts) - set(expected_contracts)
    if unexpected:
        raise SchemaValidationError(
            f"database has unexpected schema objects: {sorted(unexpected)!r}"
        )
    for name, table_name, sql in autoindexes:
        _validate_sqlite_autoindex(connection, name, table_name, sql)


def _expect_check_rejection(
    connection: sqlite3.Connection,
    table: str,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    savepoint = "schema_check_contract_attempt"
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        try:
            connection.execute(statement, parameters)
        except sqlite3.IntegrityError as error:
            if (
                getattr(error, "sqlite_errorcode", None)
                == sqlite3.SQLITE_CONSTRAINT_CHECK
            ):
                return
            raise SchemaValidationError(
                f"database table {table} CHECK probe was rejected by an "
                "unexpected constraint"
            ) from error
        raise SchemaValidationError(
            f"database table {table} accepted an invalid CHECK probe"
        )
    finally:
        connection.execute(f"ROLLBACK TO {savepoint}")
        connection.execute(f"RELEASE {savepoint}")


def _validate_check_semantics(connection: sqlite3.Connection) -> None:
    """Prove required CHECK behavior inside an always-rolled-back savepoint."""
    savepoint = "schema_check_contract_probe"
    started_transaction = not connection.in_transaction
    prefix = f"__schema_check_probe_{uuid4().hex}"
    project_id = f"{prefix}_project"
    photo_id = f"{prefix}_photo"
    removed_photo_id = f"{prefix}_removed"
    if started_transaction:
        connection.execute("BEGIN")
    connection.execute(f"SAVEPOINT {savepoint}")
    try:
        connection.execute(
            "INSERT INTO projects (id, root_path) VALUES (?, ?)",
            (project_id, f"/{prefix}"),
        )
        connection.execute(
            """
            INSERT INTO photos (id, project_id, filename, mark)
            VALUES (?, ?, ?, ?)
            """,
            (photo_id, project_id, f"{prefix}.jpg", "待定"),
        )
        connection.execute(
            """
            INSERT INTO removed_photos (id, project_id, filename, mark)
            VALUES (?, ?, ?, ?)
            """,
            (removed_photo_id, project_id, f"{prefix}.jpg", "待定"),
        )
        connection.execute(
            """
            INSERT INTO photo_move_transactions (
                id, project_id, photo_id, before_mark, after_mark,
                created_at_ns
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"{prefix}_move",
                project_id,
                photo_id,
                "未处理",
                "保留",
                0,
            ),
        )
        probes = (
            (
                "photo_assets",
                """
                INSERT INTO photo_assets
                    (photo_id, kind, path, size, mtime_ns, present, origin_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    photo_id,
                    "jpeg ",
                    f"/{prefix}-kind",
                    0,
                    0,
                    1,
                    f"/{prefix}-kind-origin",
                ),
            ),
            (
                "photo_assets",
                """
                INSERT INTO photo_assets
                    (photo_id, kind, path, size, mtime_ns, present, origin_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    photo_id,
                    "jpeg",
                    f"/{prefix}-present",
                    0,
                    0,
                    2,
                    f"/{prefix}-present-origin",
                ),
            ),
            (
                "removed_photo_assets",
                """
                INSERT INTO removed_photo_assets
                    (photo_id, kind, path, size, mtime_ns, present, origin_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    removed_photo_id,
                    "jpeg ",
                    f"/{prefix}-removed-kind",
                    0,
                    0,
                    1,
                    f"/{prefix}-removed-kind-origin",
                ),
            ),
            (
                "removed_photo_assets",
                """
                INSERT INTO removed_photo_assets
                    (photo_id, kind, path, size, mtime_ns, present, origin_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    removed_photo_id,
                    "jpeg",
                    f"/{prefix}-removed-present",
                    0,
                    0,
                    2,
                    f"/{prefix}-removed-present-origin",
                ),
            ),
            (
                "analysis_results",
                """
                INSERT INTO analysis_results
                    (photo_id, analyzer, status, result_json)
                VALUES (?, ?, ?, ?)
                """,
                (photo_id, f"{prefix}-analyzer", "pending ", None),
            ),
            (
                "photo_move_items",
                """
                INSERT INTO photo_move_items (
                    transaction_id, ordinal, kind, source_path, target_path,
                    size, mtime_ns, completed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{prefix}_move",
                    0,
                    "jpeg ",
                    f"/{prefix}-source",
                    f"/{prefix}-target",
                    0,
                    0,
                    0,
                ),
            ),
            (
                "photo_move_items",
                """
                INSERT INTO photo_move_items (
                    transaction_id, ordinal, kind, source_path, target_path,
                    size, mtime_ns, completed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{prefix}_move",
                    1,
                    "jpeg",
                    f"/{prefix}-source",
                    f"/{prefix}-target",
                    0,
                    0,
                    2,
                ),
            ),
        )
        for table, statement, parameters in probes:
            _expect_check_rejection(
                connection,
                table,
                statement,
                parameters,
            )
    except SchemaValidationError:
        raise
    except sqlite3.DatabaseError as error:
        raise SchemaValidationError(
            "database CHECK probe could not be executed safely"
        ) from error
    finally:
        try:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
        finally:
            if started_transaction:
                connection.rollback()


def create_schema(connection: sqlite3.Connection) -> None:
    """Create a fresh version-three database on an empty connection."""
    if connection.in_transaction:
        raise RuntimeError(
            "cannot create schema while an active transaction is open"
        )
    if connection.execute(
        "SELECT 1 FROM sqlite_master LIMIT 1"
    ).fetchone() is not None:
        raise RuntimeError("cannot create schema in a non-empty database")
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("failed to enable SQLite foreign key enforcement")
    connection.executescript(_CREATE_TABLES_SQL)
    connection.execute(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        ("schema_version", str(SCHEMA_VERSION)),
    )
    validate_schema(connection)


def validate_schema(connection: sqlite3.Connection) -> None:
    """Validate application structure and SQLite integrity without repair."""
    try:
        version_rows = connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            ("schema_version",),
        ).fetchmany(2)
    except sqlite3.DatabaseError as error:
        raise SchemaValidationError(
            "database metadata table is missing or unreadable"
        ) from error
    if version_rows != [(str(SCHEMA_VERSION),)]:
        raise SchemaValidationError(
            "database schema_version does not match the application"
        )

    tables = {
        str(name)
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing_tables = set(_REQUIRED_COLUMNS) - tables
    if missing_tables:
        raise SchemaValidationError(
            f"database is missing required tables: {sorted(missing_tables)!r}"
        )
    _validate_table_contracts(connection)
    for table, required in _REQUIRED_COLUMNS.items():
        columns = tuple(
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
            ) in connection.execute(f"PRAGMA table_info({table})")
        )
        if columns != required:
            raise SchemaValidationError(
                f"database table {table} has an unexpected column contract: "
                f"{columns!r}"
            )

    _validate_check_semantics(connection)

    for (
        index_name,
        (
            expected_table,
            expected_columns,
            expected_unique,
            expected_partial,
        ),
    ) in _REQUIRED_INDEXES.items():
        indexes = {
            str(row[1]): row
            for row in connection.execute(
                f"PRAGMA index_list({expected_table})"
            )
        }
        index = indexes.get(index_name)
        if index is None:
            raise SchemaValidationError(
                f"database table {expected_table} is missing required "
                f"index {index_name}"
            )
        actual_unique = int(index[2])
        actual_origin = str(index[3])
        actual_partial = int(index[4])
        actual_columns = tuple(
            str(row[2])
            for row in connection.execute(
                f"PRAGMA index_xinfo({index_name})"
            )
            if int(row[5]) == 1
        )
        if (
            actual_columns != expected_columns
            or actual_unique != expected_unique
            or actual_origin != "c"
            or actual_partial != expected_partial
        ):
            raise SchemaValidationError(
                f"database index {index_name} has an unexpected contract"
            )

    foreign_keys: set[tuple[str, str, str, str, str]] = set()
    for table in _REQUIRED_COLUMNS:
        for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
            foreign_keys.add(
                (
                    table,
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[6]).upper(),
                )
            )
    missing_foreign_keys = _REQUIRED_FOREIGN_KEYS - foreign_keys
    if missing_foreign_keys:
        raise SchemaValidationError(
            "database is missing required foreign keys: "
            f"{sorted(missing_foreign_keys)!r}"
        )

    foreign_key_errors = connection.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        raise SchemaValidationError(
            f"database foreign_key_check failed: {foreign_key_errors!r}"
        )
    quick_check = connection.execute("PRAGMA quick_check").fetchall()
    if quick_check != [("ok",)]:
        raise SchemaValidationError(
            f"database quick_check failed: {quick_check!r}"
        )
