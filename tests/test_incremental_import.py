"""Incremental import behavior across filesystem snapshots and persistence."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.services.import_service import (
    PhotoCandidate,
    candidate_to_photo_record,
)
from src.xuanpian.store.project_store import (
    PhotoChange,
    ProjectStore,
    StoredAnalysis,
)


def _candidate(
    photo_id: str,
    jpeg: Path,
    raw: Path,
    *,
    display_name: str | None = None,
) -> PhotoCandidate:
    return PhotoCandidate(
        photo_id=photo_id,
        display_name=display_name or jpeg.name,
        jpeg_path=str(jpeg),
        raw_path=str(raw),
    )


def _succeeded(photo_id: str) -> StoredAnalysis:
    return StoredAnalysis(
        photo_id=photo_id,
        analyzer="blur",
        status=AnalysisStatus.SUCCEEDED,
        result={"score": 42},
        failure=None,
    )


def _pending(photo_id: str) -> StoredAnalysis:
    return StoredAnalysis(
        photo_id=photo_id,
        analyzer="blur",
        status=AnalysisStatus.PENDING,
        result=None,
        failure=None,
    )


@pytest.mark.parametrize(
    ("asset_name", "change_kind"),
    [
        ("jpeg", "size"),
        ("jpeg", "mtime"),
        ("raw", "size"),
        ("raw", "mtime"),
    ],
)
def test_changed_asset_invalidates_only_its_logical_photo(
    tmp_path: Path,
    asset_name: str,
    change_kind: str,
) -> None:
    changed_jpeg = tmp_path / "changed.JPG"
    changed_raw = tmp_path / "changed.NEF"
    stable_jpeg = tmp_path / "stable.JPG"
    stable_raw = tmp_path / "stable.NEF"
    for path, content in (
        (changed_jpeg, b"jpeg-a"),
        (changed_raw, b"raw-a"),
        (stable_jpeg, b"jpeg-b"),
        (stable_raw, b"raw-b"),
    ):
        path.write_bytes(content)
    changed_candidate = _candidate(
        "changed", changed_jpeg, changed_raw
    )
    stable_candidate = _candidate("stable", stable_jpeg, stable_raw)

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(changed_candidate),
                candidate_to_photo_record(stable_candidate),
            ]
        )
        for photo_id in ("changed", "stable"):
            store.save_mark(photo_id, "keep")
            store.save_analysis(
                photo_id,
                "blur",
                AnalysisStatus.SUCCEEDED,
                {"score": 42},
                None,
            )

        changed_path = (
            changed_jpeg if asset_name == "jpeg" else changed_raw
        )
        if change_kind == "size":
            changed_path.write_bytes(changed_path.read_bytes() + b"x")
        else:
            original_mtime = changed_path.stat().st_mtime_ns
            os.utime(
                changed_path,
                ns=(original_mtime + 1_000_000, original_mtime + 1_000_000),
            )

        assert store.upsert_photos(
            [
                candidate_to_photo_record(changed_candidate),
                candidate_to_photo_record(stable_candidate),
            ]
        ) == {
            "changed": PhotoChange.CHANGED,
            "stable": PhotoChange.UNCHANGED,
        }
        assert store.get_analysis("changed", "blur") == _pending("changed")
        assert store.get_analysis("stable", "blur") == _succeeded("stable")
        assert [photo.mark for photo in store.list_photos()] == [
            "keep",
            "keep",
        ]


def test_unchanged_assets_reuse_analysis_and_allow_display_name_update(
    tmp_path: Path,
) -> None:
    jpeg = tmp_path / "DSC_0001.JPG"
    raw = tmp_path / "DSC_0001.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    initial = _candidate("one", jpeg, raw)
    renamed = _candidate(
        "one",
        jpeg,
        raw,
        display_name="Favorite frame.JPG",
    )

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos([candidate_to_photo_record(initial)])
        store.save_mark("one", "pick")
        store.save_analysis(
            "one",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 42},
            None,
        )

        assert store.upsert_photos(
            [candidate_to_photo_record(renamed, mark="replace-me")]
        ) == {"one": PhotoChange.UNCHANGED}
        persisted = store.list_photos()[0]
        assert persisted.filename == "Favorite frame.JPG"
        assert persisted.mark == "pick"
        assert store.get_analysis("one", "blur") == _succeeded("one")


def test_missing_one_then_both_assets_preserves_logical_photo(
    tmp_path: Path,
) -> None:
    jpeg = tmp_path / "DSC_0002.JPG"
    raw = tmp_path / "DSC_0002.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw")
    candidate = _candidate("one", jpeg, raw)

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos([candidate_to_photo_record(candidate)])
        store.save_mark("one", "pick")
        store.save_analysis(
            "one",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 42},
            None,
        )

        raw.unlink()
        assert store.upsert_photos(
            [candidate_to_photo_record(candidate)]
        ) == {"one": PhotoChange.CHANGED}
        persisted = store.list_photos()[0]
        assert persisted.id == "one"
        assert persisted.jpeg is not None
        assert persisted.raw is None
        assert persisted.mark == "pick"
        assert store.get_analysis("one", "blur") == _pending("one")

        store.save_analysis(
            "one",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 42},
            None,
        )
        jpeg.unlink()
        assert store.upsert_photos(
            [candidate_to_photo_record(candidate)]
        ) == {"one": PhotoChange.MISSING}
        persisted = store.list_photos()[0]
        assert persisted.id == "one"
        assert persisted.jpeg is None
        assert persisted.raw is None
        assert persisted.mark == "pick"
        assert store.get_analysis("one", "blur") == _pending("one")


def test_partial_batch_never_marks_unmentioned_photos_missing(
    tmp_path: Path,
) -> None:
    first_jpeg = tmp_path / "first.JPG"
    first_raw = tmp_path / "first.NEF"
    second_jpeg = tmp_path / "second.JPG"
    second_raw = tmp_path / "second.NEF"
    for path in (first_jpeg, first_raw, second_jpeg, second_raw):
        path.touch()
    first = _candidate("first", first_jpeg, first_raw)
    second = _candidate("second", second_jpeg, second_raw)

    with ProjectStore.open(tmp_path / "project.sqlite3") as store:
        store.upsert_photos(
            [
                candidate_to_photo_record(first),
                candidate_to_photo_record(second),
            ]
        )
        store.save_analysis(
            "second",
            "blur",
            AnalysisStatus.SUCCEEDED,
            {"score": 42},
            None,
        )

        first_jpeg.unlink()
        first_raw.unlink()
        assert store.upsert_photos(
            [candidate_to_photo_record(first)]
        ) == {"first": PhotoChange.MISSING}
        assert [photo.id for photo in store.list_photos()] == [
            "first",
            "second",
        ]
        assert store.get_analysis("second", "blur") == _succeeded("second")
