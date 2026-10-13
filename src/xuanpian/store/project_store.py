"""Single-writer SQLite persistence for one photo project."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterator, Sequence

from ..domain.analysis import AnalysisFailure, AnalysisStatus
from ..domain.culling import PhotoMark, normalize_legacy_mark
from ..domain.technical_analysis import TECHNICAL_ALGORITHM_VERSION
from ..domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
    natural_sort_key,
)
from ..domain.photo_move import MoveItem, PhotoMoveTransaction
from .database_lock import ProjectDatabaseLock
from .migrations import migrate_database
from .schema import create_schema, validate_schema


class PhotoChange(str, Enum):
    NEW = "new"
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    MISSING = "missing"


class PhotoMarkConflictError(RuntimeError):
    """Two logical photos with different user marks cannot auto-merge."""


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    photo_id: str
    analyzer: str
    status: AnalysisStatus
    result: object | None
    failure: AnalysisFailure | None


@dataclass(frozen=True, slots=True)
class PhotoReconciliation:
    """One survivor upsert and the identities it atomically replaces."""

    record: PhotoRecord
    superseded_photo_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PhotoReconcileResult:
    """Committed persistence change and its resolved user mark."""

    change: PhotoChange
    resolved_mark: str


@dataclass(frozen=True, slots=True)
class PendingPhotoMove:
    """A frozen move plan plus its durable per-item completion evidence."""

    transaction: PhotoMoveTransaction
    completed_ordinals: frozenset[int]


class ProjectStore:
    """A SQLite project store owned by the thread that opened it."""

    _DEFAULT_PROJECT_ID = "project"

    def __init__(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        root_path: str,
        database_lock: ProjectDatabaseLock,
    ) -> None:
        self._connection = connection
        self._project_id = project_id
        self._root_path = root_path
        self._database_lock = database_lock
        self._owner_thread_id = threading.get_ident()
        self._closed = False

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        root_path: Path | None = None,
    ) -> ProjectStore:
        database_path = Path(path).expanduser().resolve()
        resolved_root = str(
            (
                Path(root_path).expanduser().resolve()
                if root_path is not None
                else database_path.parent
            )
        )
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_lock = ProjectDatabaseLock.acquire(database_path)
        connection: sqlite3.Connection | None = None
        try:
            database_lock.assert_safe_database_link()
            is_fresh = (
                not database_path.exists()
                or (
                    database_path.is_file()
                    and database_path.stat().st_size == 0
                )
            )
            if not is_fresh:
                migrate_database(database_path, lock=database_lock)
            connection = sqlite3.connect(database_path)
            if is_fresh:
                create_schema(connection)
            else:
                connection.execute("PRAGMA foreign_keys = ON")
                validate_schema(connection)
            connection.commit()
            projects = connection.execute(
                "SELECT id FROM projects ORDER BY id"
            ).fetchall()
            if len(projects) > 1:
                raise RuntimeError("project database contains multiple projects")
            if projects:
                project_id = str(projects[0][0])
                if root_path is not None:
                    connection.execute(
                        "UPDATE projects SET root_path = ? WHERE id = ?",
                        (resolved_root, project_id),
                    )
                    connection.commit()
            else:
                project_id = cls._DEFAULT_PROJECT_ID
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """
                        INSERT INTO projects (id, root_path)
                        VALUES (?, ?)
                        """,
                        (project_id, resolved_root),
                    )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            stored_root = connection.execute(
                "SELECT root_path FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if stored_root is None:
                raise RuntimeError("project database has no project root")
            return cls(
                connection,
                project_id,
                str(stored_root[0]),
                database_lock,
            )
        except BaseException:
            try:
                if connection is not None:
                    connection.close()
            finally:
                database_lock.release()
            raise

    def __enter__(self) -> ProjectStore:
        self._ensure_open_on_owner_thread()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    @property
    def root_path(self) -> str:
        self._ensure_open_on_owner_thread()
        return self._root_path

    def upsert_photos(
        self, records: Sequence[PhotoRecord]
    ) -> dict[str, PhotoChange]:
        self._ensure_open_on_owner_thread()
        changes: dict[str, PhotoChange] = {}
        with self._transaction():
            for record in records:
                changes[record.id] = self._upsert_photo(record)
        return changes

    def reconcile_photo(
        self,
        record: PhotoRecord,
        *,
        superseded_photo_ids: Sequence[str] = (),
    ) -> PhotoChange:
        """Atomically upsert a survivor and remove merged identities."""
        return self.reconcile_photos(
            (
                PhotoReconciliation(
                    record,
                    tuple(superseded_photo_ids),
                ),
            )
        )[0]

    def reconcile_photos(
        self,
        reconciliations: Sequence[PhotoReconciliation],
    ) -> tuple[PhotoChange, ...]:
        """Apply a batch of survivor upserts and merged-ID removals once."""
        return tuple(
            result.change
            for result in self.reconcile_photos_with_marks(reconciliations)
        )

    def reconcile_photos_with_marks(
        self,
        reconciliations: Sequence[PhotoReconciliation],
    ) -> tuple[PhotoReconcileResult, ...]:
        """Apply a batch and return each committed resolved user mark."""
        self._ensure_open_on_owner_thread()
        results: list[PhotoReconcileResult] = []
        with self._transaction():
            for reconciliation in reconciliations:
                record = self._record_with_merged_mark(reconciliation)
                superseded_ids = tuple(
                    photo_id
                    for photo_id in dict.fromkeys(
                        reconciliation.superseded_photo_ids
                    )
                    if photo_id != record.id
                )
                superseded_assets = self._latest_superseded_assets(
                    superseded_ids
                )
                for photo_id in superseded_ids:
                    self._connection.execute(
                        """
                        DELETE FROM photos
                        WHERE id = ? AND project_id = ?
                        """,
                        (photo_id, self._project_id),
                    )
                change = self._upsert_photo(record)
                self._connection.execute(
                    """
                    UPDATE photos SET mark = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (record.mark, record.id, self._project_id),
                )
                self._adopt_superseded_assets(
                    record.id,
                    superseded_assets,
                )
                results.append(
                    PhotoReconcileResult(change, record.mark)
                )
        return tuple(results)

    def _latest_superseded_assets(
        self,
        superseded_ids: Sequence[str],
    ) -> dict[str, tuple[str, int, int, str]]:
        """Choose the last explicitly ordered superseded identity per kind."""
        latest: dict[str, tuple[str, int, int, str]] = {}
        for photo_id in superseded_ids:
            rows = self._connection.execute(
                """
                SELECT kind, path, size, mtime_ns, origin_path
                FROM photo_assets
                WHERE photo_id = ?
                ORDER BY kind
                """,
                (photo_id,),
            ).fetchall()
            for kind, path, size, mtime_ns, origin_path in rows:
                latest[str(kind)] = (
                    str(path),
                    int(size),
                    int(mtime_ns),
                    str(origin_path),
                )
        return latest

    def _adopt_superseded_assets(
        self,
        survivor_id: str,
        assets: dict[str, tuple[str, int, int, str]],
    ) -> None:
        """Retain known missing paths without replacing a present asset."""
        for kind in sorted(assets):
            path, size, mtime_ns, origin_path = assets[kind]
            row = self._connection.execute(
                """
                SELECT present FROM photo_assets
                WHERE photo_id = ? AND kind = ?
                """,
                (survivor_id, kind),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO photo_assets (
                        photo_id, kind, path, size, mtime_ns, present,
                        origin_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        survivor_id,
                        kind,
                        path,
                        size,
                        mtime_ns,
                        0,
                        origin_path,
                    ),
                )
            elif int(row[0]) == 0:
                self._connection.execute(
                    """
                    UPDATE photo_assets
                    SET path = ?, size = ?, mtime_ns = ?, present = 0,
                        origin_path = ?
                    WHERE photo_id = ? AND kind = ? AND present = 0
                    """,
                    (
                        path,
                        size,
                        mtime_ns,
                        origin_path,
                        survivor_id,
                        kind,
                    ),
                )

    def _record_with_merged_mark(
        self,
        reconciliation: PhotoReconciliation,
    ) -> PhotoRecord:
        record = reconciliation.record
        ids = tuple(
            dict.fromkeys(
                (record.id, *reconciliation.superseded_photo_ids)
            )
        )
        placeholders = ",".join("?" for _ in ids)
        rows = self._connection.execute(
            f"""
            SELECT id, mark FROM photos
            WHERE project_id = ? AND id IN ({placeholders})
            """,
            (self._project_id, *ids),
        ).fetchall()
        marks: set[PhotoMark] = set()
        managed_spellings: set[str] = set()
        unmanaged_ids: list[str] = []
        for photo_id, raw_mark in rows:
            if not isinstance(raw_mark, str):
                unmanaged_ids.append(str(photo_id))
                continue
            mark = normalize_legacy_mark(raw_mark)
            if mark is None:
                unmanaged_ids.append(str(photo_id))
            elif mark is not PhotoMark.UNPROCESSED:
                marks.add(mark)
                managed_spellings.add(raw_mark)
        if unmanaged_ids:
            raise PhotoMarkConflictError(
                "cannot automatically merge photos with unmanaged marks: "
                f"{sorted(unmanaged_ids)!r}"
            )
        if len(marks) > 1:
            involved = sorted(str(photo_id) for photo_id, _mark in rows)
            raise PhotoMarkConflictError(
                "cannot automatically merge photos with conflicting marks: "
                f"{involved!r}"
            )
        if marks:
            semantic_mark = next(iter(marks))
            resolved_mark = (
                semantic_mark.value
                if len(managed_spellings) > 1
                else next(iter(managed_spellings))
            )
            return replace(record, mark=resolved_mark)
        return record

    def photo_ids_by_asset_paths(
        self,
        paths: Sequence[str],
    ) -> dict[str, str]:
        """Return exact persisted identities for normalized source paths."""
        self._ensure_open_on_owner_thread()
        normalized = tuple(
            str(Path(path).expanduser().resolve())
            for path in dict.fromkeys(paths)
        )
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        rows = self._connection.execute(
            f"""
            SELECT assets.path, assets.photo_id
            FROM photo_assets AS assets
            JOIN photos ON photos.id = assets.photo_id
            WHERE photos.project_id = ?
              AND assets.path IN ({placeholders})
            """,
            (self._project_id, *normalized),
        ).fetchall()
        return {str(path): str(photo_id) for path, photo_id in rows}

    def removed_asset_paths(
        self,
        paths: Sequence[str],
    ) -> set[str]:
        """Return normalized candidate paths excluded by user tombstones."""
        self._ensure_open_on_owner_thread()
        normalized = tuple(
            str(Path(path).expanduser().resolve())
            for path in dict.fromkeys(paths)
        )
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        rows = self._connection.execute(
            f"""
            SELECT assets.path
            FROM removed_photo_assets AS assets
            JOIN removed_photos AS removed ON removed.id = assets.photo_id
            WHERE removed.project_id = ?
              AND assets.path IN ({placeholders})
            """,
            (self._project_id, *normalized),
        ).fetchall()
        return {str(path) for (path,) in rows}

    def list_photos(self) -> list[PhotoRecord]:
        self._ensure_open_on_owner_thread()
        rows = self._connection.execute(
            """
            SELECT id FROM photos
            WHERE project_id = ?
            ORDER BY id
            """,
            (self._project_id,),
        ).fetchall()
        photos = [
            photo
            for (photo_id,) in rows
            if (photo := self._load_photo(str(photo_id))) is not None
        ]
        return sorted(
            photos,
            key=lambda photo: (natural_sort_key(photo.filename), photo.id),
        )

    def get_photo(self, photo_id: str) -> PhotoRecord | None:
        """Return one persisted logical photo by stable identity."""
        self._ensure_open_on_owner_thread()
        return self._load_photo(photo_id)

    def save_mark(self, photo_id: str, mark: str) -> None:
        self.save_marks(((photo_id, mark),))

    def save_marks(
        self,
        marks: Sequence[tuple[str, str]],
    ) -> None:
        """Persist a user-visible marking command atomically."""
        self._ensure_open_on_owner_thread()
        with self._transaction():
            for photo_id, mark in marks:
                cursor = self._connection.execute(
                    """
                    UPDATE photos SET mark = ?
                    WHERE id = ? AND project_id = ?
                    """,
                    (mark, photo_id, self._project_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(photo_id)

    def begin_photo_move(self, transaction: PhotoMoveTransaction) -> None:
        """Atomically persist a validated move plan and all ordered items."""
        self._ensure_open_on_owner_thread()
        if not isinstance(transaction, PhotoMoveTransaction):
            raise ValueError("transaction must be a PhotoMoveTransaction")
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM photo_move_transactions WHERE id = ?",
                (transaction.id,),
            ).fetchone() is not None:
                raise RuntimeError(
                    f"move transaction already exists: {transaction.id}"
                )
            if self._connection.execute(
                """
                SELECT 1 FROM photo_move_transactions
                WHERE photo_id = ?
                """,
                (transaction.photo_id,),
            ).fetchone() is not None:
                raise RuntimeError(
                    f"photo already has a pending move: {transaction.photo_id}"
                )
            self._move_before_mark(transaction)
            self._validate_move_source_assets(transaction)

            cursor = self._connection.execute(
                """
                INSERT INTO photo_move_transactions (
                    id, project_id, photo_id, before_mark, after_mark,
                    created_at_ns, inverse_of_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transaction.id,
                    self._project_id,
                    transaction.photo_id,
                    transaction.before_mark.value,
                    transaction.after_mark.value,
                    transaction.created_at_ns,
                    self._encode_move_snapshot(transaction.inverse_of),
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("failed to insert move transaction")
            for item in transaction.items:
                cursor = self._connection.execute(
                    """
                    INSERT INTO photo_move_items (
                        transaction_id, ordinal, kind, source_path,
                        target_path, size, mtime_ns, completed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        transaction.id,
                        item.ordinal,
                        item.kind.value,
                        item.source_path,
                        item.target_path,
                        item.size,
                        item.mtime_ns,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("failed to insert move item")

    def mark_move_item_completed(
        self,
        transaction_id: str,
        ordinal: int,
    ) -> None:
        """Durably mark one item complete; repeating the call is idempotent."""
        self._ensure_open_on_owner_thread()
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise ValueError("ordinal must be a non-negative integer")
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE photo_move_items SET completed = 1
                WHERE transaction_id = ? AND ordinal = ?
                  AND EXISTS (
                      SELECT 1 FROM photo_move_transactions AS moves
                      JOIN photos ON photos.id = moves.photo_id
                      WHERE moves.id = photo_move_items.transaction_id
                        AND moves.project_id = ?
                        AND photos.project_id = ?
                  )
                """,
                (
                    transaction_id,
                    ordinal,
                    self._project_id,
                    self._project_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError((transaction_id, ordinal))

    def commit_photo_move(
        self,
        transaction_id: str,
        assets: Sequence[PhotoAssetRecord],
    ) -> None:
        """Atomically publish a completed move and remove its journal."""
        self._ensure_open_on_owner_thread()
        with self._transaction():
            committed_assets = tuple(assets)
            pending, before_mark = self._load_validated_pending_move(
                transaction_id
            )
            transaction = pending.transaction
            expected_ordinals = frozenset(
                item.ordinal for item in transaction.items
            )
            if pending.completed_ordinals != expected_ordinals:
                raise RuntimeError("move transaction is incomplete")
            self._validate_move_commit_assets(transaction, committed_assets)

            for asset in committed_assets:
                cursor = self._connection.execute(
                    """
                    UPDATE photo_assets
                    SET path = ?, size = ?, mtime_ns = ?, present = 1
                    WHERE photo_id = ? AND kind = ?
                      AND EXISTS (
                          SELECT 1 FROM photos
                          WHERE photos.id = photo_assets.photo_id
                            AND photos.project_id = ?
                      )
                    """,
                    (
                        asset.fingerprint.path,
                        asset.fingerprint.size,
                        asset.fingerprint.mtime_ns,
                        transaction.photo_id,
                        asset.kind.value,
                        self._project_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("move transaction assets changed")
            cursor = self._connection.execute(
                """
                UPDATE photos SET mark = ?
                WHERE id = ? AND project_id = ? AND mark = ?
                """,
                (
                    transaction.after_mark.value,
                    transaction.photo_id,
                    self._project_id,
                    before_mark,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("move transaction before mark is stale")
            cursor = self._connection.execute(
                """
                DELETE FROM photo_move_transactions
                WHERE id = ? AND project_id = ?
                  AND EXISTS (
                      SELECT 1 FROM photos
                      WHERE photos.id = photo_move_transactions.photo_id
                        AND photos.project_id = ?
                  )
                """,
                (transaction_id, self._project_id, self._project_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("move transaction disappeared")

    def abort_photo_move(self, transaction_id: str) -> None:
        """Discard an owned journal without changing photo state."""
        self._ensure_open_on_owner_thread()
        with self._transaction():
            cursor = self._connection.execute(
                """
                DELETE FROM photo_move_transactions
                WHERE id = ? AND project_id = ?
                  AND EXISTS (
                      SELECT 1 FROM photos
                      WHERE photos.id = photo_move_transactions.photo_id
                        AND photos.project_id = ?
                  )
                """,
                (transaction_id, self._project_id, self._project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(transaction_id)

    def is_photo_move_uncommitted(
        self, transaction: PhotoMoveTransaction, before_record: PhotoRecord,
    ) -> bool:
        """Prove retained journal and complete unchanged photo in one snapshot.

        Missing journal or changed state cannot establish non-commit. Read or
        transaction errors propagate so callers fail closed instead of moving
        files based on an ambiguous persistence outcome.
        """
        self._ensure_open_on_owner_thread()
        with self._transaction():
            try:
                pending, _ = self._load_validated_pending_move(transaction.id)
            except KeyError:
                return False
            return (
                pending.transaction == transaction
                and self.get_photo(transaction.photo_id) == before_record
            )

    def list_pending_photo_moves(self) -> tuple[PendingPhotoMove, ...]:
        """Return owned journals in stable creation order."""
        self._ensure_open_on_owner_thread()
        rows = self._connection.execute(
            """
            SELECT id FROM photo_move_transactions
            WHERE project_id = ?
            ORDER BY created_at_ns, id
            """,
            (self._project_id,),
        ).fetchall()
        return tuple(
            self._load_validated_pending_move(str(row[0]))[0]
            for row in rows
        )

    def culling_notice_confirmed(self) -> bool:
        """Return the project-scoped notice flag, defaulting to false."""
        self._ensure_open_on_owner_thread()
        row = self._connection.execute(
            """
            SELECT value FROM project_settings
            WHERE project_id = ? AND key = 'culling_notice_confirmed'
            """,
            (self._project_id,),
        ).fetchone()
        if row is None:
            return False
        if row[0] == "true":
            return True
        if row[0] == "false":
            return False
        raise RuntimeError("invalid boolean project setting")

    def set_culling_notice_confirmed(self, value: bool) -> None:
        """Atomically store the project-scoped notice flag."""
        self._ensure_open_on_owner_thread()
        if not isinstance(value, bool):
            raise ValueError("culling notice setting must be a boolean")
        with self._transaction():
            cursor = self._connection.execute(
                """
                INSERT INTO project_settings (project_id, key, value)
                VALUES (?, 'culling_notice_confirmed', ?)
                ON CONFLICT (project_id, key) DO UPDATE SET
                    value = excluded.value
                """,
                (self._project_id, "true" if value else "false"),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("failed to persist culling notice setting")

    def delete_photos(self, photo_ids: Sequence[str]) -> None:
        """Remove photos from the project while retaining scan tombstones."""
        self._ensure_open_on_owner_thread()
        with self._transaction():
            for photo_id in dict.fromkeys(photo_ids):
                row = self._connection.execute(
                    """
                    SELECT filename, mark FROM photos
                    WHERE id = ? AND project_id = ?
                    """,
                    (photo_id, self._project_id),
                ).fetchone()
                if row is None:
                    raise KeyError(photo_id)
                self._connection.execute(
                    """
                    INSERT INTO removed_photos (
                        id, project_id, filename, mark
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        project_id = excluded.project_id,
                        filename = excluded.filename,
                        mark = excluded.mark
                    """,
                    (photo_id, self._project_id, str(row[0]), str(row[1])),
                )
                self._connection.execute(
                    "DELETE FROM removed_photo_assets WHERE photo_id = ?",
                    (photo_id,),
                )
                self._connection.execute(
                    """
                    INSERT INTO removed_photo_assets (
                        photo_id, kind, path, size, mtime_ns, present,
                        origin_path
                    )
                    SELECT photo_id, kind, path, size, mtime_ns, present,
                        origin_path
                    FROM photo_assets
                    WHERE photo_id = ?
                    """,
                    (photo_id,),
                )
                cursor = self._connection.execute(
                    """
                    DELETE FROM photos
                    WHERE id = ? AND project_id = ?
                    """,
                    (photo_id, self._project_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(photo_id)

    def reinclude_photos(self, photo_ids: Sequence[str]) -> None:
        """Explicitly restore removed identities so scans may include them."""
        self._ensure_open_on_owner_thread()
        with self._transaction():
            for photo_id in dict.fromkeys(photo_ids):
                row = self._connection.execute(
                    """
                    SELECT filename, mark FROM removed_photos
                    WHERE id = ? AND project_id = ?
                    """,
                    (photo_id, self._project_id),
                ).fetchone()
                if row is None:
                    raise KeyError(photo_id)
                self._connection.execute(
                    """
                    INSERT INTO photos (id, project_id, filename, mark)
                    VALUES (?, ?, ?, ?)
                    """,
                    (photo_id, self._project_id, str(row[0]), str(row[1])),
                )
                self._connection.execute(
                    """
                    INSERT INTO photo_assets (
                        photo_id, kind, path, size, mtime_ns, present,
                        origin_path
                    )
                    SELECT photo_id, kind, path, size, mtime_ns, present,
                        origin_path
                    FROM removed_photo_assets
                    WHERE photo_id = ?
                    """,
                    (photo_id,),
                )
                self._connection.execute(
                    "DELETE FROM removed_photos WHERE id = ?",
                    (photo_id,),
                )

    def restore_photos(self, photo_ids: Sequence[str]) -> None:
        """Compatibility spelling for explicit project re-inclusion."""
        self.reinclude_photos(photo_ids)

    def save_analysis(
        self,
        photo_id: str,
        analyzer: str,
        status: AnalysisStatus,
        result: object | None,
        failure: AnalysisFailure | None,
    ) -> None:
        self.save_analyses(
            ((photo_id, analyzer, status, result, failure),)
        )

    def save_analyses(
        self,
        analyses: Sequence[
            tuple[
                str,
                str,
                AnalysisStatus,
                object | None,
                AnalysisFailure | None,
            ]
        ],
    ) -> None:
        """Persist a set of analysis transitions atomically."""
        self._ensure_open_on_owner_thread()
        items = tuple(analyses)
        if not items:
            return
        with self._transaction():
            for photo_id, analyzer, status, result, failure in items:
                self._save_analysis_row(
                    photo_id,
                    analyzer,
                    status,
                    result,
                    failure,
                )

    def _save_analysis_row(
        self,
        photo_id: str,
        analyzer: str,
        status: AnalysisStatus,
        result: object | None,
        failure: AnalysisFailure | None,
    ) -> None:
        result_json = (
            None
            if result is None
            else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        self._connection.execute(
            """
            INSERT INTO analysis_results (
                photo_id, analyzer, status, result_json
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (photo_id, analyzer) DO UPDATE SET
                status = excluded.status,
                result_json = excluded.result_json
            """,
            (photo_id, analyzer, status.value, result_json),
        )
        self._connection.execute(
            """
            DELETE FROM analysis_failures
            WHERE photo_id = ? AND analyzer = ?
            """,
            (photo_id, analyzer),
        )
        if failure is not None:
            self._connection.execute(
                """
                INSERT INTO analysis_failures (
                    photo_id, analyzer, message, exception_type
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    photo_id,
                    analyzer,
                    failure.message,
                    failure.exception_type,
                ),
            )

    def get_analysis(
        self, photo_id: str, analyzer: str
    ) -> StoredAnalysis | None:
        self._ensure_open_on_owner_thread()
        row = self._connection.execute(
            """
            SELECT
                results.status,
                results.result_json,
                failures.message,
                failures.exception_type
            FROM analysis_results AS results
            LEFT JOIN analysis_failures AS failures
              ON failures.photo_id = results.photo_id
             AND failures.analyzer = results.analyzer
            WHERE results.photo_id = ? AND results.analyzer = ?
            """,
            (photo_id, analyzer),
        ).fetchone()
        if row is None:
            return None
        return self._stored_analysis(photo_id, analyzer, row)

    def get_current_technical_analysis(self, photo_id: str) -> StoredAnalysis | None:
        """Return a valid terminal state for the current technical contract."""
        analysis = self.get_analysis(photo_id, "technical")
        if analysis is None:
            return None
        if analysis.status is AnalysisStatus.FAILED:
            if analysis.result is None and analysis.failure is not None:
                return analysis
            return None
        if analysis.status is AnalysisStatus.CANCELED:
            if analysis.result is None and analysis.failure is None:
                return analysis
            return None
        if analysis.status is not AnalysisStatus.SUCCEEDED:
            return None
        if not isinstance(analysis.result, dict):
            return None
        if analysis.result.get("algorithm_version") != TECHNICAL_ALGORITHM_VERSION:
            return None
        return analysis

    def list_analyses(self, photo_id: str) -> list[StoredAnalysis]:
        self._ensure_open_on_owner_thread()
        rows = self._connection.execute(
            """
            SELECT
                results.analyzer,
                results.status,
                results.result_json,
                failures.message,
                failures.exception_type
            FROM analysis_results AS results
            LEFT JOIN analysis_failures AS failures
              ON failures.photo_id = results.photo_id
             AND failures.analyzer = results.analyzer
            WHERE results.photo_id = ?
            ORDER BY results.analyzer
            """,
            (photo_id,),
        ).fetchall()
        return [
            self._stored_analysis(photo_id, str(analyzer), values)
            for analyzer, *values in rows
        ]

    def recover_interrupted(self) -> int:
        self._ensure_open_on_owner_thread()
        with self._transaction():
            cursor = self._connection.execute(
                """
                UPDATE analysis_results SET status = ?
                WHERE status = ?
                """,
                (
                    AnalysisStatus.PENDING.value,
                    AnalysisStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount

    def close(self) -> None:
        self._ensure_owner_thread()
        if self._closed:
            return
        try:
            self._connection.close()
        finally:
            self._closed = True
            self._database_lock.release()

    def _ensure_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError(
                "ProjectStore may only be used from its owning thread"
            )

    def _ensure_open_on_owner_thread(self) -> None:
        self._ensure_owner_thread()
        if self._closed:
            raise RuntimeError("ProjectStore is closed")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction:
            raise RuntimeError("nested project store transaction")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _load_pending_move(self, transaction_id: str) -> PendingPhotoMove:
        row = self._connection.execute(
            """
            SELECT photo_id, before_mark, after_mark, created_at_ns, inverse_of_json
            FROM photo_move_transactions
            WHERE id = ? AND project_id = ?
            """,
            (transaction_id, self._project_id),
        ).fetchone()
        if row is None:
            raise KeyError(transaction_id)
        if self._connection.execute(
            """
            SELECT 1 FROM photos
            WHERE id = ? AND project_id = ?
            """,
            (row[0], self._project_id),
        ).fetchone() is None:
            raise RuntimeError("move transaction photo ownership mismatch")
        item_rows = self._connection.execute(
            """
            SELECT ordinal, kind, source_path, target_path, size, mtime_ns,
                   completed
            FROM photo_move_items
            WHERE transaction_id = ?
            ORDER BY ordinal
            """,
            (transaction_id,),
        ).fetchall()
        try:
            created_at_ns = self._persisted_non_negative_integer(
                row[3],
                "created_at_ns",
            )
            items: list[MoveItem] = []
            completed_ordinals: set[int] = set()
            seen_ordinals: set[int] = set()
            for (
                ordinal_value,
                kind,
                source_path,
                target_path,
                size_value,
                mtime_ns_value,
                completed_value,
            ) in item_rows:
                ordinal = self._persisted_non_negative_integer(
                    ordinal_value,
                    "ordinal",
                )
                size = self._persisted_non_negative_integer(
                    size_value,
                    "size",
                )
                mtime_ns = self._persisted_non_negative_integer(
                    mtime_ns_value,
                    "mtime_ns",
                )
                completed = self._persisted_non_negative_integer(
                    completed_value,
                    "completed",
                )
                if completed not in (0, 1):
                    raise RuntimeError(
                        "invalid persisted move completed flag"
                    )
                if ordinal in seen_ordinals:
                    raise RuntimeError(
                        "invalid persisted move item ordinal collision"
                    )
                seen_ordinals.add(ordinal)
                items.append(
                    MoveItem(
                        ordinal=ordinal,
                        kind=AssetKind(kind),
                        source_path=source_path,
                        target_path=target_path,
                        size=size,
                        mtime_ns=mtime_ns,
                    )
                )
                if completed == 1:
                    completed_ordinals.add(ordinal)
            transaction = PhotoMoveTransaction(
                id=transaction_id,
                photo_id=row[0],
                before_mark=PhotoMark(row[1]),
                after_mark=PhotoMark(row[2]),
                created_at_ns=created_at_ns,
                items=tuple(items),
                inverse_of=self._decode_move_snapshot(row[4]),
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid persisted move transaction") from error
        return PendingPhotoMove(
            transaction=transaction,
            completed_ordinals=frozenset(completed_ordinals),
        )

    @staticmethod
    def _encode_move_snapshot(transaction: PhotoMoveTransaction | None) -> str | None:
        if transaction is None:
            return None
        if transaction.inverse_of is not None:
            raise ValueError("nested inverse snapshot is not permitted")
        return json.dumps({
            "id": transaction.id, "photo_id": transaction.photo_id,
            "before_mark": transaction.before_mark.value,
            "after_mark": transaction.after_mark.value,
            "created_at_ns": transaction.created_at_ns,
            "items": [{
                "ordinal": item.ordinal, "kind": item.kind.value,
                "source_path": item.source_path, "target_path": item.target_path,
                "size": item.size, "mtime_ns": item.mtime_ns,
            } for item in transaction.items],
        }, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _decode_move_snapshot(value: object) -> PhotoMoveTransaction | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid move snapshot")
        data = json.loads(value)
        if not isinstance(data, dict) or set(data) != {
            "id", "photo_id", "before_mark", "after_mark", "created_at_ns", "items",
        } or not isinstance(data["items"], list):
            raise ValueError("invalid or nested move snapshot")
        items = []
        for item in data["items"]:
            if not isinstance(item, dict) or set(item) != {
                "ordinal", "kind", "source_path", "target_path", "size", "mtime_ns",
            }:
                raise ValueError("invalid move snapshot item")
            items.append(MoveItem(**{**item, "kind": AssetKind(item["kind"])}))
        return PhotoMoveTransaction(
            id=data["id"], photo_id=data["photo_id"],
            before_mark=PhotoMark(data["before_mark"]),
            after_mark=PhotoMark(data["after_mark"]),
            created_at_ns=data["created_at_ns"], items=tuple(items),
        )

    def _load_validated_pending_move(
        self,
        transaction_id: str,
    ) -> tuple[PendingPhotoMove, str]:
        pending = self._load_pending_move(transaction_id)
        before_mark = self._move_before_mark(pending.transaction)
        self._validate_move_source_assets(pending.transaction)
        return pending, before_mark

    @staticmethod
    def _persisted_non_negative_integer(
        value: object,
        field_name: str,
    ) -> int:
        if type(value) is not int or value < 0:
            raise RuntimeError(
                f"invalid persisted move {field_name}: "
                "expected a non-negative SQLite INTEGER"
            )
        return value

    def _move_before_mark(self, transaction: PhotoMoveTransaction) -> str:
        row = self._connection.execute(
            """
            SELECT mark FROM photos
            WHERE id = ? AND project_id = ?
            """,
            (transaction.photo_id, self._project_id),
        ).fetchone()
        if row is None:
            raise KeyError(transaction.photo_id)
        raw_mark = row[0]
        if not isinstance(raw_mark, str):
            raise RuntimeError("photo mark is not managed")
        persisted_mark = normalize_legacy_mark(raw_mark)
        if persisted_mark is None:
            raise RuntimeError("photo mark is not managed")
        if persisted_mark is not transaction.before_mark:
            raise RuntimeError("move transaction before mark is stale")
        return raw_mark

    def _move_source_assets(
        self,
        transaction: PhotoMoveTransaction,
    ) -> dict[AssetKind, tuple[str, int, int, str]]:
        rows = self._connection.execute(
            """
            SELECT assets.kind, assets.path, assets.size, assets.mtime_ns,
                   assets.origin_path
            FROM photo_assets AS assets
            JOIN photos ON photos.id = assets.photo_id
            WHERE assets.photo_id = ? AND assets.present = 1
              AND photos.project_id = ?
            ORDER BY assets.kind
            """,
            (transaction.photo_id, self._project_id),
        ).fetchall()
        persisted: dict[AssetKind, tuple[str, int, int, str]] = {}
        try:
            for kind_value, path, size_value, mtime_value, origin_path in rows:
                if (
                    not isinstance(kind_value, str)
                    or not isinstance(path, str)
                    or not isinstance(origin_path, str)
                ):
                    raise RuntimeError("invalid persisted photo assets")
                kind = AssetKind(kind_value)
                size = self._persisted_non_negative_integer(
                    size_value,
                    "photo asset size",
                )
                mtime_ns = self._persisted_non_negative_integer(
                    mtime_value,
                    "photo asset mtime_ns",
                )
                asset = PhotoAssetRecord(
                    kind=kind,
                    fingerprint=FileFingerprint(
                        path=path,
                        size=size,
                        mtime_ns=mtime_ns,
                    ),
                    origin_path=origin_path,
                )
                if kind in persisted:
                    raise RuntimeError("invalid persisted photo assets")
                persisted[kind] = (
                    asset.fingerprint.path,
                    asset.fingerprint.size,
                    asset.fingerprint.mtime_ns,
                    asset.origin_path,
                )
        except (TypeError, ValueError) as error:
            raise RuntimeError("invalid persisted photo assets") from error
        return persisted

    def _validate_move_source_assets(
        self,
        transaction: PhotoMoveTransaction,
    ) -> None:
        persisted = self._move_source_assets(transaction)
        planned = {
            item.kind: (item.source_path, item.size, item.mtime_ns)
            for item in transaction.items
        }
        if len(planned) != len(transaction.items) or {
            kind: values[:3] for kind, values in persisted.items()
        } != planned:
            raise RuntimeError("move transaction source assets do not match photo")
        transaction.validate_inverse()
        reference = transaction.inverse_of or transaction
        for item in reference.items:
            origin = Path(persisted[item.kind][3])
            target = (
                origin if reference.after_mark is PhotoMark.UNPROCESSED
                else origin.parent / reference.after_mark.value / origin.name
            )
            allowed_sources = {origin}
            if reference.before_mark is not PhotoMark.UNPROCESSED:
                allowed_sources.add(origin.parent / reference.before_mark.value / origin.name)
            if Path(item.target_path) != target or Path(item.source_path) not in allowed_sources:
                raise RuntimeError("move transaction paths do not match immutable origin")

    def _validate_move_commit_assets(
        self,
        transaction: PhotoMoveTransaction,
        assets: Sequence[PhotoAssetRecord],
    ) -> None:
        if not all(isinstance(asset, PhotoAssetRecord) for asset in assets):
            raise RuntimeError("move transaction assets are invalid")
        provided = {asset.kind: asset for asset in assets}
        planned = {item.kind: item for item in transaction.items}
        persisted = self._move_source_assets(transaction)
        if (
            len(provided) != len(assets)
            or len(planned) != len(transaction.items)
            or provided.keys() != planned.keys()
            or provided.keys() != persisted.keys()
        ):
            raise RuntimeError("move transaction assets do not match journal")
        for kind, item in planned.items():
            asset = provided[kind]
            fingerprint = asset.fingerprint
            if (
                fingerprint.path != item.target_path
                or fingerprint.size != item.size
                or fingerprint.mtime_ns != item.mtime_ns
                or asset.origin_path != persisted[kind][3]
            ):
                raise RuntimeError("move transaction assets do not match journal")

    def _load_photo(self, photo_id: str) -> PhotoRecord | None:
        row = self._connection.execute(
            """
            SELECT filename, mark FROM photos
            WHERE id = ? AND project_id = ?
            """,
            (photo_id, self._project_id),
        ).fetchone()
        if row is None:
            return None
        assets = self._connection.execute(
            """
            SELECT kind, path, size, mtime_ns, origin_path FROM photo_assets
            WHERE photo_id = ? AND present = 1
            """,
            (photo_id,),
        ).fetchall()
        by_kind = {
            AssetKind(kind): PhotoAssetRecord(
                kind=AssetKind(kind),
                fingerprint=FileFingerprint(
                    path=str(path),
                    size=int(size),
                    mtime_ns=int(mtime_ns),
                ),
                origin_path=str(origin_path),
            )
            for kind, path, size, mtime_ns, origin_path in assets
        }
        return PhotoRecord(
            id=photo_id,
            filename=str(row[0]),
            mark=str(row[1]),
            jpeg=by_kind.get(AssetKind.JPEG),
            raw=by_kind.get(AssetKind.RAW),
        )

    def _upsert_photo(self, record: PhotoRecord) -> PhotoChange:
        existing = self._load_photo(record.id)
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO photos (
                    id, project_id, filename, mark
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    record.id,
                    self._project_id,
                    record.filename,
                    record.mark,
                ),
            )
            self._replace_assets(record)
            return (
                PhotoChange.NEW
                if record.source_assets
                else PhotoChange.MISSING
            )
        if not record.source_assets:
            self._update_filename(record)
            self._replace_assets(record)
            self._invalidate_analyses(record.id)
            return PhotoChange.MISSING
        if self._same_assets(existing, record):
            self._update_filename(record)
            return PhotoChange.UNCHANGED
        self._update_filename(record)
        self._replace_assets(record)
        self._invalidate_analyses(record.id)
        return PhotoChange.CHANGED

    @staticmethod
    def _same_assets(
        persisted: PhotoRecord, candidate: PhotoRecord
    ) -> bool:
        return (
            persisted.jpeg == candidate.jpeg
            and persisted.raw == candidate.raw
        )

    def _update_filename(self, record: PhotoRecord) -> None:
        self._connection.execute(
            """
            UPDATE photos SET filename = ?
            WHERE id = ? AND project_id = ?
            """,
            (record.filename, record.id, self._project_id),
        )

    def _replace_assets(self, record: PhotoRecord) -> None:
        self._connection.execute(
            "UPDATE photo_assets SET present = 0 WHERE photo_id = ?",
            (record.id,),
        )
        self._connection.executemany(
            """
            INSERT INTO photo_assets (
                photo_id, kind, path, size, mtime_ns, present, origin_path
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT (photo_id, kind) DO UPDATE SET
                path = excluded.path,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                present = 1
            """,
            [
                (
                    record.id,
                    asset.kind.value,
                    asset.fingerprint.path,
                    asset.fingerprint.size,
                    asset.fingerprint.mtime_ns,
                    asset.origin_path,
                )
                for asset in record.source_assets
            ],
        )

    def _invalidate_analyses(self, photo_id: str) -> None:
        self._connection.execute(
            """
            UPDATE analysis_results
            SET status = ?, result_json = NULL
            WHERE photo_id = ?
            """,
            (AnalysisStatus.PENDING.value, photo_id),
        )
        self._connection.execute(
            "DELETE FROM analysis_failures WHERE photo_id = ?",
            (photo_id,),
        )

    @staticmethod
    def _stored_analysis(
        photo_id: str,
        analyzer: str,
        row: Sequence[object],
    ) -> StoredAnalysis:
        status, result_json, message, exception_type = row
        failure = (
            None
            if message is None
            else AnalysisFailure(
                analyzer=analyzer,
                message=str(message),
                exception_type=str(exception_type),
            )
        )
        return StoredAnalysis(
            photo_id=photo_id,
            analyzer=analyzer,
            status=AnalysisStatus(str(status)),
            result=None if result_json is None else json.loads(str(result_json)),
            failure=failure,
        )
