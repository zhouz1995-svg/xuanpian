from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, Thread
from time import monotonic

import pytest
from PyQt6.QtGui import QColor, QImage

from src.xuanpian.cache.thumbnail_cache import (
    ThumbnailCache,
    ThumbnailCacheError,
    ThumbnailKey,
)
from src.xuanpian.domain.photo import FileFingerprint


def _fingerprint(path: Path) -> FileFingerprint:
    return FileFingerprint(path=str(path.resolve()), size=123, mtime_ns=456)


def _image(color: str, width: int = 12, height: int = 8) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    return image


def _color(image: QImage) -> str:
    return image.pixelColor(0, 0).name()


class TestThumbnailKey:
    def test_stable_hash_includes_fingerprint_and_dimensions(self, tmp_path: Path) -> None:
        fingerprint = _fingerprint(tmp_path / "source.jpg")

        key = ThumbnailKey(fingerprint, 160, 120)

        assert key.stable_hash == ThumbnailKey(fingerprint, 160, 120).stable_hash
        assert key.stable_hash != ThumbnailKey(fingerprint, 161, 120).stable_hash
        assert key.stable_hash != ThumbnailKey(
            FileFingerprint(path=fingerprint.path, size=124, mtime_ns=456), 160, 120
        ).stable_hash

    @pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (-1, 1), (True, 1)])
    def test_requires_positive_integer_dimensions(
        self, tmp_path: Path, width: int, height: int
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), width, height)


class TestThumbnailCache:
    def test_failed_initial_directory_scan_blocks_stale_hit_until_invalidation_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        writer = ThumbnailCache(root, memory_limit_bytes=0)
        writer.put(key, _image("red", 8, 8))
        writer.close()

        original_iterdir = Path.iterdir
        deny_scan = True

        def controlled_iterdir(path: Path):
            if path == root and deny_scan:
                raise PermissionError("cache directory temporarily unreadable")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "iterdir", controlled_iterdir)
        cache = ThumbnailCache(root, memory_limit_bytes=0)

        with pytest.raises(ThumbnailCacheError, match="index"):
            cache.get(key)
        with pytest.raises(ThumbnailCacheError, match="index"):
            cache.invalidate(fingerprint)

        deny_scan = False
        cache.invalidate(fingerprint)
        cache.drain()
        cache.close()

        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        assert reopened.get(key) is None
        reopened.close()

    def test_cleanup_fsyncs_png_unlink_before_removing_manifest(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        png_path = cache.path_for(key)
        entered = Event()
        release = Event()
        events: list[str] = []
        original_delete = cache._delete_invalidated_entries
        original_remove = cache._remove_cache_file
        original_fsync_directory = cache._fsync_directory_locked

        def blocked_delete(tokens) -> None:
            entered.set()
            assert release.wait(2)
            original_delete(tokens)

        def observed_remove(path: Path):
            if path == png_path:
                events.append("unlink_png")
            elif path == cache._tombstone_path:
                events.append("unlink_manifest")
            return original_remove(path)

        def observed_fsync_directory() -> None:
            events.append("fsync_directory")
            original_fsync_directory()

        monkeypatch.setattr(cache, "_delete_invalidated_entries", blocked_delete)
        monkeypatch.setattr(cache, "_remove_cache_file", observed_remove)
        monkeypatch.setattr(
            cache,
            "_fsync_directory_locked",
            observed_fsync_directory,
        )

        cache.invalidate(fingerprint)
        assert entered.wait(1)
        events.clear()
        release.set()
        cache.drain()

        assert events[-4:] == [
            "unlink_png",
            "fsync_directory",
            "unlink_manifest",
            "fsync_directory",
        ]
        cache.close()

    def test_png_directory_fsync_failure_keeps_tombstone_for_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        png_path = cache.path_for(key)
        fail_after_png_unlink = True
        original_fsync_directory = cache._fsync_directory_locked

        def controlled_fsync_directory() -> None:
            if fail_after_png_unlink and not png_path.exists():
                raise OSError("directory fsync unavailable")
            original_fsync_directory()

        monkeypatch.setattr(
            cache,
            "_fsync_directory_locked",
            controlled_fsync_directory,
        )

        cache.invalidate(fingerprint)
        with pytest.raises(
            ThumbnailCacheError,
            match="directory fsync unavailable",
        ):
            cache.drain()

        assert png_path.name in cache._tombstone_paths
        assert cache._tombstone_path.exists()
        reopened = ThumbnailCache(cache.root, memory_limit_bytes=0)
        assert reopened.get(key) is None
        reopened.close()

        fail_after_png_unlink = False
        cache.invalidate(fingerprint)
        cache.drain()
        assert not cache._tombstone_path.exists()
        cache.close()

    def test_manifest_directory_fsync_failure_keeps_in_memory_tombstone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        png_path = cache.path_for(key)
        original_fsync_directory = cache._fsync_directory_locked
        fsync_calls = 0
        fail_manifest_fsync = True

        def controlled_fsync_directory() -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fail_manifest_fsync and fsync_calls == 3:
                raise OSError("manifest directory fsync unavailable")
            original_fsync_directory()

        monkeypatch.setattr(
            cache,
            "_fsync_directory_locked",
            controlled_fsync_directory,
        )

        cache.invalidate(fingerprint)
        with pytest.raises(
            ThumbnailCacheError,
            match="manifest directory fsync unavailable",
        ):
            cache.drain()

        assert not png_path.exists()
        assert png_path.name in cache._tombstone_paths
        fail_manifest_fsync = False
        cache.invalidate(fingerprint)
        cache.drain()
        assert cache._tombstone_paths == set()
        cache.close()

    def test_invalidate_many_persists_batch_before_background_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        fingerprints = tuple(
            _fingerprint(tmp_path / f"source-{index}.jpg")
            for index in range(3)
        )
        keys = tuple(
            ThumbnailKey(fingerprint, 8, 8)
            for fingerprint in fingerprints
        )
        for key in keys:
            cache.put(key, _image("red", 8, 8))

        entered = Event()
        release = Event()
        original_delete = cache._delete_invalidated_entries
        original_persist = cache._persist_tombstones_locked
        persist_calls = 0

        def blocked_delete(tokens) -> None:
            entered.set()
            assert release.wait(2)
            original_delete(tokens)

        def counted_persist(entries) -> None:
            nonlocal persist_calls
            persist_calls += 1
            original_persist(entries)

        monkeypatch.setattr(cache, "_delete_invalidated_entries", blocked_delete)
        monkeypatch.setattr(
            cache,
            "_persist_tombstones_locked",
            counted_persist,
        )

        cache.invalidate_many(fingerprints)

        assert entered.wait(1)
        assert persist_calls == 1
        manifest = json.loads(cache._tombstone_path.read_text(encoding="utf-8"))
        assert manifest == {
            "version": 2,
            "entries": sorted(cache.path_for(key).name for key in keys),
        }
        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        assert all(reopened.get(key) is None for key in keys)
        reopened.close()

        release.set()
        cache.drain()
        cache.close()

    def test_manifest_failure_rolls_back_invalidation_for_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=10_000,
        )
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        old_token = cache._token_locked(fingerprint)
        original_persist = cache._persist_tombstones_locked
        fail = True

        def controlled_persist(entries) -> None:
            if fail:
                raise PermissionError("manifest read-only")
            original_persist(entries)

        monkeypatch.setattr(
            cache,
            "_persist_tombstones_locked",
            controlled_persist,
        )

        with pytest.raises(ThumbnailCacheError, match="manifest read-only"):
            cache.invalidate_many((fingerprint,))

        assert cache._token_locked(fingerprint) == old_token
        assert cache._tombstone_paths == set()
        assert cache._invalidated_disk_tokens == {}
        assert cache._cleanup_queue.unfinished_tasks == 0
        cached = cache.get(key)
        assert cached is not None and _color(cached) == "#ff0000"

        fail = False
        cache.invalidate_many((fingerprint,))
        assert cache._token_locked(fingerprint) != old_token
        cache.drain()
        assert cache.get(key) is None
        cache.close()

    def test_thousand_puts_do_not_rescan_or_restat_the_cache_directory(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(
            root,
            memory_limit_bytes=0,
            disk_limit_bytes=100_000_000,
            disk_limit_entries=2_000,
        )
        original_iterdir = Path.iterdir
        original_stat = Path.stat
        scans = 0
        owned_stats = 0

        def counted_iterdir(path: Path):
            nonlocal scans
            if path == root:
                scans += 1
            return original_iterdir(path)

        def counted_stat(path: Path, *args, **kwargs):
            nonlocal owned_stats
            if (
                path.parent == root
                and path.name.startswith("thumbnail-")
                and path.suffix == ".png"
            ):
                owned_stats += 1
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "iterdir", counted_iterdir)
        monkeypatch.setattr(Path, "stat", counted_stat)
        image = _image("red", 2, 2)
        for index in range(1_000):
            cache.put(
                ThumbnailKey(
                    _fingerprint(tmp_path / f"source-{index}.jpg"),
                    2,
                    2,
                ),
                image,
            )

        assert scans == 0
        assert owned_stats <= 1_000

    def test_invalidate_many_returns_before_one_background_index_pass(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        fingerprints = tuple(
            _fingerprint(tmp_path / f"source-{index}.jpg")
            for index in range(1_000)
        )
        for fingerprint in fingerprints[:10]:
            cache.put(
                ThumbnailKey(fingerprint, 8, 8),
                _image("red", 8, 8),
            )
        entered = Event()
        release = Event()
        passes = 0
        original_delete = getattr(cache, "_delete_invalidated_entries", None)

        def observed_delete(tokens) -> None:
            nonlocal passes
            passes += 1
            entered.set()
            assert release.wait(2)
            assert original_delete is not None
            original_delete(tokens)

        monkeypatch.setattr(
            cache,
            "_delete_invalidated_entries",
            observed_delete,
            raising=False,
        )

        started = monotonic()
        cache.invalidate_many(fingerprints)
        elapsed = monotonic() - started

        assert elapsed < 0.2
        assert entered.wait(1)
        release.set()
        cache.drain()
        assert passes == 1
        assert not list(root.glob("*.png"))
        cache.close()

    def test_drain_and_close_finish_cleanup_worker_without_leaking_thread(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        cache.put(
            ThumbnailKey(fingerprint, 8, 8),
            _image("red", 8, 8),
        )

        cache.invalidate_many((fingerprint,))
        cache.drain()

        assert not list(cache.root.glob("*.png"))
        worker = cache._cleanup_thread
        assert worker is not None and worker.is_alive()
        cache.close()
        cache.close()
        assert not worker.is_alive()

    def test_async_invalidation_does_not_delete_a_new_generation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        entered = Event()
        release = Event()
        original_delete = getattr(cache, "_delete_invalidated_entries", None)

        def delayed_delete(tokens) -> None:
            entered.set()
            assert release.wait(2)
            assert original_delete is not None
            original_delete(tokens)

        monkeypatch.setattr(
            cache,
            "_delete_invalidated_entries",
            delayed_delete,
            raising=False,
        )
        cache.invalidate_many((fingerprint,))
        assert entered.wait(1)
        cache.put(key, _image("green", 8, 8))
        release.set()
        cache.drain()

        cached = cache.get(key)
        assert cached is not None and _color(cached) == "#008000"
        cache.close()

    def test_failed_unlink_keeps_index_bytes_and_tombstone_until_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        path = cache.path_for(key)
        encoded_size = path.stat().st_size
        original_unlink = Path.unlink
        fail_unlink = True

        def controlled_unlink(target: Path, *args, **kwargs):
            if target == path and fail_unlink:
                raise PermissionError("cache file is read-only")
            return original_unlink(target, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", controlled_unlink)
        cache.invalidate(fingerprint)

        with pytest.raises(ThumbnailCacheError, match="read-only"):
            cache.drain()

        assert path.exists()
        assert path in cache._disk_entries
        assert cache._disk_bytes == encoded_size
        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        assert reopened.get(key) is None
        reopened.close()

        fail_unlink = False
        cache.invalidate(fingerprint)
        cache.drain()

        assert not path.exists()
        assert path not in cache._disk_entries
        assert cache._disk_bytes == 0
        cache.close()

    @pytest.mark.parametrize("denied_identity_call", [1, 2])
    def test_get_permission_error_preserves_tracking_for_invalidation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        denied_identity_call: int,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        path = cache.path_for(key)
        original_stat = Path.stat
        identity_calls = 0
        deny_stat = True

        def controlled_stat(target: Path, *args, **kwargs):
            nonlocal identity_calls
            if target == path and deny_stat:
                identity_calls += 1
                if identity_calls == denied_identity_call:
                    raise PermissionError("cache metadata is unreadable")
            return original_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", controlled_stat)

        assert cache.get(key) is None
        assert path in cache._disk_entries
        assert path.name in cache._owned_entry_names

        deny_stat = False
        cache.invalidate(fingerprint)
        cache.drain()
        assert not path.exists()
        cache.close()

        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        assert reopened.get(key) is None
        reopened.close()

    def test_concurrent_close_sends_one_sentinel_and_all_callers_wait(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        cache.put(
            ThumbnailKey(fingerprint, 8, 8),
            _image("red", 8, 8),
        )
        cleanup_entered = Event()
        release_cleanup = Event()
        original_delete = cache._delete_invalidated_entries
        original_put = cache._cleanup_queue.put
        sentinels = 0
        sentinel_lock = Lock()

        def delayed_delete(tokens) -> None:
            cleanup_entered.set()
            assert release_cleanup.wait(2)
            original_delete(tokens)

        def counted_put(item, *args, **kwargs):
            nonlocal sentinels
            if item is None:
                with sentinel_lock:
                    sentinels += 1
            return original_put(item, *args, **kwargs)

        monkeypatch.setattr(cache, "_delete_invalidated_entries", delayed_delete)
        monkeypatch.setattr(cache._cleanup_queue, "put", counted_put)
        cache.invalidate(fingerprint)
        assert cleanup_entered.wait(1)
        start = Barrier(2)
        finished: list[int] = []

        def close_cache(index: int) -> None:
            start.wait()
            cache.close()
            finished.append(index)

        callers = [
            Thread(target=close_cache, args=(index,), daemon=True)
            for index in range(2)
        ]
        for caller in callers:
            caller.start()
        assert not finished
        release_cleanup.set()
        for caller in callers:
            caller.join(timeout=2)

        assert sorted(finished) == [0, 1]
        assert sentinels == 1
        assert cache._cleanup_thread is not None
        assert not cache._cleanup_thread.is_alive()
        started = monotonic()
        cache.drain()
        assert monotonic() - started < 0.1
        with pytest.raises(ThumbnailCacheError, match="closed"):
            cache.invalidate(fingerprint)

    def test_tombstones_are_per_path_when_only_one_size_is_recreated(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        first = ThumbnailKey(fingerprint, 8, 8)
        second = ThumbnailKey(fingerprint, 16, 16)
        cache.put(first, _image("red", 8, 8))
        cache.put(second, _image("blue", 16, 16))
        second_path = cache.path_for(second)
        original_unlink = Path.unlink
        fail_second = True

        def controlled_unlink(target: Path, *args, **kwargs):
            if target == second_path and fail_second:
                raise PermissionError("second size cannot be removed")
            return original_unlink(target, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", controlled_unlink)
        cache.invalidate(fingerprint)
        with pytest.raises(ThumbnailCacheError, match="second size"):
            cache.drain()

        cache.put(first, _image("green", 8, 8))

        assert _color(cache.get(first)) == "#008000"
        assert cache.get(second) is None
        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        assert _color(reopened.get(first)) == "#008000"
        assert reopened.get(second) is None

        fail_second = False
        cache.invalidate(fingerprint)
        cache.drain()
        reopened.close()
        cache.close()

    def test_corrupt_tombstone_manifest_recovers_conservatively(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(key, _image("red", 8, 8))
        cache._tombstone_path.write_text("{broken", encoding="utf-8")

        reopened = ThumbnailCache(root, memory_limit_bytes=0)

        assert reopened.get(key) is None
        payload = __import__("json").loads(
            reopened._tombstone_path.read_text(encoding="utf-8")
        )
        assert payload["version"] == 2
        assert payload["entries"] == [cache.path_for(key).name]
        reopened.clear()
        reopened.close()
        cache.close()

    def test_version_one_fingerprint_tombstones_migrate_to_exact_entries(
        self,
        tmp_path: Path,
    ) -> None:
        import json

        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        first = ThumbnailKey(fingerprint, 8, 8)
        second = ThumbnailKey(fingerprint, 16, 16)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(first, _image("red", 8, 8))
        cache.put(second, _image("blue", 16, 16))
        cache._tombstone_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "fingerprints": [first.fingerprint_hash],
                }
            ),
            encoding="utf-8",
        )

        reopened = ThumbnailCache(root, memory_limit_bytes=0)

        assert reopened.get(first) is None
        assert reopened.get(second) is None
        payload = json.loads(
            reopened._tombstone_path.read_text(encoding="utf-8")
        )
        assert payload == {
            "version": 2,
            "entries": sorted(
                [
                    cache.path_for(first).name,
                    cache.path_for(second).name,
                ]
            ),
        }
        reopened.clear()
        reopened.close()
        cache.close()

    def test_corrupt_manifest_with_unstatable_owned_entry_stays_tombstoned(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(key, _image("red", 8, 8))
        path = cache.path_for(key)
        cache._tombstone_path.write_text("{broken", encoding="utf-8")
        original_stat = Path.stat
        deny_stat = True

        def controlled_stat(target: Path, *args, **kwargs):
            if target == path and deny_stat:
                raise PermissionError("stat denied")
            return original_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", controlled_stat)
        first_reopen = ThumbnailCache(root, memory_limit_bytes=0)
        assert first_reopen.get(key) is None
        first_reopen.close()

        deny_stat = False
        second_reopen = ThumbnailCache(root, memory_limit_bytes=0)

        assert second_reopen.get(key) is None
        second_reopen.clear()
        second_reopen.close()
        cache.close()

    def test_unstatable_v2_tombstone_survives_unrelated_cleanup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import json

        root = tmp_path / "thumb-cache"
        stale_fingerprint = _fingerprint(tmp_path / "stale.jpg")
        other_fingerprint = _fingerprint(tmp_path / "other.jpg")
        stale = ThumbnailKey(stale_fingerprint, 8, 8)
        other = ThumbnailKey(other_fingerprint, 8, 8)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(stale, _image("red", 8, 8))
        cache.put(other, _image("blue", 8, 8))
        stale_path = cache.path_for(stale)
        cache._tombstone_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "entries": [stale_path.name],
                }
            ),
            encoding="utf-8",
        )
        original_stat = Path.stat
        deny_stat = True

        def controlled_stat(target: Path, *args, **kwargs):
            if target == stale_path and deny_stat:
                raise PermissionError("stale stat denied")
            return original_stat(target, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", controlled_stat)
        reopened = ThumbnailCache(root, memory_limit_bytes=0)
        reopened.invalidate(other_fingerprint)
        reopened.drain()
        reopened.close()

        deny_stat = False
        final_reopen = ThumbnailCache(root, memory_limit_bytes=0)

        assert final_reopen.get(stale) is None
        payload = json.loads(
            final_reopen._tombstone_path.read_text(encoding="utf-8")
        )
        assert payload["entries"] == [stale_path.name]
        final_reopen.clear()
        final_reopen.close()
        cache.close()

    def test_unexpected_cleanup_exception_reaches_drain_and_all_close_callers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        cache.put(
            ThumbnailKey(fingerprint, 8, 8),
            _image("red", 8, 8),
        )

        def explode(_tokens) -> None:
            raise RuntimeError("unexpected cleanup crash")

        monkeypatch.setattr(cache, "_delete_invalidated_entries", explode)
        cache.invalidate(fingerprint)
        with pytest.raises(ThumbnailCacheError, match="unexpected cleanup crash"):
            cache.drain()

        start = Barrier(2)
        errors: list[str] = []

        def close_cache() -> None:
            start.wait()
            try:
                cache.close()
            except ThumbnailCacheError as error:
                errors.append(str(error))

        callers = [Thread(target=close_cache, daemon=True) for _ in range(2)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=2)

        assert errors == [
            "thumbnail cache cleanup failed: unexpected cleanup crash",
            "thumbnail cache cleanup failed: unexpected cleanup crash",
        ]
        assert cache._cleanup_thread is not None
        assert not cache._cleanup_thread.is_alive()

    def test_cleanup_thread_start_failure_rolls_back_and_next_call_retries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.xuanpian.cache import thumbnail_cache

        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 8, 8)
        cache.put(key, _image("red", 8, 8))
        original_start = thumbnail_cache.Thread.start
        attempts = 0

        def fail_once(thread) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("thread start unavailable")
            original_start(thread)

        monkeypatch.setattr(thumbnail_cache.Thread, "start", fail_once)
        with pytest.raises(ThumbnailCacheError, match="thread start unavailable"):
            cache.invalidate(fingerprint)

        assert cache._cleanup_thread is None
        assert cache._cleanup_queue.unfinished_tasks == 0
        cached = cache.get(key)
        assert cached is not None and _color(cached) == "#ff0000"

        cache.invalidate(fingerprint)
        cache.drain()

        assert attempts == 2
        assert cache.get(key) is None
        cache.close()

    def test_different_key_disk_reads_can_overlap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        keys = tuple(
            ThumbnailKey(_fingerprint(tmp_path / f"source-{index}.jpg"), 80, 60)
            for index in range(2)
        )
        for key in keys:
            cache.put(key, _image("red", 80, 60))

        reads_entered = Barrier(2)
        overlapped = Event()

        def read_disk(path: Path) -> QImage:
            try:
                reads_entered.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
            else:
                overlapped.set()
            return QImage(str(path))

        monkeypatch.setattr(cache, "_read_disk", read_disk, raising=False)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(cache.get, keys))

        assert overlapped.is_set()
        assert all(image is not None and not image.isNull() for image in results)

    def test_different_key_png_encoding_can_overlap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        keys = tuple(
            ThumbnailKey(_fingerprint(tmp_path / f"source-{index}.jpg"), 80, 60)
            for index in range(2)
        )
        writes_entered = Barrier(2)
        overlapped = Event()

        def write_temporary(image: QImage, temporary: Path, destination: Path) -> None:
            try:
                writes_entered.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
            else:
                overlapped.set()
            if not image.save(str(temporary), "PNG"):
                raise OSError(f"failed to save thumbnail cache entry: {destination}")

        monkeypatch.setattr(
            cache,
            "_write_temporary",
            write_temporary,
            raising=False,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(cache.put, key, _image("red", 80, 60))
                for key in keys
            ]
            for future in futures:
                future.result(timeout=2)

        assert overlapped.is_set()

    def test_factory_can_invalidate_its_own_fingerprint_without_deadlock(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=1_000_000)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 80, 60)
        done = Event()
        results: list[QImage | None] = []

        def request() -> None:
            results.append(
                cache.get_or_create(
                    key,
                    lambda: (
                        cache.invalidate(fingerprint),
                        _image("red", 80, 60),
                    )[1],
                )
            )
            done.set()

        worker = Thread(target=request, daemon=True)
        worker.start()
        completed_without_deadlock = done.wait(1)

        assert completed_without_deadlock
        assert results and results[0] is not None
        assert cache.get(key) is None

    def test_clear_expires_inflight_write_without_waiting_or_repollution(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=1_000_000)
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 80, 60)
        factory_entered = Event()
        release_factory = Event()
        request_done = Event()
        clear_done = Event()
        results: list[QImage | None] = []

        def factory() -> QImage:
            factory_entered.set()
            assert release_factory.wait(2)
            return _image("red", 80, 60)

        def request() -> None:
            results.append(cache.get_or_create(key, factory))
            request_done.set()

        request_thread = Thread(target=request, daemon=True)
        request_thread.start()
        assert factory_entered.wait(1)
        clear_thread = Thread(
            target=lambda: (cache.clear(), clear_done.set()),
            daemon=True,
        )
        clear_thread.start()
        returned_before_flight = clear_done.wait(0.5)
        release_factory.set()
        assert request_done.wait(1)
        clear_thread.join(timeout=1)

        assert returned_before_flight
        assert results[0] is not None and _color(results[0]) == "#ff0000"
        assert cache.get(key) is None
        assert not list(cache.root.glob("*.png"))

    def test_invalidate_expires_old_flight_but_preserves_registered_waiters(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=0)
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        key = ThumbnailKey(fingerprint, 80, 60)
        old_entered = Event()
        waiter_registered = Event()
        release_old = Event()
        calls: list[str] = []
        original_wait_for = cache._condition.wait_for

        def observed_wait_for(predicate, timeout=None):
            waiter_registered.set()
            return original_wait_for(predicate, timeout)

        monkeypatch.setattr(cache._condition, "wait_for", observed_wait_for)

        def old_factory() -> QImage:
            calls.append("old")
            old_entered.set()
            assert release_old.wait(2)
            return _image("red", 80, 60)

        with ThreadPoolExecutor(max_workers=3) as executor:
            owner = executor.submit(cache.get_or_create, key, old_factory)
            assert old_entered.wait(1)
            waiter = executor.submit(cache.get_or_create, key, old_factory)
            assert waiter_registered.wait(1)
            cache.invalidate(fingerprint)
            fresh = executor.submit(
                cache.get_or_create,
                key,
                lambda: (calls.append("new"), _image("green", 80, 60))[1],
            )
            assert _color(fresh.result(timeout=1)) == "#008000"
            release_old.set()
            assert _color(owner.result(timeout=1)) == "#ff0000"
            assert _color(waiter.result(timeout=1)) == "#ff0000"

        assert calls == ["old", "new"]
        cached = cache.get(key)
        assert cached is not None and _color(cached) == "#008000"

    def test_same_key_concurrent_miss_decodes_once(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=1_000_000,
        )
        key = ThumbnailKey(
            _fingerprint(tmp_path / "source.jpg"),
            80,
            60,
        )
        start = Barrier(4)
        release = Event()
        calls = 0
        calls_lock = Lock()

        def factory() -> QImage:
            nonlocal calls
            with calls_lock:
                calls += 1
            assert release.wait(2)
            return _image("red", 80, 60)

        def request() -> QImage:
            start.wait()
            return cache.get_or_create(key, factory)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(request) for _ in range(4)]
            release.set()
            images = [future.result(timeout=2) for future in futures]

        assert calls == 1
        assert all(not image.isNull() for image in images)
        assert len(cache._memory) == 1
        assert cache._memory_bytes == sum(
            byte_count for _image_value, byte_count in cache._memory.values()
        )
        assert cache._memory_bytes <= cache.memory_limit_bytes

    def test_concurrent_waiters_share_result_when_persistent_write_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=0,
        )
        key = ThumbnailKey(
            _fingerprint(tmp_path / "source.jpg"),
            80,
            60,
        )
        start = Barrier(4)
        factory_entered = Event()
        waiters_ready = Event()
        release = Event()
        calls = 0
        waiter_count = 0
        calls_lock = Lock()
        original_wait_for = cache._condition.wait_for

        def fail_put(cache_key, image) -> None:
            raise OSError("persistent cache unavailable")

        def observed_wait_for(predicate, timeout=None):
            nonlocal waiter_count
            waiter_count += 1
            if waiter_count == 3:
                waiters_ready.set()
            return original_wait_for(predicate, timeout)

        monkeypatch.setattr(cache, "put", fail_put)
        monkeypatch.setattr(cache._condition, "wait_for", observed_wait_for)

        def factory() -> QImage:
            nonlocal calls
            with calls_lock:
                calls += 1
            factory_entered.set()
            assert release.wait(2)
            return _image("red", 80, 60)

        def request() -> QImage:
            start.wait()
            return cache.get_or_create(key, factory)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(request) for _ in range(4)]
            assert factory_entered.wait(2)
            assert waiters_ready.wait(2)
            release.set()
            images = [future.result(timeout=2) for future in futures]

        assert calls == 1
        assert all(_color(image) == "#ff0000" for image in images)
        assert "persistent cache unavailable" in caplog.text

        later = cache.get_or_create(key, factory)

        assert _color(later) == "#ff0000"
        assert calls == 2

    def test_different_key_factories_can_overlap(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=1_000_000,
        )
        keys = (
            ThumbnailKey(
                _fingerprint(tmp_path / "first.jpg"),
                80,
                60,
            ),
            ThumbnailKey(
                _fingerprint(tmp_path / "second.jpg"),
                80,
                60,
            ),
        )
        factories_entered = Barrier(2)
        overlapped = Event()

        def factory(color: str) -> QImage:
            try:
                factories_entered.wait(timeout=1)
            except BrokenBarrierError:
                pass
            else:
                overlapped.set()
            return _image(color, 80, 60)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    cache.get_or_create,
                    key,
                    lambda color=color: factory(color),
                )
                for key, color in zip(
                    keys,
                    ("red", "blue"),
                    strict=True,
                )
            ]
            images = [future.result(timeout=2) for future in futures]

        assert overlapped.is_set()
        assert [_color(image) for image in images] == [
            "#ff0000",
            "#0000ff",
        ]

    def test_failed_factory_notifies_waiters_and_allows_retry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=1_000_000,
        )
        key = ThumbnailKey(
            _fingerprint(tmp_path / "source.jpg"),
            80,
            60,
        )

        first_factory_entered = Event()
        waiter_ready = Event()
        release_failure = Event()
        calls = 0
        calls_lock = Lock()
        original_wait_for = cache._condition.wait_for

        def observed_wait_for(predicate, timeout=None):
            waiter_ready.set()
            return original_wait_for(predicate, timeout)

        monkeypatch.setattr(cache._condition, "wait_for", observed_wait_for)

        def factory() -> QImage:
            nonlocal calls
            with calls_lock:
                calls += 1
                attempt = calls
            if attempt == 1:
                first_factory_entered.set()
                assert release_failure.wait(2)
                raise RuntimeError("decode failed")
            return _image("green", 80, 60)

        with ThreadPoolExecutor(max_workers=2) as executor:
            failed = executor.submit(cache.get_or_create, key, factory)
            assert first_factory_entered.wait(2)
            waiter = executor.submit(cache.get_or_create, key, factory)
            assert waiter_ready.wait(2)
            release_failure.set()
            with pytest.raises(RuntimeError, match="decode failed"):
                failed.result(timeout=2)
            with pytest.raises(RuntimeError, match="decode failed"):
                waiter.result(timeout=2)

        assert calls == 1
        result = cache.get_or_create(key, factory)
        assert calls == 2
        assert _color(result) == "#008000"
        cached = cache.get_or_create(
            key,
            lambda: (_ for _ in ()).throw(
                AssertionError("cached retry must not decode")
            ),
        )
        assert _color(cached) == "#008000"

    def test_concurrent_public_operations_preserve_memory_and_disk_invariants(
        self,
        tmp_path: Path,
    ) -> None:
        cache = ThumbnailCache(
            tmp_path / "thumb-cache",
            memory_limit_bytes=80 * 60 * 4 * 4,
        )
        fingerprints = tuple(
            _fingerprint(tmp_path / f"source-{index}.jpg")
            for index in range(4)
        )
        keys = tuple(
            ThumbnailKey(file_fingerprint, 80, 60)
            for file_fingerprint in fingerprints
        )
        start = Barrier(4)

        def exercise(index: int) -> None:
            start.wait()
            for iteration in range(12):
                cache.put(keys[index], _image("red", 80, 60))
                cache.get(keys[index])
                if iteration % 4 == 0:
                    cache.invalidate(fingerprints[index])
                if index == 0 and iteration % 6 == 0:
                    cache.clear()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(exercise, index)
                for index in range(4)
            ]
            for future in futures:
                future.result(timeout=5)

        assert cache._memory_bytes == sum(
            byte_count for _image_value, byte_count in cache._memory.values()
        )
        assert 0 <= cache._memory_bytes <= cache.memory_limit_bytes
        assert not list(cache.root.glob("*.tmp"))
        assert all(
            not QImage(str(path)).isNull()
            for path in cache.root.glob("*.png")
        )

    def test_gets_png_from_disk_in_a_fresh_cache(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)
        ThumbnailCache(root, memory_limit_bytes=0).put(key, _image("red"))

        result = ThumbnailCache(root, memory_limit_bytes=0).get(key)

        assert result is not None
        assert _color(result) == "#ff0000"

    def test_different_sizes_of_one_source_are_isolated(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        small = ThumbnailKey(fingerprint, 80, 60)
        large = ThumbnailKey(fingerprint, 320, 240)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(small, _image("red"))
        cache.put(large, _image("blue"))
        cache = ThumbnailCache(root, memory_limit_bytes=0)

        small_result = cache.get(small)
        large_result = cache.get(large)

        assert small_result is not None and _color(small_result) == "#ff0000"
        assert large_result is not None and _color(large_result) == "#0000ff"

    def test_corrupt_disk_file_is_deleted_and_treated_as_a_miss(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)
        ThumbnailCache(root, memory_limit_bytes=0).put(key, _image("red"))
        cached_file = next(root.glob("*.png"))
        cached_file.write_bytes(b"not a png")

        assert ThumbnailCache(root, memory_limit_bytes=0).get(key) is None
        assert not cached_file.exists()

    def test_memory_lru_evicts_the_least_recent_image(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        first = ThumbnailKey(fingerprint, 10, 10)
        second = ThumbnailKey(fingerprint, 20, 20)
        third = ThumbnailKey(fingerprint, 30, 30)
        one_image_bytes = _image("red").sizeInBytes()
        cache = ThumbnailCache(root, memory_limit_bytes=one_image_bytes * 2)
        cache.put(first, _image("red"))
        cache.put(second, _image("blue"))
        assert cache.get(first) is not None
        second_file = cache.path_for(second)
        second_file.unlink()
        cache.put(third, _image("green"))

        assert cache.get(second) is None
        assert cache.get(first) is not None

    def test_put_and_get_do_not_expose_the_internal_image(self, tmp_path: Path) -> None:
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)
        image = _image("red")
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=10_000)
        cache.put(key, image)
        image.fill(QColor("blue"))

        result = cache.get(key)
        assert result is not None and _color(result) == "#ff0000"
        result.fill(QColor("green"))
        again = cache.get(key)
        assert again is not None and _color(again) == "#ff0000"

    def test_oversize_image_is_available_on_disk_but_not_in_memory(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)
        cache = ThumbnailCache(root, memory_limit_bytes=0)
        cache.put(key, _image("red"))
        cache.path_for(key).unlink()

        assert cache.get(key) is None

    def test_invalidate_removes_all_sizes_for_a_fingerprint(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        fingerprint = _fingerprint(tmp_path / "source.jpg")
        cache = ThumbnailCache(root, memory_limit_bytes=10_000)
        first = ThumbnailKey(fingerprint, 80, 60)
        second = ThumbnailKey(fingerprint, 160, 120)
        cache.put(first, _image("red"))
        cache.put(second, _image("blue"))

        cache.invalidate(fingerprint)

        assert cache.get(first) is None
        assert cache.get(second) is None
        cache.drain()
        assert not list(root.glob("*.png"))
        cache.close()

    def test_clear_preserves_original_and_unrelated_root_files(self, tmp_path: Path) -> None:
        root = tmp_path / "thumb-cache"
        source = tmp_path / "source.jpg"
        source.write_bytes(b"original image")
        unrelated = root / "keep-me.txt"
        root.mkdir()
        unrelated.write_text("unrelated")
        cache = ThumbnailCache(root, memory_limit_bytes=10_000)
        cache.put(ThumbnailKey(_fingerprint(source), 160, 120), _image("red"))

        cache.clear()

        assert source.read_bytes() == b"original image"
        assert unrelated.read_text() == "unrelated"
        assert not list(root.glob("*.png"))

    def test_failed_atomic_replace_cleans_up_temporary_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.xuanpian.cache import thumbnail_cache

        root = tmp_path / "thumb-cache"
        cache = ThumbnailCache(root, memory_limit_bytes=10_000)
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)

        def fail_replace(source: Path, destination: Path) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(thumbnail_cache.os, "replace", fail_replace)

        with pytest.raises(OSError, match="disk full"):
            cache.put(key, _image("red"))

        assert not list(root.iterdir())

    def test_rejects_null_images(self, tmp_path: Path) -> None:
        cache = ThumbnailCache(tmp_path / "thumb-cache", memory_limit_bytes=10_000)
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 160, 120)

        with pytest.raises(ValueError, match="null"):
            cache.put(key, QImage())

    def test_disk_entry_limit_prunes_oldest_owned_png_and_preserves_unrelated(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "thumb-cache"
        root.mkdir()
        unrelated = root / "keep-me.txt"
        unrelated.write_text("not cache-owned")
        cache = ThumbnailCache(
            root,
            memory_limit_bytes=0,
            disk_limit_bytes=10_000_000,
            disk_limit_entries=2,
        )
        keys = tuple(
            ThumbnailKey(_fingerprint(tmp_path / f"source-{index}.jpg"), 80, 60)
            for index in range(3)
        )
        for index, key in enumerate(keys):
            cache.put(key, _image("red", 80, 60))
            os.utime(cache.path_for(key), ns=(index + 1, index + 1))

        cache.prune()

        assert not cache.path_for(keys[0]).exists()
        assert cache.path_for(keys[1]).exists()
        assert cache.path_for(keys[2]).exists()
        assert unrelated.read_text() == "not cache-owned"

    def test_disk_byte_limit_is_enforced_on_startup_and_after_writes(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "thumb-cache"
        key = ThumbnailKey(_fingerprint(tmp_path / "source.jpg"), 80, 60)
        ThumbnailCache(
            root,
            memory_limit_bytes=0,
            disk_limit_bytes=10_000_000,
            disk_limit_entries=10,
        ).put(key, _image("red", 80, 60))
        unrelated = root / "keep.dat"
        unrelated.write_bytes(b"keep")

        cache = ThumbnailCache(
            root,
            memory_limit_bytes=0,
            disk_limit_bytes=0,
            disk_limit_entries=10,
        )

        assert not cache.path_for(key).exists()
        assert unrelated.read_bytes() == b"keep"
