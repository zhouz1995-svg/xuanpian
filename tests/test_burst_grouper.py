"""Burst (similar-photo) grouping tests."""

from __future__ import annotations

import pytest

from src.xuanpian.services.burst_grouper import (
    BurstGroup,
    group_photos,
    parse_capture_time,
)


def test_parse_capture_time_accepts_exif_and_iso_separators() -> None:
    assert parse_capture_time("2026:05:02 16:40:31") is not None
    assert parse_capture_time("2026:05:02T16:40:31") is not None
    assert parse_capture_time("  2026:05:02 16:40:31  ") is not None


def test_parse_capture_time_rejects_garbage() -> None:
    assert parse_capture_time("") is None
    assert parse_capture_time("unknown") is None
    assert parse_capture_time("2026:13:02 16:40:31") is None


def test_consecutive_close_photos_form_one_group() -> None:
    ids = ["a", "b", "c"]
    times = {"a": 100.0, "b": 100.4, "c": 101.0}
    groups = group_photos(ids, times)
    assert groups == {
        "a": BurstGroup("a", ("a", "b", "c")),
        "b": BurstGroup("a", ("a", "b", "c")),
        "c": BurstGroup("a", ("a", "b", "c")),
    }


def test_gap_larger_than_threshold_splits_groups() -> None:
    ids = ["a", "b", "c"]
    times = {"a": 100.0, "b": 100.5, "c": 110.0}
    groups = group_photos(ids, times)
    assert groups["a"].photo_ids == ("a", "b")
    assert "c" not in groups


def test_unknown_time_breaks_the_run() -> None:
    ids = ["a", "b", "c", "d"]
    times = {"a": 100.0, "b": None, "c": 101.0, "d": 101.5}
    groups = group_photos(ids, times)
    assert groups == {
        "c": BurstGroup("c", ("c", "d")),
        "d": BurstGroup("c", ("c", "d")),
    }
    assert "a" not in groups
    assert "b" not in groups


def test_min_members_filters_singletons() -> None:
    ids = ["a", "b"]
    times = {"a": 100.0, "b": 100.5}
    assert group_photos(ids, times, min_members=3) == {}
    assert group_photos(ids, times, min_members=2) != {}


def test_multiple_bursts_across_the_list() -> None:
    ids = ["a", "b", "c", "d", "e"]
    times = {"a": 0.0, "b": 0.3, "c": 10.0, "d": 10.2, "e": 30.0}
    groups = group_photos(ids, times)
    assert groups["a"].group_id == "a"
    assert groups["a"].photo_ids == ("a", "b")
    assert groups["c"].group_id == "c"
    assert groups["c"].photo_ids == ("c", "d")
    assert "e" not in groups


def test_duplicate_times_are_part_of_same_burst() -> None:
    ids = ["a", "b", "c"]
    times = {"a": 100.0, "b": 100.0, "c": 100.0}
    groups = group_photos(ids, times)
    assert groups["a"].photo_ids == ("a", "b", "c")


def test_reverse_capture_time_breaks_a_burst_group() -> None:
    groups = group_photos(["a", "b"], {"a": 20.0, "b": 10.0})

    assert groups == {}


def test_invalid_arguments_raise() -> None:
    with pytest.raises(ValueError):
        group_photos([], {}, gap_seconds=-1)
    with pytest.raises(ValueError):
        group_photos([], {}, min_members=1)
