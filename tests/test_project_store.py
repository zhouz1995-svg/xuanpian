"""Behavior tests for the single-writer SQLite project store."""

from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.xuanpian.domain.analysis import AnalysisFailure, AnalysisStatus
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.store import project_store
from src.xuanpian.store.project_store import (
    PhotoChange,
    ProjectStore,
    StoredAnalysis,
)


def _asset(
    kind: AssetKind,
    path: Path,
    *,
    size: int = 1024,
    mtime_ns: int = 1234,
) -> PhotoAssetRecord:
    fingerprint_path = str(path.absolute())
    return PhotoAssetRecord(
        kind=kind,
        fingerprint=FileFingerprint(
            path=fingerprint_path,
            size=size,
            mtime_ns=mtime_ns,
        ),
        origin_path=fingerprint_path,
    )


def _photo(
    photo_id: str,
    directory: Path,
    *,
    jpeg: bool = True,
    raw: bool = False,
    filename: str | None = None,
    mark: str = "",
    size: int = 1024,
) -> PhotoRecord:
    stem = photo_id.upper()
    jpeg_asset = (
        _asset(AssetKind.JPEG, directory / f"{stem}.jpg", size=size)
        if jpeg
        else None
    )
    raw_asset = (
        _asset(AssetKind.RAW, directory / f"{stem}.cr3", size=size + 1)
        if raw
        else None
    )
    return PhotoRecord(
        id=photo_id,
        filename=filename or (f"{stem}.jpg" if jpeg else f"{stem}.cr3"),
        mark=mark,
        jpeg=jpeg_asset,
        raw=raw_asset,
    )


def test_current_technical_analysis_validates_terminal_contract(
    tmp_path: Path,
) -> None:
    store = ProjectStore.open(tmp_path / "project.sqlite3")
    store.upsert_photos([_photo("one", tmp_path)])
    store.save_analysis("one", "composition", AnalysisStatus.SUCCEEDED, {"score": 90}, None)
    assert store.get_current_technical_analysis("one") is None

    store.save_analysis(
        "one", "technical", AnalysisStatus.SUCCEEDED,
        {"algorithm_version": "technical-v3"}, None,
    )
    assert store.get_current_technical_analysis("one") is None

    payload = {"algorithm_version": "technical-v4"}
    store.save_analysis("one", "technical", AnalysisStatus.SUCCEEDED, payload, None)
    assert store.get_current_technical_analysis("one") is None

    stale_payload = {"algorithm_version": "technical-v5"}
    store.save_analysis(
        "one", "technical", AnalysisStatus.SUCCEEDED, stale_payload, None
    )
    assert store.get_current_technical_analysis("one") is None

    stale_payload = {"algorithm_version": "technical-v6"}
    store.save_analysis(
        "one", "technical", AnalysisStatus.SUCCEEDED, stale_payload, None
    )
    assert store.get_current_technical_analysis("one") is None

    payload = {"algorithm_version": "technical-v8"}
    store.save_analysis("one", "technical", AnalysisStatus.SUCCEEDED, payload, None)
    assert store.get_current_technical_analysis("one").result == payload

    failure = AnalysisFailure(
        "technical", "model unavailable", "RuntimeError"
    )
    store.save_analysis(
        "one",
        "technical",
        AnalysisStatus.FAILED,
        {"algorithm_version": "technical-v4"},
        failure,
    )
    assert store.get_current_technical_analysis("one") is None
    store.save_analysis(
        "one", "technical", AnalysisStatus.FAILED, None, None
    )
    assert store.get_current_technical_analysis("one") is None
    store.save_analysis(
        "one", "technical", AnalysisStatus.FAILED, None, failure
    )
    assert store.get_current_technical_analysis("one").status is (
        AnalysisStatus.FAILED
    )

    store.save_analysis(
        "one", "technical", AnalysisStatus.CANCELED, payload, None
    )
    assert store.get_current_technical_analysis("one") is None
    store.save_analysis(
        "one", "technical", AnalysisStatus.CANCELED, None, failure
    )
    assert store.get_current_technical_analysis("one") is None
    store.save_analysis(
        "one", "technical", AnalysisStatus.CANCELED, None, None
    )
    assert store.get_current_technical_analysis("one").status is (
        AnalysisStatus.CANCELED
    )

    for status in (AnalysisStatus.PENDING, AnalysisStatus.RUNNING):
        store.save_analysis("one", "technical", status, None, None)
        assert store.get_current_technical_analysis("one") is None
    store.close()


def test_round_trip_restores_assets_marks_failures_and_zero_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "nested" / "project.sqlite3"
    jpeg_only = _photo("jpeg", tmp_path, mark="input-jpeg")
    raw_only = _photo("raw", tmp_path, jpeg=False, raw=True)
    paired = _photo("paired", tmp_path, jpeg=True, raw=True)
    failure = AnalysisFailure(
        analyzer="face",
        message="model unavailable",
        exception_type="RuntimeError",
    )

    store = ProjectStore.open(database)
    assert store.upsert_photos([jpeg_only, raw_only, paired]) == {
        "jpeg": PhotoChange.NEW,
        "raw": PhotoChange.NEW,
        "paired": PhotoChange.NEW,
    }
    store.save_mark("jpeg", "keep")
    store.save_analysis(
        "jpeg",
        "exposure",
        AnalysisStatus.SUCCEEDED,
        0,
        None,
    )
    store.save_analysis(
        "raw",
        "face",
        AnalysisStatus.FAILED,
        None,
        failure,
    )
    store.close()

    reopened = ProjectStore.open(database)
    assert reopened.list_photos() == [
        PhotoRecord(
            id="jpeg",
            filename="JPEG.jpg",
            mark="keep",
            jpeg=jpeg_only.jpeg,
            raw=None,
        ),
        paired,
        raw_only,
    ]
    assert reopened.get_analysis("jpeg", "exposure") == StoredAnalysis(
        photo_id="jpeg",
        analyzer="exposure",
        status=AnalysisStatus.SUCCEEDED,
        result=0,
        failure=None,
    )
    assert reopened.get_analysis("raw", "face") == StoredAnalysis(
        photo_id="raw",
        analyzer="face",
        status=AnalysisStatus.FAILED,
        result=None,
        failure=failure,
    )
    reopened.close()


def test_raw_only_upgrade_preserves_id_mark_and_invalidates_analyses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    raw_only = _photo("one", tmp_path, jpeg=False, raw=True, mark="imported")
    upgraded = _photo("one", tmp_path, jpeg=True, raw=True, mark="overwrite")

    with ProjectStore.open(database) as store:
        assert store.upsert_photos([raw_only]) == {"one": PhotoChange.NEW}
        store.save_mark("one", "keep")
        store.save_analysis(
            "one",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 0.0, "blurry": False},
            None,
        )
        store.save_analysis(
            "one",
            "face",
            AnalysisStatus.FAILED,
            None,
            AnalysisFailure("face", "model unavailable", "RuntimeError"),
        )

        assert store.upsert_photos([upgraded]) == {"one": PhotoChange.CHANGED}
        assert store.list_photos() == [
            PhotoRecord(
                id="one",
                filename="ONE.jpg",
                mark="keep",
                jpeg=upgraded.jpeg,
                raw=upgraded.raw,
            )
        ]
        assert store.get_analysis("one", "blur") == StoredAnalysis(
            photo_id="one",
            analyzer="blur",
            status=AnalysisStatus.PENDING,
            result=None,
            failure=None,
        )
        assert store.get_analysis("one", "face") == StoredAnalysis(
            photo_id="one",
            analyzer="face",
            status=AnalysisStatus.PENDING,
            result=None,
            failure=None,
        )


def test_upsert_reports_unchanged_and_fingerprint_changes(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite3"
    original = _photo("one", tmp_path, mark="first")
    mark_only = _photo("one", tmp_path, mark="ignored")
    changed = _photo("one", tmp_path, size=2048)

    with ProjectStore.open(database) as store:
        assert store.upsert_photos([original]) == {"one": PhotoChange.NEW}
        assert store.upsert_photos([mark_only]) == {
            "one": PhotoChange.UNCHANGED
        }
        assert store.upsert_photos([changed]) == {"one": PhotoChange.CHANGED}
        assert store.list_photos()[0].mark == "first"


def test_upsert_rolls_back_every_photo_and_asset_on_mid_batch_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store._connection.execute(  # type: ignore[attr-defined]
            """
            CREATE TRIGGER reject_bad_photo
            BEFORE INSERT ON photos
            WHEN NEW.id = 'bad'
            BEGIN
                SELECT RAISE(ABORT, 'injected failure');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            store.upsert_photos(
                [_photo("good", tmp_path), _photo("bad", tmp_path)]
            )
        assert store.list_photos() == []

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM photos").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM photo_assets"
        ).fetchone() == (0,)


def test_reconcile_photos_commits_survivors_and_superseded_deletes_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    raw = _photo("raw-id", tmp_path, jpeg=False, raw=True, mark="入选")
    jpeg = _photo("jpeg-id", tmp_path, jpeg=True)
    paired = PhotoRecord(
        id="jpeg-id",
        filename="JPEG-ID.jpg",
        mark="",
        jpeg=jpeg.jpeg,
        raw=raw.raw,
    )

    with ProjectStore.open(database) as store:
        store.upsert_photos((raw, jpeg))
        transactions: list[str] = []
        store._connection.set_trace_callback(  # type: ignore[attr-defined]
            lambda statement: transactions.append(statement)
        )

        changes = store.reconcile_photos(
            (
                project_store.PhotoReconciliation(
                    paired,
                    superseded_photo_ids=("raw-id",),
                ),
                project_store.PhotoReconciliation(_photo("third", tmp_path)),
            )
        )

        assert changes == (
            PhotoChange.CHANGED,
            PhotoChange.NEW,
        )
        assert [
            statement
            for statement in transactions
            if statement == "BEGIN IMMEDIATE"
        ] == ["BEGIN IMMEDIATE"]
        assert [photo.id for photo in store.list_photos()] == [
            "jpeg-id",
            "third",
        ]


def test_reconcile_photos_rolls_back_survivors_and_deletes_together(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    survivor = _photo("survivor", tmp_path)
    superseded = _photo("superseded", tmp_path)
    with ProjectStore.open(database) as store:
        store._connection.execute(  # type: ignore[attr-defined]
            """
            CREATE TRIGGER reject_bad_reconcile
            BEFORE INSERT ON photos
            WHEN NEW.id = 'bad'
            BEGIN
                SELECT RAISE(ABORT, 'injected reconcile failure');
            END
            """
        )
        store.upsert_photos((survivor, superseded))
        with pytest.raises(
            sqlite3.IntegrityError,
            match="injected reconcile failure",
        ):
            store.reconcile_photos(
                (
                    project_store.PhotoReconciliation(
                        _photo("survivor", tmp_path, size=2048),
                        superseded_photo_ids=("superseded",),
                    ),
                    project_store.PhotoReconciliation(
                        _photo("bad", tmp_path)
                    ),
                )
            )

        assert store.get_photo("survivor") == survivor
        assert store.get_photo("superseded") == superseded
        assert store.get_photo("bad") is None


def test_reconcile_adopts_latest_superseded_missing_path_and_restores_stable_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    jpeg = _asset(AssetKind.JPEG, tmp_path / "survivor.jpg")
    old_raw = _asset(AssetKind.RAW, tmp_path / "old.cr3")
    older_superseded_raw = _asset(
        AssetKind.RAW,
        tmp_path / "older-superseded.cr3",
        size=1536,
        mtime_ns=3456,
    )
    newer_raw = _asset(
        AssetKind.RAW,
        tmp_path / "newer.cr3",
        size=2049,
        mtime_ns=5678,
    )
    survivor = PhotoRecord(
        "survivor",
        "survivor.jpg",
        "入选",
        jpeg=jpeg,
        raw=old_raw,
    )
    jpeg_only = PhotoRecord(
        "survivor",
        "survivor.jpg",
        "",
        jpeg=jpeg,
        raw=None,
    )
    superseded = PhotoRecord(
        "new-raw",
        "newer.cr3",
        "",
        jpeg=None,
        raw=newer_raw,
    )
    older_superseded = PhotoRecord(
        "older-raw",
        "older-superseded.cr3",
        "",
        jpeg=None,
        raw=older_superseded_raw,
    )

    with ProjectStore.open(database) as store:
        store.upsert_photos((survivor, older_superseded, superseded))
        store.upsert_photos(
            (
                jpeg_only,
                PhotoRecord(
                    "older-raw",
                    "older-superseded.cr3",
                    "",
                    jpeg=None,
                    raw=None,
                ),
                PhotoRecord(
                    "new-raw",
                    "newer.cr3",
                    "",
                    jpeg=None,
                    raw=None,
                ),
            )
        )

        store.reconcile_photos(
            (
                project_store.PhotoReconciliation(
                    jpeg_only,
                    superseded_photo_ids=("older-raw", "new-raw"),
                ),
            )
        )

        assert store.photo_ids_by_asset_paths(
            (
                old_raw.fingerprint.path,
                older_superseded_raw.fingerprint.path,
                newer_raw.fingerprint.path,
            )
        ) == {newer_raw.fingerprint.path: "survivor"}
        assert store.get_photo("survivor") == PhotoRecord(
            "survivor",
            "survivor.jpg",
            "入选",
            jpeg=jpeg,
            raw=None,
        )

        restored = PhotoRecord(
            "survivor",
            "survivor.jpg",
            "",
            jpeg=jpeg,
            raw=newer_raw,
        )
        store.reconcile_photo(restored)
        assert store.get_photo("survivor") == PhotoRecord(
            "survivor",
            "survivor.jpg",
            "入选",
            jpeg=jpeg,
            raw=newer_raw,
        )


def test_reconcile_does_not_replace_present_survivor_with_missing_superseded(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    survivor = _photo("survivor", tmp_path, jpeg=True, raw=True)
    replacement_raw = _asset(AssetKind.RAW, tmp_path / "replacement.cr3")
    superseded = PhotoRecord(
        "superseded",
        "replacement.cr3",
        "",
        jpeg=None,
        raw=replacement_raw,
    )

    with ProjectStore.open(database) as store:
        store.upsert_photos((survivor, superseded))
        store.upsert_photos(
            (
                PhotoRecord(
                    "superseded",
                    "replacement.cr3",
                    "",
                    jpeg=None,
                    raw=None,
                ),
            )
        )

        store.reconcile_photos(
            (
                project_store.PhotoReconciliation(
                    survivor,
                    superseded_photo_ids=("superseded",),
                ),
            )
        )

        assert store.get_photo("survivor") == survivor
        assert store.photo_ids_by_asset_paths(
            (
                survivor.raw.fingerprint.path,
                replacement_raw.fingerprint.path,
            )
        ) == {survivor.raw.fingerprint.path: "survivor"}


def test_reconcile_path_uniqueness_conflict_rolls_back_asset_adoption(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    jpeg = _asset(AssetKind.JPEG, tmp_path / "survivor.jpg")
    old_raw = _asset(AssetKind.RAW, tmp_path / "old.cr3")
    conflicting_raw = _asset(AssetKind.RAW, tmp_path / "conflict.cr3")
    survivor = PhotoRecord(
        "survivor",
        "survivor.jpg",
        "入选",
        jpeg=jpeg,
        raw=old_raw,
    )
    jpeg_only = PhotoRecord(
        "survivor",
        "survivor.jpg",
        "",
        jpeg=jpeg,
        raw=None,
    )
    superseded = PhotoRecord(
        "superseded",
        "conflict.cr3",
        "",
        jpeg=None,
        raw=conflicting_raw,
    )
    unrelated = PhotoRecord(
        "unrelated",
        "conflict.cr3",
        "待定",
        jpeg=None,
        raw=conflicting_raw,
    )

    with ProjectStore.open(database) as store:
        store.upsert_photos((survivor, superseded, unrelated))
        store.upsert_photos(
            (
                jpeg_only,
                PhotoRecord(
                    "superseded",
                    "conflict.cr3",
                    "",
                    jpeg=None,
                    raw=None,
                ),
            )
        )
        store._connection.execute(  # type: ignore[attr-defined]
            """
            CREATE UNIQUE INDEX test_unique_live_path
            ON photo_assets(path)
            WHERE photo_id != 'superseded'
            """
        )
        before_photos = store.list_photos()
        before_assets = store._connection.execute(  # type: ignore[attr-defined]
            """
            SELECT photo_id, kind, path, size, mtime_ns, present
            FROM photo_assets ORDER BY photo_id, kind
            """
        ).fetchall()

        with pytest.raises(sqlite3.IntegrityError):
            store.reconcile_photos(
                (
                    project_store.PhotoReconciliation(
                        jpeg_only,
                        superseded_photo_ids=("superseded",),
                    ),
                )
            )

        assert store.list_photos() == before_photos
        assert store._connection.execute(  # type: ignore[attr-defined]
            """
            SELECT photo_id, kind, path, size, mtime_ns, present
            FROM photo_assets ORDER BY photo_id, kind
            """
        ).fetchall() == before_assets


def test_save_marks_is_one_transaction_and_rolls_back_partial_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store.upsert_photos(
            (_photo("one", tmp_path), _photo("two", tmp_path))
        )
        transactions: list[str] = []
        store._connection.set_trace_callback(  # type: ignore[attr-defined]
            lambda statement: transactions.append(statement)
        )

        store.save_marks((("one", "入选"), ("two", "待定")))

        assert transactions.count("BEGIN IMMEDIATE") == 1
        assert store.get_photo("one").mark == "入选"
        assert store.get_photo("two").mark == "待定"

        with pytest.raises(KeyError, match="missing"):
            store.save_marks((("one", "淘汰"), ("missing", "入选")))
        assert store.get_photo("one").mark == "入选"


def test_save_analyses_is_one_transaction_and_rolls_back_partial_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store.upsert_photos(
            (_photo("one", tmp_path), _photo("two", tmp_path))
        )
        transactions: list[str] = []
        store._connection.set_trace_callback(  # type: ignore[attr-defined]
            lambda statement: transactions.append(statement)
        )

        store.save_analyses(
            (
                (
                    "one",
                    "blur",
                    AnalysisStatus.RUNNING,
                    None,
                    None,
                ),
                (
                    "two",
                    "face",
                    AnalysisStatus.RUNNING,
                    None,
                    None,
                ),
            )
        )

        assert transactions.count("BEGIN IMMEDIATE") == 1
        assert store.get_analysis("one", "blur").status is AnalysisStatus.RUNNING
        assert store.get_analysis("two", "face").status is AnalysisStatus.RUNNING

        with pytest.raises(sqlite3.IntegrityError):
            store.save_analyses(
                (
                    (
                        "one",
                        "blur",
                        AnalysisStatus.SUCCEEDED,
                        {"score": 1},
                        None,
                    ),
                    (
                        "missing",
                        "blur",
                        AnalysisStatus.RUNNING,
                        None,
                        None,
                    ),
                )
            )
        assert store.get_analysis("one", "blur").status is AnalysisStatus.RUNNING


def test_commit_failure_rolls_back_and_leaves_store_usable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store._connection.executescript(  # type: ignore[attr-defined]
            """
            CREATE TABLE deferred_parent (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE deferred_child (
                parent_id TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES deferred_parent(id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER fail_photo_commit
            AFTER INSERT ON photos
            BEGIN
                INSERT INTO deferred_child (parent_id) VALUES ('missing');
            END;
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            store.upsert_photos([_photo("one", tmp_path)])

        assert store.list_photos() == []
        assert store.upsert_photos([]) == {}


def test_recover_interrupted_only_resets_running_analyses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"

    with ProjectStore.open(database) as store:
        store.upsert_photos([_photo("one", tmp_path)])
        for analyzer, status in (
            ("pending", AnalysisStatus.PENDING),
            ("running-a", AnalysisStatus.RUNNING),
            ("running-b", AnalysisStatus.RUNNING),
            ("done", AnalysisStatus.SUCCEEDED),
            ("failed", AnalysisStatus.FAILED),
            ("canceled", AnalysisStatus.CANCELED),
        ):
            store.save_analysis("one", analyzer, status, None, None)

    with ProjectStore.open(database) as store:
        assert store.recover_interrupted() == 2
        assert {
            item.analyzer: item.status for item in store.list_analyses("one")
        } == {
            "canceled": AnalysisStatus.CANCELED,
            "done": AnalysisStatus.SUCCEEDED,
            "failed": AnalysisStatus.FAILED,
            "pending": AnalysisStatus.PENDING,
            "running-a": AnalysisStatus.PENDING,
            "running-b": AnalysisStatus.PENDING,
        }
        assert store.recover_interrupted() == 0


def test_saving_analysis_replaces_stale_failure_and_preserves_json_zeroes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    failure = AnalysisFailure("blur", "failed", "ValueError")

    with ProjectStore.open(database) as store:
        store.upsert_photos([_photo("one", tmp_path)])
        store.save_analysis(
            "one", "blur", AnalysisStatus.FAILED, None, failure
        )
        store.save_analysis(
            "one",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"zero": 0, "false": False, "empty": "", "items": []},
            None,
        )

        assert store.get_analysis("one", "blur") == StoredAnalysis(
            photo_id="one",
            analyzer="blur",
            status=AnalysisStatus.SUCCEEDED,
            result={"zero": 0, "false": False, "empty": "", "items": []},
            failure=None,
        )


def test_open_creates_one_stable_project_and_context_manager_closes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "new" / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store.upsert_photos([_photo("one", tmp_path), _photo("two", tmp_path)])

    assert database.is_file()
    with pytest.raises(RuntimeError, match="closed"):
        store.list_photos()

    ProjectStore.open(database).close()
    with sqlite3.connect(database) as connection:
        project_rows = connection.execute(
            "SELECT id FROM projects"
        ).fetchall()
        photo_projects = connection.execute(
            "SELECT DISTINCT project_id FROM photos"
        ).fetchall()
    assert len(project_rows) == 1
    assert photo_projects == project_rows


def test_open_rejects_a_second_live_store_and_reopens_after_close(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    first = ProjectStore.open(database)

    with pytest.raises(RuntimeError, match="already open.*close it and retry"):
        ProjectStore.open(database)

    first.close()
    first.close()

    reopened = ProjectStore.open(database)
    reopened.close()


def test_open_rejects_hardlink_alias_of_a_live_project(
    tmp_path: Path,
) -> None:
    database = tmp_path / "project.sqlite3"
    alias = tmp_path / "project-alias.sqlite3"
    first = ProjectStore.open(database)
    os.link(database, alias)

    try:
        with pytest.raises(RuntimeError, match="multiple hard links"):
            ProjectStore.open(alias)
    finally:
        first.close()
        alias.unlink()

    ProjectStore.open(database).close()


def test_store_rejects_public_calls_from_non_owner_thread(
    tmp_path: Path,
) -> None:
    store = ProjectStore.open(tmp_path / "project.sqlite3")

    with ThreadPoolExecutor(max_workers=1) as executor:
        read_error = executor.submit(store.list_photos).exception()
        close_error = executor.submit(store.close).exception()

    assert isinstance(read_error, RuntimeError)
    assert "owning thread" in str(read_error)
    assert isinstance(close_error, RuntimeError)
    assert "owning thread" in str(close_error)
    store.close()


def test_culling_notice_setting_is_scoped_to_the_open_project(
    tmp_path: Path,
) -> None:
    store = ProjectStore.open(tmp_path / "project.sqlite3")
    store._connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO projects (id, root_path) VALUES ('foreign', '/foreign')"
    )
    store._connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO project_settings (project_id, key, value)
        VALUES ('foreign', 'culling_notice_confirmed', 'true')
        """
    )
    store._connection.commit()  # type: ignore[attr-defined]

    assert store.culling_notice_confirmed() is False
    store.set_culling_notice_confirmed(True)
    rows = store._connection.execute(  # type: ignore[attr-defined]
        """
        SELECT project_id, value FROM project_settings
        WHERE key = 'culling_notice_confirmed'
        ORDER BY project_id
        """
    ).fetchall()
    assert rows == [("foreign", "true"), ("project", "true")]
    store.close()


def test_delete_photos_removes_photo_and_cascaded_assets(
    tmp_path: Path,
) -> None:
    store = ProjectStore.open(tmp_path / "project.sqlite3")
    one = _photo("one", tmp_path)
    two = _photo("two", tmp_path)
    store.upsert_photos([one, two])

    store.delete_photos(("one",))

    assert store.get_photo("one") is None
    assert store.get_photo("two") == two
    assert store.photo_ids_by_asset_paths([one.jpeg.fingerprint.path]) == {}
    store.close()


def test_delete_photos_rolls_back_when_any_id_is_missing(
    tmp_path: Path,
) -> None:
    store = ProjectStore.open(tmp_path / "project.sqlite3")
    one = _photo("one", tmp_path)
    store.upsert_photos([one])

    with pytest.raises(KeyError):
        store.delete_photos(("one", "missing"))

    assert store.get_photo("one") == one
    store.close()


def test_stored_analysis_is_frozen() -> None:
    analysis = StoredAnalysis(
        photo_id="one",
        analyzer="blur",
        status=AnalysisStatus.PENDING,
        result=None,
        failure=None,
    )

    with pytest.raises(FrozenInstanceError):
        analysis.status = AnalysisStatus.RUNNING  # type: ignore[misc]


def test_list_photos_orders_by_filename_naturally_not_by_uuid(tmp_path: Path) -> None:
    database = tmp_path / "project.sqlite3"
    with ProjectStore.open(database) as store:
        store.upsert_photos(
            [
                _photo("c", tmp_path, filename="IMG_10.jpg"),
                _photo("a", tmp_path, filename="IMG_2.jpg"),
                _photo("b", tmp_path, filename="IMG_1.jpg"),
            ]
        )
    with ProjectStore.open(database) as reopened:
        assert [photo.id for photo in reopened.list_photos()] == [
            "b",
            "a",
            "c",
        ]
