from __future__ import annotations

import errno
import unicodedata
from pathlib import Path

import pytest

from src.xuanpian.services import export_service
from src.xuanpian.services.export_service import (
    ConflictPolicy,
    ExportItem,
    ExportMode,
    ExportRequest,
    ExportService,
)


def move_request(
    source: Path,
    destination: Path,
    conflict: ConflictPolicy = ConflictPolicy.SKIP,
) -> ExportRequest:
    return ExportRequest(
        destination=destination,
        items=(ExportItem(source=source, target_name=source.name),),
        mode=ExportMode.MOVE,
        conflict=conflict,
    )


def copy_request(
    source: Path,
    destination: Path,
    conflict: ConflictPolicy = ConflictPolicy.SKIP,
) -> ExportRequest:
    return ExportRequest(
        destination=destination,
        items=(ExportItem(source=source, target_name=source.name),),
        mode=ExportMode.COPY,
        conflict=conflict,
    )


def test_move_deletes_source_only_after_verified_promotion(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")

    report = ExportService().export(
        move_request(source, tmp_path / "out"), lambda: False
    )

    assert report.results[0].success is True
    assert not source.exists()
    assert (tmp_path / "out" / "source.raw").read_bytes() == b"photo"


def test_default_conflict_does_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"new photo")
    destination = tmp_path / "out"
    destination.mkdir()
    target = destination / source.name
    target.write_bytes(b"existing photo")

    report = ExportService().export(copy_request(source, destination), lambda: False)

    assert report.results[0].success is False
    assert report.results[0].target == target
    assert report.results[0].error is not None
    assert source.read_bytes() == b"new photo"
    assert target.read_bytes() == b"existing photo"


def test_unique_conflict_uses_available_name(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"new photo")
    destination = tmp_path / "out"
    destination.mkdir()
    existing = destination / source.name
    existing.write_bytes(b"existing photo")

    report = ExportService().export(
        copy_request(source, destination, ConflictPolicy.UNIQUE), lambda: False
    )

    result = report.results[0]
    assert result.success is True
    assert result.target == destination / "source (1).raw"
    assert result.target.read_bytes() == b"new photo"
    assert existing.read_bytes() == b"existing photo"
    assert source.read_bytes() == b"new photo"


def test_overwrite_requires_explicit_policy(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"new photo")
    destination = tmp_path / "out"
    destination.mkdir()
    target = destination / source.name
    target.write_bytes(b"existing photo")

    report = ExportService().export(
        copy_request(source, destination, ConflictPolicy.OVERWRITE), lambda: False
    )

    assert report.results[0].success is True
    assert report.results[0].target == target
    assert target.read_bytes() == b"new photo"
    assert source.read_bytes() == b"new photo"


def test_copy_failure_keeps_source_and_cleans_partial(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    failed_quarantine: Path | None = None

    def fail_after_partial_copy(copy_source: Path, temp: Path) -> None:
        nonlocal failed_quarantine
        failed_quarantine = copy_source
        temp.write_bytes(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(export_service.shutil, "copy2", fail_after_partial_copy)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "copy failed" in (result.error or "")
    assert failed_quarantine is not None
    assert failed_quarantine != source
    assert failed_quarantine.name.endswith(".moving")
    assert source.read_bytes() == b"photo"
    assert not failed_quarantine.exists()
    assert not (destination / source.name).exists()
    assert list(destination.glob("*.partial")) == []


def test_size_mismatch_blocks_promotion_and_move_deletion(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"complete photo")
    destination = tmp_path / "out"

    def copy_truncated(_source: Path, temp: Path) -> None:
        temp.write_bytes(b"short")

    monkeypatch.setattr(export_service.shutil, "copy2", copy_truncated)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "大小不一致" in (result.error or "")
    assert source.read_bytes() == b"complete photo"
    assert not (destination / source.name).exists()
    assert list(destination.glob("*.partial")) == []


def test_replace_failure_keeps_source_and_cleans_partial(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    destination.mkdir()
    target = destination / source.name
    target.write_bytes(b"existing")

    def fail_replace(_temp: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(export_service.os, "replace", fail_replace)

    report = ExportService().export(
        move_request(source, destination, ConflictPolicy.OVERWRITE), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "replace failed" in (result.error or "")
    assert source.read_bytes() == b"photo"
    assert target.read_bytes() == b"existing"
    assert list(destination.glob("*.partial")) == []


def test_move_quarantine_unlink_failure_keeps_target_and_quarantine(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_unlink = Path.unlink
    failed_quarantine: Path | None = None

    def fail_quarantine_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed_quarantine
        if path.name.endswith(".moving"):
            failed_quarantine = path
            raise OSError("unlink failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_unlink)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "unlink failed" in (result.error or "")
    assert failed_quarantine is not None
    assert str(failed_quarantine) in (result.error or "")
    assert result.recovery_path == failed_quarantine
    assert not source.exists()
    assert (destination / source.name).read_bytes() == b"photo"
    assert failed_quarantine.read_bytes() == b"photo"
    assert list(destination.glob("*.partial")) == []


def test_move_quarantine_runtime_error_reports_recovery_path(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_unlink = Path.unlink
    failed_quarantine: Path | None = None

    def fail_quarantine_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed_quarantine
        if path.name.endswith(".moving"):
            failed_quarantine = path
            raise RuntimeError("runtime cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_quarantine_unlink)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "runtime cleanup failure" in (result.error or "")
    assert failed_quarantine is not None
    assert result.recovery_path == failed_quarantine
    assert failed_quarantine.read_bytes() == b"photo"
    assert (destination / source.name).read_bytes() == b"photo"


def test_cancel_stops_before_creating_later_targets(tmp_path: Path) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(source=first, target_name=first.name),
            ExportItem(source=second, target_name=second.name),
        ),
        mode=ExportMode.COPY,
    )
    def cancel_before_second() -> bool:
        return (destination / first.name).exists()

    report = ExportService().export(request, cancel_before_second)

    assert report.canceled is True
    assert len(report.results) == 1
    assert report.results[0].source == first
    assert (destination / first.name).read_bytes() == b"first"
    assert not (destination / second.name).exists()
    assert second.read_bytes() == b"second"


def test_immediate_cancel_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"

    report = ExportService().export(
        copy_request(source, destination), lambda: True
    )

    assert report.canceled is True
    assert report.results == ()
    assert not destination.exists()
    assert source.read_bytes() == b"photo"


def test_target_name_cannot_escape_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(ExportItem(source=source, target_name="../escaped.raw"),),
        mode=ExportMode.COPY,
    )

    report = ExportService().export(request, lambda: False)

    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert source.read_bytes() == b"photo"
    assert not (tmp_path / "escaped.raw").exists()
    assert list(destination.glob("*.partial")) == []


def test_move_rejects_source_as_overwrite_target(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    request = ExportRequest(
        destination=tmp_path,
        items=(ExportItem(source=source, target_name=source.name),),
        mode=ExportMode.MOVE,
        conflict=ConflictPolicy.OVERWRITE,
    )

    report = ExportService().export(request, lambda: False)

    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert source.read_bytes() == b"photo"
    assert list(tmp_path.glob("*.partial")) == []


def test_default_conflict_does_not_overwrite_target_created_during_copy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"new photo")
    destination = tmp_path / "out"
    target = destination / source.name
    original_copy2 = export_service.shutil.copy2

    def copy_then_create_conflict(copy_source: Path, temp: Path) -> None:
        original_copy2(copy_source, temp)
        target.write_bytes(b"concurrent photo")

    monkeypatch.setattr(
        export_service.shutil, "copy2", copy_then_create_conflict
    )

    report = ExportService().export(
        copy_request(source, destination), lambda: False
    )

    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert source.read_bytes() == b"new photo"
    assert target.read_bytes() == b"concurrent photo"
    assert list(destination.glob("*.partial")) == []


def test_unique_treats_broken_symlink_as_occupied(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    destination.mkdir()
    occupied = destination / source.name
    occupied.symlink_to(tmp_path / "missing.raw")

    report = ExportService().export(
        copy_request(source, destination, ConflictPolicy.UNIQUE), lambda: False
    )

    result = report.results[0]
    assert result.success is True
    assert result.target == destination / "source (1).raw"
    assert result.target.read_bytes() == b"photo"
    assert occupied.is_symlink()


def test_unknown_conflict_policy_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(ExportItem(source=source, target_name=source.name),),
        mode=ExportMode.COPY,
        conflict="skip",  # type: ignore[arg-type]
    )

    report = ExportService().export(request, lambda: False)

    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert source.read_bytes() == b"photo"
    assert not destination.exists()
    assert not (destination / source.name).exists()


def test_unknown_export_mode_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(ExportItem(source=source, target_name=source.name),),
        mode="copy",  # type: ignore[arg-type]
    )

    report = ExportService().export(request, lambda: False)

    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert source.read_bytes() == b"photo"
    assert not destination.exists()
    assert not (destination / source.name).exists()


def test_partial_cleanup_failure_preserves_primary_error(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_unlink = Path.unlink

    def fail_after_partial_copy(_source: Path, temp: Path) -> None:
        temp.write_bytes(b"partial")
        raise OSError("copy failed")

    def fail_partial_unlink(path: Path, *args, **kwargs) -> None:
        if path.name.endswith(".partial"):
            raise OSError("cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export_service.shutil, "copy2", fail_after_partial_copy)
    monkeypatch.setattr(Path, "unlink", fail_partial_unlink)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "copy failed" in (result.error or "")
    assert "cleanup failed" in (result.error or "")
    assert result.recovery_path is not None
    assert result.recovery_path.name.endswith(".partial")
    assert result.recovery_path.exists()
    assert source.read_bytes() == b"photo"
    assert not (destination / source.name).exists()
    assert len(list(destination.glob("*.partial"))) == 1


def test_move_quarantine_never_deletes_new_same_size_source(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_copy2 = export_service.shutil.copy2
    copied_source: Path | None = None

    def copy_then_create_new_source(copy_source: Path, temp: Path) -> None:
        nonlocal copied_source
        copied_source = copy_source
        original_copy2(copy_source, temp)
        source.write_bytes(b"other")

    monkeypatch.setattr(
        export_service.shutil, "copy2", copy_then_create_new_source
    )

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is True
    assert copied_source is not None
    assert copied_source != source
    assert copied_source.parent == source.parent
    assert copied_source.name.endswith(".moving")
    assert source.read_bytes() == b"other"
    assert (destination / source.name).read_bytes() == b"photo"
    assert not copied_source.exists()
    assert list(tmp_path.glob("*.moving")) == []
    assert list(destination.glob("*.partial")) == []


def test_destination_creation_failure_is_reported_for_each_item(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    destination = tmp_path / "not-a-directory"
    destination.write_bytes(b"occupied")
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(first, first.name),
            ExportItem(second, second.name),
        ),
        mode=ExportMode.COPY,
    )

    report = ExportService().export(request, lambda: False)

    assert len(report.results) == 2
    assert all(result.success is False for result in report.results)
    assert all(result.error is not None for result in report.results)
    assert [result.source for result in report.results] == [first, second]
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert destination.read_bytes() == b"occupied"


def test_unique_retries_when_resolved_name_is_claimed_during_copy(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"new photo")
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / source.name).write_bytes(b"original target")
    raced_target = destination / "source (1).raw"
    original_copy2 = export_service.shutil.copy2
    copy_calls = 0

    def copy_then_claim_first_candidate(copy_source: Path, temp: Path) -> None:
        nonlocal copy_calls
        copy_calls += 1
        original_copy2(copy_source, temp)
        if copy_calls == 1:
            raced_target.write_bytes(b"concurrent target")

    monkeypatch.setattr(
        export_service.shutil, "copy2", copy_then_claim_first_candidate
    )

    report = ExportService().export(
        copy_request(source, destination, ConflictPolicy.UNIQUE), lambda: False
    )

    result = report.results[0]
    assert result.success is True
    assert result.target == destination / "source (2).raw"
    assert result.target.read_bytes() == b"new photo"
    assert raced_target.read_bytes() == b"concurrent target"
    assert source.read_bytes() == b"new photo"
    assert copy_calls == 2
    assert list(destination.glob("*.partial")) == []


def test_symlink_loop_is_reported_and_later_item_continues(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    destination = tmp_path / "out"
    destination.mkdir()
    loop_target = destination / "loop.raw"
    loop_target.symlink_to(loop_target.name)
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(first, loop_target.name),
            ExportItem(second, second.name),
        ),
        mode=ExportMode.COPY,
        conflict=ConflictPolicy.OVERWRITE,
    )

    report = ExportService().export(request, lambda: False)

    assert len(report.results) == 2
    assert report.results[0].success is False
    assert report.results[0].error is not None
    assert report.results[1].success is True
    assert loop_target.is_symlink()
    assert (destination / second.name).read_bytes() == b"second"
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


def test_restore_conflict_keeps_new_source_and_quarantine(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    failed_quarantine: Path | None = None

    def occupy_source_then_fail(copy_source: Path, _temp: Path) -> None:
        nonlocal failed_quarantine
        failed_quarantine = copy_source
        source.write_bytes(b"other")
        raise OSError("copy failed")

    monkeypatch.setattr(
        export_service.shutil, "copy2", occupy_source_then_fail
    )

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "copy failed" in (result.error or "")
    assert failed_quarantine is not None
    assert str(failed_quarantine) in (result.error or "")
    assert result.recovery_path == failed_quarantine
    assert source.read_bytes() == b"other"
    assert failed_quarantine.read_bytes() == b"photo"
    assert not (destination / source.name).exists()


def test_cancel_after_quarantine_restores_source(tmp_path: Path) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    checks = 0

    def cancel_after_quarantine() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    report = ExportService().export(
        move_request(source, destination), cancel_after_quarantine
    )

    assert report.canceled is True
    assert len(report.results) == 1
    assert report.results[0].success is False
    assert "取消" in (report.results[0].error or "")
    assert source.read_bytes() == b"photo"
    assert not (destination / source.name).exists()
    assert list(tmp_path.glob("*.moving")) == []
    assert list(destination.glob("*.partial")) == []


def test_cancel_during_copy_restores_quarantine_before_promotion(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_copy2 = export_service.shutil.copy2
    canceled = False

    def copy_then_cancel(copy_source: Path, temp: Path) -> None:
        nonlocal canceled
        original_copy2(copy_source, temp)
        canceled = True

    monkeypatch.setattr(export_service.shutil, "copy2", copy_then_cancel)

    report = ExportService().export(
        move_request(source, destination), lambda: canceled
    )

    assert report.canceled is True
    assert len(report.results) == 1
    assert report.results[0].success is False
    assert "取消" in (report.results[0].error or "")
    assert source.read_bytes() == b"photo"
    assert not (destination / source.name).exists()
    assert list(tmp_path.glob("*.moving")) == []
    assert list(destination.glob("*.partial")) == []


def test_cancel_with_partial_cleanup_failure_stays_canceled(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_copy2 = export_service.shutil.copy2
    original_unlink = Path.unlink
    canceled = False

    def copy_then_cancel(copy_source: Path, temp: Path) -> None:
        nonlocal canceled
        original_copy2(copy_source, temp)
        canceled = True

    def fail_partial_unlink(path: Path, *args, **kwargs) -> None:
        if path.name.endswith(".partial"):
            raise OSError("cleanup failed")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export_service.shutil, "copy2", copy_then_cancel)
    monkeypatch.setattr(Path, "unlink", fail_partial_unlink)

    report = ExportService().export(
        move_request(source, destination), lambda: canceled
    )

    assert report.canceled is True
    assert len(report.results) == 1
    assert report.results[0].success is False
    assert "取消" in (report.results[0].error or "")
    assert "cleanup failed" in (report.results[0].error or "")
    assert source.read_bytes() == b"photo"
    assert not (destination / source.name).exists()
    assert list(tmp_path.glob("*.moving")) == []
    assert len(list(destination.glob("*.partial"))) == 1


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
@pytest.mark.parametrize(
    ("conflict", "expected_successes"),
    [
        (ConflictPolicy.SKIP, [True, False]),
        (ConflictPolicy.UNIQUE, [True, True]),
        (ConflictPolicy.OVERWRITE, [True, False]),
    ],
)
def test_batch_duplicate_target_names_never_overwrite_an_earlier_item(
    tmp_path: Path,
    mode: ExportMode,
    conflict: ConflictPolicy,
    expected_successes: list[bool],
) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first photo")
    second.write_bytes(b"second photo")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(first, "same.raw"),
            ExportItem(second, "same.raw"),
        ),
        mode=mode,
        conflict=conflict,
    )

    report = ExportService().export(request, lambda: False)

    assert [result.success for result in report.results] == expected_successes
    assert (destination / "same.raw").read_bytes() == b"first photo"
    if conflict is ConflictPolicy.UNIQUE:
        assert (destination / "same (1).raw").read_bytes() == b"second photo"
    else:
        assert not (destination / "same (1).raw").exists()
    if mode is ExportMode.COPY:
        assert first.read_bytes() == b"first photo"
        assert second.read_bytes() == b"second photo"
    elif conflict is ConflictPolicy.UNIQUE:
        assert not first.exists()
        assert not second.exists()
    else:
        assert not first.exists()
        assert second.read_bytes() == b"second photo"


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
@pytest.mark.parametrize(
    ("conflict", "first_succeeds"),
    [
        (ConflictPolicy.SKIP, False),
        (ConflictPolicy.UNIQUE, True),
        (ConflictPolicy.OVERWRITE, False),
    ],
)
def test_batch_target_never_aliases_another_batch_source(
    tmp_path: Path,
    mode: ExportMode,
    conflict: ConflictPolicy,
    first_succeeds: bool,
) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first photo")
    second.write_bytes(b"second photo")
    request = ExportRequest(
        destination=tmp_path,
        items=(
            ExportItem(first, second.name),
            ExportItem(second, "second-export.raw"),
        ),
        mode=mode,
        conflict=conflict,
    )

    report = ExportService().export(request, lambda: False)

    assert [result.success for result in report.results] == [first_succeeds, True]
    if conflict is ConflictPolicy.UNIQUE:
        assert report.results[0].target == tmp_path / "second (1).raw"
        assert (tmp_path / "second (1).raw").read_bytes() == b"first photo"
    else:
        assert report.results[0].target == second
        assert report.results[0].error is not None
    assert (tmp_path / "second-export.raw").read_bytes() == b"second photo"
    if mode is ExportMode.COPY:
        assert first.read_bytes() == b"first photo"
        assert second.read_bytes() == b"second photo"
    elif conflict is ConflictPolicy.UNIQUE:
        assert not first.exists()
        assert not second.exists()
    else:
        assert first.read_bytes() == b"first photo"
        assert not second.exists()


def test_move_runtime_error_is_reported_after_source_is_restored(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"

    def fail_with_runtime_error(_source: Path, _temp: Path) -> None:
        raise RuntimeError("unexpected copy failure")

    monkeypatch.setattr(export_service.shutil, "copy2", fail_with_runtime_error)

    report = ExportService().export(
        move_request(source, destination), lambda: False
    )

    assert len(report.results) == 1
    assert report.results[0].success is False
    assert "unexpected copy failure" in (report.results[0].error or "")
    assert source.read_bytes() == b"photo"
    assert list(tmp_path.glob("*.moving")) == []


def test_move_callback_runtime_error_is_reported_after_source_is_restored(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    checks = 0

    def broken_cancellation_callback() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        raise RuntimeError("cancel callback failed")

    report = ExportService().export(
        move_request(source, tmp_path / "out"), broken_cancellation_callback
    )

    assert len(report.results) == 1
    assert report.results[0].success is False
    assert "cancel callback failed" in (report.results[0].error or "")
    assert source.read_bytes() == b"photo"
    assert list(tmp_path.glob("*.moving")) == []


def test_move_rename_runtime_error_after_success_restores_known_location(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    original_rename = export_service.os.rename
    observed_quarantine: Path | None = None

    def rename_then_fail(rename_source: Path, quarantine: Path) -> None:
        nonlocal observed_quarantine
        observed_quarantine = quarantine
        original_rename(rename_source, quarantine)
        raise RuntimeError("rename completion uncertain")

    monkeypatch.setattr(export_service.os, "rename", rename_then_fail)

    report = ExportService().export(
        move_request(source, tmp_path / "out"), lambda: False
    )

    result = report.results[0]
    assert result.success is False
    assert "rename completion uncertain" in (result.error or "")
    assert result.recovery_path == source
    assert result.recovery_path.read_bytes() == b"photo"
    assert observed_quarantine is not None
    assert not observed_quarantine.exists()


def test_move_rename_terminal_exception_after_success_notes_restored_location(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    original_rename = export_service.os.rename
    observed_quarantine: Path | None = None

    def rename_then_interrupt(rename_source: Path, quarantine: Path) -> None:
        nonlocal observed_quarantine
        observed_quarantine = quarantine
        original_rename(rename_source, quarantine)
        raise KeyboardInterrupt()

    monkeypatch.setattr(export_service.os, "rename", rename_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        ExportService().export(
            move_request(source, tmp_path / "out"), lambda: False
        )

    assert source.read_bytes() == b"photo"
    assert observed_quarantine is not None
    assert not observed_quarantine.exists()
    assert str(source) in " ".join(caught.value.__notes__)


def test_terminal_partial_cleanup_exception_is_not_suppressed_by_runtime_error(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_unlink = Path.unlink

    def copy_partial_then_fail(_source: Path, temp: Path) -> None:
        temp.write_bytes(b"partial")
        raise RuntimeError("primary runtime failure")

    def interrupt_partial_cleanup(path: Path, *args, **kwargs) -> None:
        if path.name.endswith(".partial"):
            raise KeyboardInterrupt()
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export_service.shutil, "copy2", copy_partial_then_fail)
    monkeypatch.setattr(Path, "unlink", interrupt_partial_cleanup)

    with pytest.raises(KeyboardInterrupt) as caught:
        ExportService().export(
            move_request(source, destination), lambda: False
        )

    partials = list(destination.glob("*.partial"))
    assert len(partials) == 1
    assert source.read_bytes() == b"photo"
    notes = " ".join(caught.value.__notes__)
    assert str(partials[0]) in notes
    assert "primary runtime failure" in notes


def test_terminal_restore_exception_notes_quarantine_before_propagating(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    service = ExportService()

    monkeypatch.setattr(
        export_service.shutil,
        "copy2",
        lambda _source, _temp: (_ for _ in ()).throw(
            RuntimeError("primary runtime failure")
        ),
    )
    monkeypatch.setattr(
        service,
        "_restore_quarantine",
        lambda _quarantine, _source: (_ for _ in ()).throw(
            KeyboardInterrupt()
        ),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        service.export(move_request(source, tmp_path / "out"), lambda: False)

    quarantines = list(tmp_path.glob("*.moving"))
    assert len(quarantines) == 1
    notes = " ".join(caught.value.__notes__)
    assert str(quarantines[0]) in notes
    assert "primary runtime failure" in notes


def test_move_terminal_exception_propagates_only_after_source_is_restored(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    checks = 0

    def interrupted_cancellation_callback() -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            return False
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        ExportService().export(
            move_request(source, tmp_path / "out"),
            interrupted_cancellation_callback,
        )

    assert source.read_bytes() == b"photo"
    assert list(tmp_path.glob("*.moving")) == []


def test_terminal_cleanup_exception_identifies_promoted_move_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    destination = tmp_path / "out"
    original_unlink = Path.unlink
    failed_quarantine: Path | None = None

    def interrupt_quarantine_unlink(path: Path, *args, **kwargs) -> None:
        nonlocal failed_quarantine
        if path.name.endswith(".moving"):
            failed_quarantine = path
            raise KeyboardInterrupt()
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupt_quarantine_unlink)

    with pytest.raises(KeyboardInterrupt) as caught:
        ExportService().export(
            move_request(source, destination), lambda: False
        )

    assert failed_quarantine is not None
    assert (destination / source.name).read_bytes() == b"photo"
    assert failed_quarantine.read_bytes() == b"photo"
    assert str(failed_quarantine) in " ".join(caught.value.__notes__)


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
@pytest.mark.parametrize(
    "error_code",
    [errno.ENOSPC, getattr(errno, "EDQUOT", 69)],
    ids=["disk-full", "quota-exceeded"],
)
def test_capacity_error_stops_batch_before_later_items(
    tmp_path: Path,
    monkeypatch,
    mode: ExportMode,
    error_code: int,
) -> None:
    first = tmp_path / "first.raw"
    second = tmp_path / "second.raw"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(ExportItem(first, first.name), ExportItem(second, second.name)),
        mode=mode,
    )
    calls = 0

    def fail_for_capacity(_source: Path, _temp: Path) -> None:
        nonlocal calls
        calls += 1
        raise OSError(error_code, "capacity exhausted")

    monkeypatch.setattr(export_service.shutil, "copy2", fail_for_capacity)

    report = ExportService().export(request, lambda: False)

    assert calls == 1
    assert len(report.results) == 1
    assert report.results[0].success is False
    assert report.stopped_reason is not None
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert not (destination / second.name).exists()
    assert list(tmp_path.glob("*.moving")) == []


def test_quota_error_during_move_recovery_reports_quota_stop_reason(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.raw"
    source.write_bytes(b"photo")
    service = ExportService()

    monkeypatch.setattr(
        export_service.shutil,
        "copy2",
        lambda _source, _temp: (_ for _ in ()).throw(
            RuntimeError("copy runtime failure")
        ),
    )
    monkeypatch.setattr(
        service,
        "_restore_quarantine",
        lambda _quarantine, _source: (_ for _ in ()).throw(
            OSError(getattr(errno, "EDQUOT", 69), "quota exhausted")
        ),
    )

    report = service.export(
        move_request(source, tmp_path / "out"), lambda: False
    )

    assert report.stopped_reason == "存储配额已用尽，已停止后续导出"
    assert report.results[0].recovery_path is not None


def test_unique_batch_target_search_is_linear(tmp_path: Path, monkeypatch) -> None:
    item_count = 40
    destination = tmp_path / "out"
    items: list[ExportItem] = []
    for index in range(item_count):
        source = tmp_path / f"source-{index}.raw"
        source.write_bytes(str(index).encode())
        items.append(ExportItem(source, "same.raw"))
    request = ExportRequest(
        destination=destination,
        items=tuple(items),
        mode=ExportMode.COPY,
        conflict=ConflictPolicy.UNIQUE,
    )
    service = ExportService()
    original_canonical_path = service._canonical_path
    canonical_checks = 0

    def count_canonical_checks(path: Path) -> Path:
        nonlocal canonical_checks
        canonical_checks += 1
        return original_canonical_path(path)

    monkeypatch.setattr(service, "_canonical_path", count_canonical_checks)

    report = service.export(request, lambda: False)

    assert all(result.success for result in report.results)
    assert canonical_checks <= item_count * 4


_EQUIVALENT_TARGET_NAMES = [
    pytest.param("Same.raw", "same.raw", id="casefold"),
    pytest.param(
        "Caf\u00e9.raw",
        unicodedata.normalize("NFD", "Caf\u00e9.raw"),
        id="unicode-nfc-nfd",
    ),
]


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
@pytest.mark.parametrize(
    ("conflict", "expected_successes"),
    [
        (ConflictPolicy.SKIP, [True, False]),
        (ConflictPolicy.UNIQUE, [True, True]),
        (ConflictPolicy.OVERWRITE, [True, False]),
    ],
)
@pytest.mark.parametrize(
    ("first_target_name", "second_target_name"),
    _EQUIVALENT_TARGET_NAMES,
)
def test_batch_filesystem_equivalent_targets_are_reserved_once(
    tmp_path: Path,
    mode: ExportMode,
    conflict: ConflictPolicy,
    expected_successes: list[bool],
    first_target_name: str,
    second_target_name: str,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    first = source_dir / "first.raw"
    second = source_dir / "second.raw"
    first.write_bytes(b"first photo")
    second.write_bytes(b"second photo")
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(first, first_target_name),
            ExportItem(second, second_target_name),
        ),
        mode=mode,
        conflict=conflict,
    )

    report = ExportService().export(request, lambda: False)

    assert [result.success for result in report.results] == expected_successes
    assert report.results[0].target is not None
    assert report.results[0].target.read_bytes() == b"first photo"
    if conflict is ConflictPolicy.UNIQUE:
        expected_unique = destination / (
            f"{Path(second_target_name).stem} (1)"
            f"{Path(second_target_name).suffix}"
        )
        assert report.results[1].target == expected_unique
        assert expected_unique.read_bytes() == b"second photo"
    else:
        assert report.results[1].error is not None

    if mode is ExportMode.COPY:
        assert first.read_bytes() == b"first photo"
        assert second.read_bytes() == b"second photo"
    elif conflict is ConflictPolicy.UNIQUE:
        assert not first.exists()
        assert not second.exists()
    else:
        assert not first.exists()
        assert second.read_bytes() == b"second photo"


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
@pytest.mark.parametrize(
    ("conflict", "first_succeeds"),
    [
        (ConflictPolicy.SKIP, False),
        (ConflictPolicy.UNIQUE, True),
        (ConflictPolicy.OVERWRITE, False),
    ],
)
@pytest.mark.parametrize(
    ("source_name", "alias_target_name"),
    [
        pytest.param("Photo.raw", "photo.raw", id="casefold"),
        pytest.param(
            "Caf\u00e9.raw",
            unicodedata.normalize("NFD", "Caf\u00e9.raw"),
            id="unicode-nfc-nfd",
        ),
    ],
)
def test_batch_target_never_filesystem_aliases_another_source(
    tmp_path: Path,
    mode: ExportMode,
    conflict: ConflictPolicy,
    first_succeeds: bool,
    source_name: str,
    alias_target_name: str,
) -> None:
    first_dir = tmp_path / "first-source"
    first_dir.mkdir()
    first = first_dir / "first.raw"
    first.write_bytes(b"first photo")
    destination = tmp_path / "destination"
    destination.mkdir()
    second = destination / source_name
    second.write_bytes(b"second photo")
    request = ExportRequest(
        destination=destination,
        items=(
            ExportItem(first, alias_target_name),
            ExportItem(second, "second-export.raw"),
        ),
        mode=mode,
        conflict=conflict,
    )

    report = ExportService().export(request, lambda: False)

    assert [result.success for result in report.results] == [first_succeeds, True]
    if conflict is ConflictPolicy.UNIQUE:
        expected_unique = destination / (
            f"{Path(alias_target_name).stem} (1)"
            f"{Path(alias_target_name).suffix}"
        )
        assert report.results[0].target == expected_unique
        assert expected_unique.read_bytes() == b"first photo"
    else:
        assert report.results[0].error is not None
    assert (destination / "second-export.raw").read_bytes() == b"second photo"

    if mode is ExportMode.COPY:
        assert first.read_bytes() == b"first photo"
        assert second.read_bytes() == b"second photo"
    elif conflict is ConflictPolicy.UNIQUE:
        assert not first.exists()
        assert not second.exists()
    else:
        assert first.read_bytes() == b"first photo"
        assert not second.exists()


def test_unique_case_variants_share_one_linear_suffix_cursor(
    tmp_path: Path, monkeypatch
) -> None:
    item_count = 40
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    destination = tmp_path / "out"
    items: list[ExportItem] = []
    letters = "abcdef"
    for index in range(item_count):
        source = source_dir / f"source-{index}.raw"
        source.write_bytes(str(index).encode())
        case_variant = "".join(
            letter.upper() if index & (1 << bit) else letter
            for bit, letter in enumerate(letters)
        )
        items.append(ExportItem(source, f"{case_variant}.raw"))

    service = ExportService()
    original_filesystem_key = service._filesystem_key
    filesystem_key_checks = 0

    def count_filesystem_key_checks(path: Path) -> str:
        nonlocal filesystem_key_checks
        filesystem_key_checks += 1
        return original_filesystem_key(path)

    monkeypatch.setattr(service, "_filesystem_key", count_filesystem_key_checks)
    report = service.export(
        ExportRequest(
            destination=destination,
            items=tuple(items),
            mode=ExportMode.COPY,
            conflict=ConflictPolicy.UNIQUE,
        ),
        lambda: False,
    )
    checks_after_export = filesystem_key_checks

    assert all(result.success for result in report.results)
    assert len(
        {
            original_filesystem_key(result.target)
            for result in report.results
            if result.target is not None
        }
    ) == item_count
    assert checks_after_export <= item_count * 4
