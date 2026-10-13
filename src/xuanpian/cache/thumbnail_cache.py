"""Persistent thumbnail cache with a bounded in-memory LRU front cache."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
from threading import Condition, RLock, Thread, local
from typing import Callable
from uuid import uuid4

from PyQt6.QtGui import QImage

from ..domain.photo import FileFingerprint


_ENTRY_RE = re.compile(r"^thumbnail-([0-9a-f]{64})-([0-9a-f]{64})\.png$")
_TEMP_RE = re.compile(
    r"^\.thumbnail-[0-9a-f]{64}-[0-9a-f]{64}\.png\.[0-9a-f]{32}\.tmp$"
)
_TOMBSTONE_NAME = ".thumbnail-invalidated.json"
_TOMBSTONE_TEMP_RE = re.compile(
    r"^\.thumbnail-invalidated\.json\.[0-9a-f]{32}\.tmp$"
)
logger = logging.getLogger(__name__)


def _digest(parts: tuple[object, ...]) -> str:
    """Create a reproducible digest without relying on Python's randomised hash."""
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ThumbnailCacheError(RuntimeError):
    """A cache infrastructure failure, distinct from decoder failures."""


class _RemovalResult(Enum):
    REMOVED = "removed"
    MISSING = "missing"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class _RemovalOutcome:
    result: _RemovalResult
    error: OSError | None = None


@dataclass(slots=True)
class _Flight:
    """Result shared by callers already waiting on one cache miss."""

    token: tuple[int, int]
    done: bool = False
    result: QImage | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class _DiskEntry:
    fingerprint_hash: str
    size: int
    token: tuple[int, int]


@dataclass(frozen=True, slots=True)
class ThumbnailKey:
    """Identity of one rendered thumbnail size for one source file version."""

    fingerprint: FileFingerprint
    width: int
    height: int

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint, FileFingerprint):
            raise TypeError("fingerprint must be a FileFingerprint")
        for dimension in (self.width, self.height):
            if (
                not isinstance(dimension, int)
                or isinstance(dimension, bool)
                or dimension <= 0
            ):
                raise ValueError("thumbnail dimensions must be positive integers")

    @property
    def fingerprint_hash(self) -> str:
        """Stable digest for every thumbnail belonging to this file version."""
        return _digest(
            (self.fingerprint.path, self.fingerprint.size, self.fingerprint.mtime_ns)
        )

    @property
    def stable_hash(self) -> str:
        """Stable digest for this exact fingerprint and requested dimensions."""
        return _digest(
            (
                self.fingerprint.path,
                self.fingerprint.size,
                self.fingerprint.mtime_ns,
                self.width,
                self.height,
            )
        )


class ThumbnailCache:
    """Store PNG thumbnails on disk and keep recent copies in a byte-bounded LRU."""

    def __init__(
        self,
        root: Path,
        memory_limit_bytes: int,
        *,
        disk_limit_bytes: int = 512 * 1024 * 1024,
        disk_limit_entries: int = 4_000,
    ) -> None:
        for name, value in (
            ("memory_limit_bytes", memory_limit_bytes),
            ("disk_limit_bytes", disk_limit_bytes),
            ("disk_limit_entries", disk_limit_entries),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.memory_limit_bytes = memory_limit_bytes
        self.disk_limit_bytes = disk_limit_bytes
        self.disk_limit_entries = disk_limit_entries
        self._memory: OrderedDict[ThumbnailKey, tuple[QImage, int]] = OrderedDict()
        self._memory_bytes = 0
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._flights: dict[ThumbnailKey, _Flight] = {}
        self._clear_generation = 0
        self._fingerprint_generations: dict[FileFingerprint, int] = {}
        self._invalidating: set[FileFingerprint] = set()
        self._clearing = False
        self._disk_mutation_lock = RLock()
        self._put_context = local()
        self._disk_entries: OrderedDict[Path, _DiskEntry] = OrderedDict()
        self._disk_bytes = 0
        self._cleanup_queue: Queue[
            dict[str, set[tuple[int, int]]] | None
        ] = Queue()
        self._cleanup_thread: Thread | None = None
        self._state = "open"
        self._close_error: ThumbnailCacheError | None = None
        self._cleanup_errors: list[BaseException] = []
        self._invalidated_disk_tokens: dict[
            str, set[tuple[int, int]]
        ] = {}
        self._owned_entry_names: set[str] = set()
        self._disk_index_state = "uninitialized"
        self._disk_index_error: OSError | None = None
        (
            self._tombstone_paths,
            self._legacy_tombstone_hashes,
            self._corrupt_tombstones,
        ) = self._load_tombstones()
        self._index_disk_once()
        if self._disk_index_state == "complete":
            self._finalize_disk_index_locked()
            self.prune()

    def path_for(self, key: ThumbnailKey) -> Path:
        """Return the dedicated cache path for *key* without touching the filesystem."""
        return self.root / f"thumbnail-{key.fingerprint_hash}-{key.stable_hash}.png"

    def get(self, key: ThumbnailKey) -> QImage | None:
        """Return an independent cached image, or ``None`` for a cache miss."""
        with self._disk_mutation_lock:
            self._ensure_disk_index_complete_locked()
        with self._condition:
            self._ensure_open_locked()
            self._wait_until_stable_locked(key.fingerprint)
            memory_entry = self._memory.get(key)
            if memory_entry is not None:
                image, _ = memory_entry
                self._memory.move_to_end(key)
                return image.copy()
            token = self._token_locked(key.fingerprint)

        path = self.path_for(key)
        with self._disk_mutation_lock:
            if path.name in self._tombstone_paths:
                return None
            disk_entry = self._disk_entries.get(path)
            if disk_entry is None or disk_entry.token != token:
                return None
            try:
                before = self._file_identity(path)
            except FileNotFoundError:
                self._discard_disk_entry_locked(path)
                self._owned_entry_names.discard(path.name)
                return None
            except OSError:
                return None
        image = self._read_disk(path)

        with self._disk_mutation_lock:
            try:
                unchanged_file = self._file_identity(path) == before
            except FileNotFoundError:
                self._discard_disk_entry_locked(path)
                self._owned_entry_names.discard(path.name)
                unchanged_file = False
            except OSError:
                unchanged_file = False
            with self._condition:
                current = self._token_locked(key.fingerprint) == token
                stable = (
                    self._state == "open"
                    and not self._clearing
                    and key.fingerprint not in self._invalidating
                )
                if not current or not stable or not unchanged_file:
                    return None
                if image.isNull():
                    outcome = self._remove_cache_file(path)
                    if outcome.result is not _RemovalResult.FAILED:
                        self._discard_disk_entry_locked(path)
                        self._owned_entry_names.discard(path.name)
                    return None
                self._store_memory(key, image)
                return image.copy()

    @staticmethod
    def _read_disk(path: Path) -> QImage:
        try:
            return QImage(str(path))
        except Exception:
            return QImage()

    @staticmethod
    def _file_identity(path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return (stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def put(self, key: ThumbnailKey, image: QImage) -> None:
        """Persist a copy of *image* and make it the most recent memory entry."""
        if not isinstance(image, QImage) or image.isNull():
            raise ValueError("cannot cache a null QImage")

        with self._disk_mutation_lock:
            self._ensure_disk_index_complete_locked()
        with self._condition:
            self._ensure_open_locked()
            self._wait_until_stable_locked(key.fingerprint)
            expected_token = getattr(
                self._put_context,
                "expected_token",
                self._token_locked(key.fingerprint),
            )
        self._put_locked(key, image, expected_token)

    def _put_locked(
        self,
        key: ThumbnailKey,
        image: QImage,
        expected_token: tuple[int, int],
    ) -> None:
        image_copy = image.copy()
        destination = self.path_for(key)
        temporary = self.root / f".{destination.name}.{uuid4().hex}.tmp"
        try:
            self._write_temporary(image_copy, temporary, destination)
            encoded_size = temporary.stat().st_size
            with self._disk_mutation_lock:
                with self._condition:
                    if (
                        self._state != "open"
                        or self._token_locked(key.fingerprint) != expected_token
                        or self._clearing
                        or key.fingerprint in self._invalidating
                    ):
                        return
                    os.replace(temporary, destination)
                    self._record_disk_entry_locked(
                        destination,
                        _DiskEntry(
                            fingerprint_hash=key.fingerprint_hash,
                            size=encoded_size,
                            token=expected_token,
                        ),
                    )
                    self._clear_tombstone_for_new_entry_locked(
                        destination.name
                    )
                    self._store_memory(key, image_copy)
                self._prune_locked()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _write_temporary(
        image: QImage,
        temporary: Path,
        destination: Path,
    ) -> None:
        if not image.save(str(temporary), "PNG"):
            raise OSError(f"failed to save thumbnail cache entry: {destination}")

    def get_or_create(
        self,
        key: ThumbnailKey,
        factory: Callable[[], QImage | None],
    ) -> QImage | None:
        """Return one cached image, generating each missing key once."""
        with self._condition:
            self._ensure_open_locked()
            self._wait_until_stable_locked(key.fingerprint)
            memory_entry = self._memory.get(key)
            if memory_entry is not None:
                image, _ = memory_entry
                self._memory.move_to_end(key)
                return image.copy()
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight(token=self._token_locked(key.fingerprint))
                self._flights[key] = flight
            else:
                self._condition.wait_for(lambda: flight.done)
                if flight.error is not None:
                    raise flight.error
                return self._copy_result(flight.result)

        try:
            cached = self.get(key)
        except Exception as error:
            cache_error = ThumbnailCacheError(
                f"failed to read thumbnail cache entry: {key.stable_hash}"
            )
            cache_error.__cause__ = error
            self._finish_flight(key, flight, error=cache_error)
            raise cache_error
        if cached is not None:
            self._finish_flight(key, flight, result=cached)
            return cached

        try:
            image = factory()
        except BaseException as error:
            self._finish_flight(key, flight, error=error)
            raise

        if isinstance(image, QImage) and not image.isNull():
            try:
                self._put_context.expected_token = flight.token
                self.put(key, image)
            except Exception as error:
                logger.warning(
                    "Failed to persist thumbnail cache entry %s; "
                    "using generated image: %s",
                    key.stable_hash,
                    error,
                )
            finally:
                try:
                    del self._put_context.expected_token
                except AttributeError:
                    pass

        shared_result = self._copy_result(image)
        self._finish_flight(key, flight, result=shared_result)
        return self._copy_result(shared_result)

    def _finish_flight(
        self,
        key: ThumbnailKey,
        flight: _Flight,
        *,
        result: QImage | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._condition:
            flight.result = self._copy_result(result)
            flight.error = error
            flight.done = True
            if self._flights.get(key) is flight:
                del self._flights[key]
            self._condition.notify_all()

    def invalidate(self, fingerprint: FileFingerprint) -> None:
        """Remove every cached size for this exact source-file fingerprint."""
        self.invalidate_many((fingerprint,))

    def invalidate_many(
        self,
        fingerprints: tuple[FileFingerprint, ...]
        | list[FileFingerprint]
        | set[FileFingerprint],
    ) -> None:
        """Expire fingerprints immediately and clean their disk files in background."""
        unique = tuple(dict.fromkeys(fingerprints))
        if any(not isinstance(item, FileFingerprint) for item in unique):
            raise TypeError("fingerprints must contain only FileFingerprint values")
        if not unique:
            return

        invalidated_tokens: dict[str, set[tuple[int, int]]] = {}
        with self._disk_mutation_lock:
            self._ensure_disk_index_complete_locked()
            with self._condition:
                self._ensure_open_locked()
                self._ensure_cleanup_thread_locked()
                for fingerprint in unique:
                    old_token = self._token_locked(fingerprint)
                    fingerprint_hash = _digest(
                        (
                            fingerprint.path,
                            fingerprint.size,
                            fingerprint.mtime_ns,
                        )
                    )
                    invalidated_tokens.setdefault(
                        fingerprint_hash,
                        set(),
                    ).add(old_token)

            prospective_tombstones = set(self._tombstone_paths)
            for path, entry in self._disk_entries.items():
                tokens = invalidated_tokens.get(entry.fingerprint_hash)
                if tokens is not None and entry.token in tokens:
                    prospective_tombstones.add(path.name)
            for entry_name in self._owned_entry_names:
                match = _ENTRY_RE.fullmatch(entry_name)
                if (
                    match is not None
                    and match.group(1) in invalidated_tokens
                ):
                    prospective_tombstones.add(entry_name)
            try:
                self._persist_tombstones_locked(prospective_tombstones)
            except OSError as error:
                raise ThumbnailCacheError(
                    f"failed to persist thumbnail invalidation: {error}"
                ) from error

            with self._condition:
                self._cleanup_errors.clear()
                self._tombstone_paths = prospective_tombstones
                for fingerprint in unique:
                    fingerprint_hash = _digest(
                        (
                            fingerprint.path,
                            fingerprint.size,
                            fingerprint.mtime_ns,
                        )
                    )
                    self._invalidated_disk_tokens.setdefault(
                        fingerprint_hash,
                        set(),
                    ).update(invalidated_tokens[fingerprint_hash])
                    self._fingerprint_generations[fingerprint] = (
                        self._fingerprint_generations.get(fingerprint, 0) + 1
                    )
                invalidated = set(unique)
                for key in tuple(self._flights):
                    if key.fingerprint in invalidated:
                        del self._flights[key]
                for key in tuple(self._memory):
                    if key.fingerprint in invalidated:
                        self._discard_memory(key)
            self._cleanup_queue.put(invalidated_tokens)

    def _ensure_cleanup_thread_locked(self) -> None:
        if (
            self._cleanup_thread is not None
            and self._cleanup_thread.is_alive()
            and self._cleanup_thread.ident is not None
        ):
            return
        self._cleanup_thread = None
        candidate = Thread(
            target=self._cleanup_loop,
            name="thumbnail-cache-cleanup",
            daemon=True,
        )
        try:
            candidate.start()
        except BaseException as error:
            raise ThumbnailCacheError(
                f"failed to start thumbnail cache cleanup: {error}"
            ) from error
        if not candidate.is_alive() or candidate.ident is None:
            raise ThumbnailCacheError(
                "failed to start thumbnail cache cleanup: worker exited"
            )
        self._cleanup_thread = candidate

    def _cleanup_loop(self) -> None:
        while True:
            first = self._cleanup_queue.get()
            if first is None:
                self._cleanup_queue.task_done()
                return
            batch = first
            consumed = 1
            while True:
                try:
                    queued = self._cleanup_queue.get_nowait()
                except Empty:
                    break
                if queued is None:  # close() enqueues this only after drain()
                    self._cleanup_queue.task_done()
                    return
                consumed += 1
                for fingerprint_hash, tokens in queued.items():
                    batch.setdefault(fingerprint_hash, set()).update(tokens)
            try:
                self._delete_invalidated_entries(batch)
            except BaseException as error:
                logger.exception("Thumbnail cache cleanup worker failed")
                with self._condition:
                    self._cleanup_errors.append(error)
            finally:
                for _ in range(consumed):
                    self._cleanup_queue.task_done()

    def _delete_invalidated_entries(
        self,
        invalidated_tokens: dict[str, set[tuple[int, int]]],
    ) -> None:
        errors: list[OSError] = []
        with self._disk_mutation_lock:
            try:
                self._persist_tombstones_locked(self._tombstone_paths)
            except OSError as error:
                errors.append(error)
            if errors:
                with self._condition:
                    self._cleanup_errors.extend(errors)
                return
            target_hashes = (
                set(invalidated_tokens)
                if invalidated_tokens
                else set(self._invalidated_disk_tokens)
            )
            removed_entry_names: set[str] = set()
            for entry_name in tuple(self._tombstone_paths):
                match = _ENTRY_RE.fullmatch(entry_name)
                if (
                    match is None
                    or match.group(1) not in target_hashes
                ):
                    continue
                path = self.root / entry_name
                entry = self._disk_entries.get(path)
                if entry is not None:
                    tokens = self._invalidated_disk_tokens.get(
                        entry.fingerprint_hash
                    )
                    if tokens is None or entry.token not in tokens:
                        continue
                outcome = self._remove_cache_file(path)
                if outcome.result is _RemovalResult.FAILED:
                    assert outcome.error is not None
                    errors.append(outcome.error)
                    continue
                removed_entry_names.add(entry_name)

            if removed_entry_names:
                try:
                    self._fsync_directory_locked()
                except OSError as error:
                    errors.append(error)
                    with self._condition:
                        self._cleanup_errors.extend(errors)
                    return

            if target_hashes:
                remaining_tombstones = (
                    self._tombstone_paths - removed_entry_names
                )
                try:
                    self._persist_tombstones_locked(remaining_tombstones)
                except OSError as error:
                    errors.append(error)
                else:
                    for entry_name in removed_entry_names:
                        path = self.root / entry_name
                        self._discard_disk_entry_locked(path)
                        self._owned_entry_names.discard(entry_name)
                    self._tombstone_paths = remaining_tombstones
                    completed_hashes = {
                        fingerprint_hash
                        for fingerprint_hash, tokens
                        in self._invalidated_disk_tokens.items()
                        if not any(
                            (
                                match := _ENTRY_RE.fullmatch(entry_name)
                            )
                            is not None
                            and match.group(1) == fingerprint_hash
                            for entry_name in self._tombstone_paths
                        )
                    }
                    for fingerprint_hash in completed_hashes:
                        self._invalidated_disk_tokens.pop(
                            fingerprint_hash,
                            None,
                        )
        if errors:
            with self._condition:
                self._cleanup_errors.extend(errors)

    def drain(self) -> None:
        """Wait until all queued disk invalidations have completed."""
        self._cleanup_queue.join()
        with self._condition:
            if self._cleanup_errors:
                first = self._cleanup_errors[0]
                error = ThumbnailCacheError(
                    f"thumbnail cache cleanup failed: {first}"
                )
                error.__cause__ = first
                raise error

    def close(self) -> None:
        """Drain and stop the lazy cleanup worker; safe to call repeatedly."""
        with self._condition:
            if self._state == "closed":
                if self._close_error is not None:
                    raise self._close_error
                return
            if self._state == "closing":
                self._condition.wait_for(lambda: self._state == "closed")
                if self._close_error is not None:
                    raise self._close_error
                return
            self._state = "closing"
            worker = self._cleanup_thread
        close_error: ThumbnailCacheError | None = None
        if worker is not None:
            for attempt in range(2):
                try:
                    self.drain()
                except ThumbnailCacheError as error:
                    close_error = error
                    if attempt == 0:
                        with self._condition:
                            self._cleanup_errors.clear()
                            self._cleanup_queue.put({})
                        continue
                else:
                    close_error = None
                break
            self._cleanup_queue.put(None)
            worker.join()
        with self._condition:
            self._close_error = close_error
            self._state = "closed"
            self._condition.notify_all()
        if close_error is not None:
            raise close_error

    def clear(self) -> None:
        """Delete only cache-owned entries, leaving the cache root and other files alone."""
        errors: list[OSError] = []
        with self._disk_mutation_lock:
            self._ensure_disk_index_complete_locked()
            with self._condition:
                self._ensure_open_locked()
                self._clearing = True
                self._clear_generation += 1
                self._fingerprint_generations.clear()
                self._flights.clear()
                self._memory.clear()
                self._memory_bytes = 0
            for entry_name in tuple(self._owned_entry_names):
                path = self.root / entry_name
                entry = self._disk_entries.get(path)
                outcome = self._remove_cache_file(path)
                if outcome.result is _RemovalResult.FAILED:
                    assert outcome.error is not None
                    errors.append(outcome.error)
                    self._tombstone_paths.add(entry_name)
                    if entry is not None:
                        self._invalidated_disk_tokens.setdefault(
                            entry.fingerprint_hash,
                            set(),
                        ).add(entry.token)
                    continue
                self._discard_disk_entry_locked(path)
                self._owned_entry_names.discard(entry_name)
                self._tombstone_paths.discard(entry_name)
            try:
                self._persist_tombstones_locked(self._tombstone_paths)
            except OSError as error:
                errors.append(error)
            with self._condition:
                self._clearing = False
                self._condition.notify_all()
        if errors:
            raise ThumbnailCacheError(
                f"thumbnail cache clear failed: {errors[0]}"
            ) from errors[0]

    def prune(self) -> None:
        """Remove oldest owned PNG entries until both disk bounds are satisfied."""
        with self._disk_mutation_lock:
            self._ensure_disk_index_complete_locked()
            with self._condition:
                self._ensure_open_locked()
            self._prune_locked()

    @property
    def _tombstone_path(self) -> Path:
        return self.root / _TOMBSTONE_NAME

    def _load_tombstones(
        self,
    ) -> tuple[set[str], set[str], bool]:
        path = self._tombstone_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set(), set(), False
        except OSError as error:
            raise ThumbnailCacheError(
                f"failed to read thumbnail cache tombstones: {path}"
            ) from error
        except (ValueError, TypeError):
            logger.warning(
                "Corrupt thumbnail tombstone manifest; invalidating "
                "all indexed entries: %s",
                path,
            )
            return set(), set(), True
        if not isinstance(payload, dict):
            return set(), set(), True
        version = payload.get("version", 1)
        if version == 1:
            values = payload.get("fingerprints")
            if isinstance(values, list) and all(
                isinstance(value, str)
                and re.fullmatch(r"[0-9a-f]{64}", value) is not None
                for value in values
            ):
                return set(), set(values), False
        elif version == 2:
            values = payload.get("entries")
            if isinstance(values, list) and all(
                isinstance(value, str)
                and _ENTRY_RE.fullmatch(value) is not None
                for value in values
            ):
                return set(values), set(), False
        logger.warning(
            "Invalid thumbnail tombstone manifest; invalidating "
            "all indexed entries: %s",
            path,
        )
        return set(), set(), True

    def _persist_tombstones_locked(self, entries: set[str]) -> None:
        path = self._tombstone_path
        if not entries:
            outcome = self._remove_cache_file(path)
            if outcome.result is _RemovalResult.FAILED:
                assert outcome.error is not None
                raise outcome.error
            self._fsync_directory_locked()
            return
        temporary = self.root / f"{_TOMBSTONE_NAME}.{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "entries": sorted(entries),
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory_locked()
        finally:
            self._remove_cache_file(temporary)

    def _fsync_directory_locked(self) -> None:
        directory_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _clear_tombstone_for_new_entry_locked(
        self,
        entry_name: str,
    ) -> None:
        if entry_name not in self._tombstone_paths:
            return
        remaining = self._tombstone_paths - {entry_name}
        self._persist_tombstones_locked(remaining)
        self._tombstone_paths = remaining

    def _index_disk_once(self) -> None:
        indexed: list[tuple[int, Path, _DiskEntry]] = []
        try:
            children = tuple(self.root.iterdir())
        except OSError as error:
            self._disk_index_state = "failed"
            self._disk_index_error = error
            return
        owned_entry_names: set[str] = set()
        for path in children:
            match = _ENTRY_RE.fullmatch(path.name)
            if match is None:
                if (
                    _TEMP_RE.fullmatch(path.name) is not None
                    or _TOMBSTONE_TEMP_RE.fullmatch(path.name) is not None
                ):
                    self._remove_cache_file(path)
                continue
            owned_entry_names.add(path.name)
            try:
                stat = path.stat()
            except OSError:
                continue
            indexed.append(
                (
                    stat.st_mtime_ns,
                    path,
                    _DiskEntry(
                        fingerprint_hash=match.group(1),
                        size=stat.st_size,
                        token=(self._clear_generation, 0),
                    ),
                )
            )
        self._disk_entries.clear()
        self._disk_bytes = 0
        self._owned_entry_names = owned_entry_names
        for _mtime_ns, path, entry in sorted(
            indexed,
            key=lambda item: (item[0], item[1].name),
        ):
            self._record_disk_entry_locked(path, entry)
        self._disk_index_state = "complete"
        self._disk_index_error = None

    def _ensure_disk_index_complete_locked(self) -> None:
        needs_finalization = bool(
            self._corrupt_tombstones
            or self._legacy_tombstone_hashes
        )
        if self._disk_index_state == "complete" and not needs_finalization:
            return
        if self._disk_index_state != "complete":
            self._index_disk_once()
        if self._disk_index_state != "complete":
            error = ThumbnailCacheError(
                "thumbnail cache disk index is unavailable"
            )
            error.__cause__ = self._disk_index_error
            raise error
        try:
            self._finalize_disk_index_locked()
        except OSError as cause:
            self._disk_index_state = "failed"
            self._disk_index_error = cause
            raise ThumbnailCacheError(
                "thumbnail cache disk index could not be finalized"
            ) from cause

    def _finalize_disk_index_locked(self) -> None:
        if self._corrupt_tombstones:
            self._tombstone_paths.update(self._owned_entry_names)
        if self._legacy_tombstone_hashes:
            self._tombstone_paths.update(
                entry_name
                for entry_name in self._owned_entry_names
                if (
                    match := _ENTRY_RE.fullmatch(entry_name)
                ) is not None
                and match.group(1) in self._legacy_tombstone_hashes
            )
        for path, entry in self._disk_entries.items():
            if path.name in self._tombstone_paths:
                self._invalidated_disk_tokens.setdefault(
                    entry.fingerprint_hash,
                    set(),
                ).add(entry.token)
        if self._corrupt_tombstones or self._legacy_tombstone_hashes:
            self._persist_tombstones_locked(self._tombstone_paths)
        self._corrupt_tombstones = False
        self._legacy_tombstone_hashes = set()

    def _record_disk_entry_locked(
        self,
        path: Path,
        entry: _DiskEntry,
    ) -> None:
        self._discard_disk_entry_locked(path)
        self._disk_entries[path] = entry
        self._disk_bytes += entry.size
        self._owned_entry_names.add(path.name)

    def _discard_disk_entry_locked(self, path: Path) -> None:
        entry = self._disk_entries.pop(path, None)
        if entry is not None:
            self._disk_bytes -= entry.size

    def _prune_locked(self) -> None:
        removed: set[Path] = set()
        for path in tuple(self._disk_entries):
            if (
                len(self._disk_entries) <= self.disk_limit_entries
                and self._disk_bytes <= self.disk_limit_bytes
            ):
                break
            outcome = self._remove_cache_file(path)
            if outcome.result is _RemovalResult.FAILED:
                continue
            removed.add(path)
            self._discard_disk_entry_locked(path)
            self._owned_entry_names.discard(path.name)
            self._tombstone_paths.discard(path.name)
        if removed:
            with self._condition:
                for key in tuple(self._memory):
                    if self.path_for(key) in removed:
                        self._discard_memory(key)

    def _token_locked(
        self,
        fingerprint: FileFingerprint,
    ) -> tuple[int, int]:
        return (
            self._clear_generation,
            self._fingerprint_generations.get(fingerprint, 0),
        )

    def _ensure_open_locked(self) -> None:
        if self._state != "open":
            raise ThumbnailCacheError("thumbnail cache is closed")

    def _wait_until_stable_locked(
        self,
        fingerprint: FileFingerprint,
    ) -> None:
        if self._clearing or fingerprint in self._invalidating:
            self._condition.wait_for(
                lambda: (
                    not self._clearing
                    and fingerprint not in self._invalidating
                )
            )

    def _store_memory(self, key: ThumbnailKey, image: QImage) -> None:
        self._discard_memory(key)
        byte_count = image.sizeInBytes()
        if byte_count > self.memory_limit_bytes:
            return
        self._memory[key] = (image.copy(), byte_count)
        self._memory_bytes += byte_count
        while self._memory_bytes > self.memory_limit_bytes:
            _, (_, oldest_bytes) = self._memory.popitem(last=False)
            self._memory_bytes -= oldest_bytes

    def _discard_memory(self, key: ThumbnailKey) -> None:
        entry = self._memory.pop(key, None)
        if entry is not None:
            self._memory_bytes -= entry[1]

    @staticmethod
    def _copy_result(image: QImage | None) -> QImage | None:
        return image.copy() if isinstance(image, QImage) else None

    @staticmethod
    def _remove_cache_file(path: Path) -> _RemovalOutcome:
        try:
            path.unlink()
        except FileNotFoundError:
            return _RemovalOutcome(_RemovalResult.MISSING)
        except OSError as error:
            logger.warning(
                "Failed to remove thumbnail cache file %s: %s",
                path,
                error,
            )
            return _RemovalOutcome(_RemovalResult.FAILED, error)
        return _RemovalOutcome(_RemovalResult.REMOVED)
