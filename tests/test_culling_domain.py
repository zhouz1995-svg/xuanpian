from __future__ import annotations

from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark


def test_photo_marks_have_the_exact_managed_persisted_values() -> None:
    assert [(mark.name, mark.value) for mark in PhotoMark] == [
        ("UNPROCESSED", "未处理"),
        ("KEEP", "保留"),
        ("HOLD", "待定"),
        ("DISCARD", "舍弃"),
    ]


def test_legacy_marks_map_without_guessing() -> None:
    assert normalize_legacy_mark("") is PhotoMark.UNPROCESSED
    assert normalize_legacy_mark("入选") is PhotoMark.KEEP
    assert normalize_legacy_mark("待定") is PhotoMark.HOLD
    assert normalize_legacy_mark("淘汰") is PhotoMark.DISCARD
    assert normalize_legacy_mark("已移出") is None
    assert normalize_legacy_mark("custom-label") is None


def test_current_managed_marks_are_also_normalized() -> None:
    assert normalize_legacy_mark("未处理") is PhotoMark.UNPROCESSED
    assert normalize_legacy_mark("保留") is PhotoMark.KEEP
    assert normalize_legacy_mark("待定") is PhotoMark.HOLD
    assert normalize_legacy_mark("舍弃") is PhotoMark.DISCARD
