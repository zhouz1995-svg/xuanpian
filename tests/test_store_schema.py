"""Tests for the initial project database schema and persisted photo records."""

from __future__ import annotations

import sqlite3

import pytest

from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.store.schema import (
    SCHEMA_VERSION,
    SchemaValidationError,
    create_schema,
    validate_schema,
)


def _columns(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[tuple[object, ...], ...]:
    return tuple(connection.execute(f"PRAGMA table_info({table})"))


def _foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, str, str, str]]:
    return {
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }


def _asset(kind: AssetKind, path: str) -> PhotoAssetRecord:
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=FileFingerprint(path=path, size=1024, mtime_ns=1234),
        origin_path=path,
    )


def test_photo_record_prefers_jpeg_for_analysis_and_keeps_source_order() -> None:
    jpeg = _asset(AssetKind.JPEG, "/photos/IMG_0001.jpg")
    raw = _asset(AssetKind.RAW, "/photos/IMG_0001.cr3")
    photo = PhotoRecord(
        id="photo-1",
        filename="IMG_0001.jpg",
        mark="keep",
        jpeg=jpeg,
        raw=raw,
    )

    assert photo.analysis_asset is jpeg
    assert photo.source_assets == (jpeg, raw)


def test_raw_only_photo_falls_back_to_raw_for_analysis() -> None:
    raw = _asset(AssetKind.RAW, "/photos/IMG_0002.nef")
    photo = PhotoRecord(
        id="photo-2",
        filename="IMG_0002.nef",
        mark="",
        jpeg=None,
        raw=raw,
    )

    assert photo.analysis_asset is raw
    assert photo.source_assets == (raw,)


def test_photo_record_is_immutable() -> None:
    photo = PhotoRecord(
        id="photo-3",
        filename="IMG_0003.jpg",
        mark="",
        jpeg=_asset(AssetKind.JPEG, "/photos/IMG_0003.jpg"),
        raw=None,
    )

    with pytest.raises(AttributeError):
        photo.mark = "reject"  # type: ignore[misc]


@pytest.mark.parametrize(
    "path, size, mtime_ns",
    [
        ("relative/IMG_0001.jpg", 1024, 1234),
        ("/photos/IMG_0001.jpg", -1, 1234),
        ("/photos/IMG_0001.jpg", 1024, -1),
        ("/photos/IMG_0001.jpg", 1024.0, 1234),
        ("/photos/IMG_0001.jpg", 1024, 1234.0),
    ],
)
def test_file_fingerprint_rejects_non_absolute_or_invalid_stat_values(
    path: str, size: int, mtime_ns: int
) -> None:
    with pytest.raises(ValueError):
        FileFingerprint(path=path, size=size, mtime_ns=mtime_ns)


@pytest.mark.parametrize(
    ("jpeg", "raw"),
    [
        (_asset(AssetKind.RAW, "/photos/IMG_0001.cr3"), None),
        (None, _asset(AssetKind.JPEG, "/photos/IMG_0001.jpg")),
    ],
)
def test_photo_record_rejects_assets_in_the_wrong_kind_slot(
    jpeg: PhotoAssetRecord | None, raw: PhotoAssetRecord | None
) -> None:
    with pytest.raises(ValueError):
        PhotoRecord(
            id="photo-4",
            filename="IMG_0004.jpg",
            mark="",
            jpeg=jpeg,
            raw=raw,
        )


def test_create_schema_initializes_version_tables_and_foreign_keys() -> None:
    connection = sqlite3.connect(":memory:")

    create_schema(connection)

    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "metadata",
        "projects",
        "photos",
        "photo_assets",
        "analysis_results",
        "analysis_failures",
    } <= table_names
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert connection.execute(
        "SELECT value FROM metadata WHERE key = ?", ("schema_version",)
    ).fetchone() == (str(SCHEMA_VERSION),)
    assert SCHEMA_VERSION == 4


def test_schema_v4_contains_exact_move_journal_contracts() -> None:
    connection = sqlite3.connect(":memory:")

    create_schema(connection)

    assert _columns(connection, "photo_assets")[-1] == (
        6,
        "origin_path",
        "TEXT",
        1,
        None,
        0,
    )
    assert _columns(connection, "removed_photo_assets")[-1] == (
        6,
        "origin_path",
        "TEXT",
        1,
        None,
        0,
    )
    assert _columns(connection, "project_settings") == (
        (0, "project_id", "TEXT", 1, None, 1),
        (1, "key", "TEXT", 1, None, 2),
        (2, "value", "TEXT", 1, None, 0),
    )
    assert _columns(connection, "photo_move_transactions") == (
        (0, "id", "TEXT", 0, None, 1),
        (1, "project_id", "TEXT", 1, None, 0),
        (2, "photo_id", "TEXT", 1, None, 0),
        (3, "before_mark", "TEXT", 1, None, 0),
        (4, "after_mark", "TEXT", 1, None, 0),
        (5, "created_at_ns", "INTEGER", 1, None, 0),
        (6, "inverse_of_json", "TEXT", 0, "NULL", 0),
    )
    assert _columns(connection, "photo_move_items") == (
        (0, "transaction_id", "TEXT", 1, None, 1),
        (1, "ordinal", "INTEGER", 1, None, 2),
        (2, "kind", "TEXT", 1, None, 0),
        (3, "source_path", "TEXT", 1, None, 0),
        (4, "target_path", "TEXT", 1, None, 0),
        (5, "size", "INTEGER", 1, None, 0),
        (6, "mtime_ns", "INTEGER", 1, None, 0),
        (7, "completed", "INTEGER", 1, "0", 0),
    )
    assert _foreign_keys(connection, "project_settings") == {
        ("projects", "project_id", "id", "CASCADE")
    }
    assert _foreign_keys(connection, "photo_move_transactions") == {
        ("projects", "project_id", "id", "CASCADE"),
        ("photos", "photo_id", "id", "CASCADE"),
    }
    assert _foreign_keys(connection, "photo_move_items") == {
        ("photo_move_transactions", "transaction_id", "id", "CASCADE")
    }
    transaction_indexes = {
        str(row[3]): (
            int(row[2]),
            tuple(
                str(column[2])
                for column in connection.execute(
                    f"PRAGMA index_info({row[1]})"
                )
            ),
        )
        for row in connection.execute(
            "PRAGMA index_list(photo_move_transactions)"
        )
    }
    assert transaction_indexes == {
        "pk": (1, ("id",)),
        "u": (1, ("photo_id",)),
    }


def test_schema_v3_enforces_settings_journal_uniqueness_and_checks() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES (?, ?)",
        ("project-1", "/photos"),
    )
    connection.execute(
        "INSERT INTO photos (id, project_id, filename, mark) "
        "VALUES (?, ?, ?, ?)",
        ("photo-1", "project-1", "one.jpg", "未处理"),
    )
    connection.execute(
        "INSERT INTO photos (id, project_id, filename, mark) "
        "VALUES (?, ?, ?, ?)",
        ("photo-2", "project-1", "two.jpg", "未处理"),
    )
    connection.execute(
        "INSERT INTO project_settings (project_id, key, value) "
        "VALUES (?, ?, ?)",
        ("project-1", "notice", "1"),
    )
    connection.execute(
        "INSERT INTO photo_move_transactions "
        "(id, project_id, photo_id, before_mark, after_mark, created_at_ns) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("move-1", "project-1", "photo-1", "未处理", "保留", 1),
    )
    connection.execute(
        "INSERT INTO photo_move_items "
        "(transaction_id, ordinal, kind, source_path, target_path, size, "
        "mtime_ns) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("move-1", 0, "jpeg", "/photos/one.jpg", "/保留/one.jpg", 1, 1),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO project_settings (project_id, key, value) "
            "VALUES (?, ?, ?)",
            ("project-1", "notice", "0"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO photo_move_transactions "
            "(id, project_id, photo_id, before_mark, after_mark, created_at_ns) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("move-2", "project-1", "photo-1", "未处理", "待定", 2),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO photo_move_items "
            "(transaction_id, ordinal, kind, source_path, target_path, size, "
            "mtime_ns, completed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("move-1", 1, "sidecar", "/a", "/b", 1, 1, 0),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO photo_move_items "
            "(transaction_id, ordinal, kind, source_path, target_path, size, "
            "mtime_ns, completed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("move-1", 1, "raw", "/a", "/b", 1, 1, 2),
        )


def test_schema_validator_rejects_changed_move_journal_declaration() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute("PRAGMA writable_schema = ON")
    declaration = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("photo_move_items",),
    ).fetchone()
    assert declaration is not None
    connection.execute(
        "UPDATE sqlite_master SET sql = ? WHERE type = 'table' AND name = ?",
        (
            str(declaration[0]).replace(
                "CHECK (completed IN (0, 1))",
                "CHECK (completed IN (0, 1, 2))",
            ),
            "photo_move_items",
        ),
    )
    connection.execute("PRAGMA writable_schema = OFF")

    with pytest.raises(SchemaValidationError, match="photo_move_items"):
        validate_schema(connection)


def test_create_schema_rejects_an_active_transaction_without_creating_tables() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE pending_work (value INTEGER NOT NULL)")
    connection.execute("INSERT INTO pending_work (value) VALUES (?)", (1,))
    assert connection.in_transaction

    with pytest.raises(RuntimeError, match="active transaction"):
        create_schema(connection)

    assert connection.in_transaction
    assert connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("metadata",),
    ).fetchone() is None


def test_photo_assets_allow_only_one_asset_kind_per_photo() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES (?, ?)",
        ("project-1", "/photos"),
    )
    connection.execute(
        "INSERT INTO photos (id, project_id, filename, mark) VALUES (?, ?, ?, ?)",
        ("photo-1", "project-1", "IMG_0001.jpg", ""),
    )
    connection.execute(
        """
        INSERT INTO photo_assets (
            photo_id, kind, path, size, mtime_ns, origin_path
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "photo-1",
            AssetKind.JPEG.value,
            "/photos/IMG_0001.jpg",
            1024,
            1234,
            "/photos/IMG_0001.jpg",
        ),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO photo_assets (
                photo_id, kind, path, size, mtime_ns, origin_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "photo-1",
                AssetKind.JPEG.value,
                "/photos/copy.jpg",
                512,
                5678,
                "/photos/copy.jpg",
            ),
        )


def test_analysis_results_store_status_and_allow_null_results() -> None:
    connection = sqlite3.connect(":memory:")
    create_schema(connection)
    connection.execute(
        "INSERT INTO projects (id, root_path) VALUES (?, ?)",
        ("project-1", "/photos"),
    )
    connection.execute(
        "INSERT INTO photos (id, project_id, filename, mark) VALUES (?, ?, ?, ?)",
        ("photo-1", "project-1", "IMG_0001.jpg", ""),
    )

    connection.execute(
        """
        INSERT INTO analysis_results (photo_id, analyzer, status, result_json)
        VALUES (?, ?, ?, ?)
        """,
        ("photo-1", "blur", AnalysisStatus.PENDING.value, None),
    )
    assert connection.execute(
        """
        SELECT status, result_json FROM analysis_results
        WHERE photo_id = ? AND analyzer = ?
        """,
        ("photo-1", "blur"),
    ).fetchone() == (AnalysisStatus.PENDING.value, None)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO analysis_results (photo_id, analyzer, status, result_json)
            VALUES (?, ?, ?, ?)
            """,
            ("photo-1", "exposure", "unknown", None),
        )
