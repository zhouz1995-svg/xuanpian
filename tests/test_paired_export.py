from pathlib import Path
import errno

import pytest

from src.xuanpian.services import export_service
from src.xuanpian.services.export_service import (
    ConflictPolicy,
    ExportItem,
    ExportMode,
    ExportRequest,
    ExportService,
    ExportUnit,
)


def _request(
    destination: Path,
    jpeg: Path,
    raw: Path,
    *,
    mode: ExportMode = ExportMode.COPY,
    conflict: ConflictPolicy = ConflictPolicy.SKIP,
) -> ExportRequest:
    return ExportRequest(
        destination=destination,
        items=(),
        units=(
            ExportUnit(
                "photo-1",
                (
                    ExportItem(jpeg, jpeg.name),
                    ExportItem(raw, raw.name),
                ),
            ),
        ),
        mode=mode,
        conflict=conflict,
    )


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    jpeg = tmp_path / "A.JPG"
    raw = tmp_path / "A.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw-original")
    return jpeg, raw


def test_empty_export_unit_is_rejected_without_creating_destination(
    tmp_path,
) -> None:
    destination = tmp_path / "out"
    request = ExportRequest(
        destination=destination,
        items=(),
        units=(ExportUnit("empty", ()),),
        mode=ExportMode.COPY,
    )

    report = ExportService().export(request, lambda: False)

    assert report.unit_results[0].success is False
    assert "不能为空" in (report.unit_results[0].error or "")
    assert not destination.exists()


def test_pair_conflict_fails_before_either_member_changes(tmp_path) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / raw.name).write_bytes(b"occupied")

    report = ExportService().export(
        _request(destination, jpeg, raw),
        lambda: False,
    )

    assert report.unit_results[0].success is False
    assert not (destination / jpeg.name).exists()
    assert (destination / raw.name).read_bytes() == b"occupied"
    assert jpeg.read_bytes() == b"jpeg"
    assert raw.read_bytes() == b"raw-original"


def test_pair_copy_failure_rolls_back_staged_members(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    original_copy = export_service.shutil.copy2

    def fail_raw(source: Path, target: Path) -> None:
        if source == raw:
            target.write_bytes(b"partial")
            raise OSError("RAW staging failed")
        original_copy(source, target)

    monkeypatch.setattr(export_service.shutil, "copy2", fail_raw)

    report = ExportService().export(
        _request(destination, jpeg, raw),
        lambda: False,
    )

    assert report.unit_results[0].success is False
    assert "RAW staging failed" in (report.unit_results[0].error or "")
    assert jpeg.exists() and raw.exists()
    assert not (destination / jpeg.name).exists()
    assert not (destination / raw.name).exists()
    assert list(destination.glob("*.partial")) == []


def test_primary_failure_reports_partial_that_cannot_be_cleaned(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    original_copy = export_service.shutil.copy2
    original_unlink = Path.unlink

    def fail_raw(source: Path, target: Path) -> None:
        if source == raw:
            target.write_bytes(b"partial")
            raise OSError("RAW staging failed")
        original_copy(source, target)

    def fail_partial_cleanup(path: Path, *args, **kwargs):
        if path.name.endswith(".partial"):
            raise OSError("partial cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(export_service.shutil, "copy2", fail_raw)
    monkeypatch.setattr(Path, "unlink", fail_partial_cleanup)

    report = ExportService().export(
        _request(destination, jpeg, raw),
        lambda: False,
    )

    result = report.unit_results[0]
    assert result.success is False
    assert result.recovery_paths
    assert all(path.exists() for path in result.recovery_paths)
    assert all(path.name.endswith(".partial") for path in result.recovery_paths)


def test_pair_move_promotion_failure_restores_both_sources(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    raw_target = destination / raw.name
    original_link = export_service.os.link

    def fail_raw_promotion(source, target, *args, **kwargs):
        if Path(target) == raw_target:
            raise OSError("RAW promotion failed")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(export_service.os, "link", fail_raw_promotion)

    report = ExportService().export(
        _request(destination, jpeg, raw, mode=ExportMode.MOVE),
        lambda: False,
    )

    assert report.unit_results[0].success is False
    assert jpeg.read_bytes() == b"jpeg"
    assert raw.read_bytes() == b"raw-original"
    assert not (destination / jpeg.name).exists()
    assert not raw_target.exists()
    assert list(tmp_path.glob("*.moving")) == []
    assert list(destination.glob("*.partial")) == []


def test_pair_move_restore_failure_retains_recovery_file(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    raw_target = destination / raw.name
    original_link = export_service.os.link

    def fail_raw_promotion_and_restore(source, target, *args, **kwargs):
        target = Path(target)
        if target in {raw_target, raw}:
            raise OSError("RAW restore failed")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(
        export_service.os,
        "link",
        fail_raw_promotion_and_restore,
    )

    report = ExportService().export(
        _request(destination, jpeg, raw, mode=ExportMode.MOVE),
        lambda: False,
    )

    result = report.unit_results[0]
    assert result.success is False
    assert jpeg.read_bytes() == b"jpeg"
    assert not raw.exists()
    assert result.recovery_paths
    assert all(path.exists() for path in result.recovery_paths)
    assert any(
        path.read_bytes() == b"raw-original"
        for path in result.recovery_paths
    )


def test_rollback_capacity_failure_stops_later_units(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    second_jpeg = tmp_path / "B.JPG"
    second_raw = tmp_path / "B.NEF"
    second_jpeg.write_bytes(b"jpeg-b")
    second_raw.write_bytes(b"raw-b")
    destination = tmp_path / "out"
    raw_target = destination / raw.name
    original_link = export_service.os.link

    def fail_promotion_and_capacity_restore(source, target, *args, **kwargs):
        target = Path(target)
        if target == raw_target:
            raise OSError("RAW promotion failed")
        if target == raw:
            raise OSError(errno.ENOSPC, "restore disk full")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(
        export_service.os,
        "link",
        fail_promotion_and_capacity_restore,
    )
    request = ExportRequest(
        destination=destination,
        items=(),
        units=(
            ExportUnit(
                "photo-a",
                (
                    ExportItem(jpeg, jpeg.name),
                    ExportItem(raw, raw.name),
                ),
            ),
            ExportUnit(
                "photo-b",
                (
                    ExportItem(second_jpeg, second_jpeg.name),
                    ExportItem(second_raw, second_raw.name),
                ),
            ),
        ),
        mode=ExportMode.MOVE,
    )

    report = ExportService().export(request, lambda: False)

    assert report.stopped_reason == "磁盘空间不足，已停止后续导出"
    assert [unit.unit_id for unit in report.unit_results] == ["photo-a"]
    assert second_jpeg.exists() and second_raw.exists()
    assert not (destination / second_jpeg.name).exists()


@pytest.mark.parametrize("mode", [ExportMode.COPY, ExportMode.MOVE])
def test_pair_overwrite_failure_restores_original_targets(
    tmp_path, monkeypatch, mode
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    jpeg_target = destination / jpeg.name
    raw_target = destination / raw.name
    jpeg_target.write_bytes(b"old-jpeg")
    raw_target.write_bytes(b"old-raw")
    original_replace = export_service.os.replace

    def fail_raw_promotion(source, target):
        if Path(source).name.endswith(".partial") and Path(target) == raw_target:
            raise OSError("RAW overwrite promotion failed")
        return original_replace(source, target)

    monkeypatch.setattr(export_service.os, "replace", fail_raw_promotion)

    report = ExportService().export(
        _request(
            destination,
            jpeg,
            raw,
            mode=mode,
            conflict=ConflictPolicy.OVERWRITE,
        ),
        lambda: False,
    )

    assert report.unit_results[0].success is False
    assert jpeg_target.read_bytes() == b"old-jpeg"
    assert raw_target.read_bytes() == b"old-raw"
    assert jpeg.read_bytes() == b"jpeg"
    assert raw.read_bytes() == b"raw-original"
    assert list(destination.glob("*.partial")) == []
    assert list(destination.glob("*.backup")) == []


def test_overwrite_backup_cleanup_failure_keeps_committed_targets(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / jpeg.name).write_bytes(b"old-jpeg")
    (destination / raw.name).write_bytes(b"old-raw")
    original_unlink = Path.unlink
    backup_unlinks = 0

    def fail_second_backup(path: Path, *args, **kwargs):
        nonlocal backup_unlinks
        if path.name.endswith(".backup"):
            backup_unlinks += 1
            if backup_unlinks == 2:
                raise OSError("backup cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_second_backup)

    report = ExportService().export(
        _request(
            destination,
            jpeg,
            raw,
            conflict=ConflictPolicy.OVERWRITE,
        ),
        lambda: False,
    )

    result = report.unit_results[0]
    assert result.success is True
    assert (destination / jpeg.name).read_bytes() == b"jpeg"
    assert (destination / raw.name).read_bytes() == b"raw-original"
    assert result.recovery_paths
    assert all(path.exists() for path in result.recovery_paths)


def test_quarantine_rename_completed_then_raised_is_rolled_back(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    original_rename = export_service.os.rename

    def rename_then_raise(source, target):
        original_rename(source, target)
        if Path(source) == raw:
            raise KeyboardInterrupt("after quarantine rename")

    monkeypatch.setattr(export_service.os, "rename", rename_then_raise)

    with pytest.raises(KeyboardInterrupt):
        ExportService().export(
            _request(destination, jpeg, raw, mode=ExportMode.MOVE),
            lambda: False,
        )

    assert jpeg.read_bytes() == b"jpeg"
    assert raw.read_bytes() == b"raw-original"
    assert list(tmp_path.glob("*.moving")) == []
    assert not (destination / jpeg.name).exists()


def test_link_promotion_completed_then_raised_removes_target(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    jpeg_target = destination / jpeg.name
    original_link = export_service.os.link

    def link_then_raise(source, target, *args, **kwargs):
        result = original_link(source, target, *args, **kwargs)
        if Path(target) == jpeg_target:
            raise KeyboardInterrupt("after link promotion")
        return result

    monkeypatch.setattr(export_service.os, "link", link_then_raise)

    with pytest.raises(KeyboardInterrupt):
        ExportService().export(
            _request(destination, jpeg, raw),
            lambda: False,
        )

    assert jpeg.exists() and raw.exists()
    assert not jpeg_target.exists()
    assert not (destination / raw.name).exists()


def test_backup_rename_completed_then_raised_restores_original(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    jpeg_target = destination / jpeg.name
    raw_target = destination / raw.name
    jpeg_target.write_bytes(b"old-jpeg")
    raw_target.write_bytes(b"old-raw")
    original_replace = export_service.os.replace

    def backup_then_raise(source, target):
        result = original_replace(source, target)
        if (
            Path(source) == raw_target
            and Path(target).name.endswith(".backup")
        ):
            raise KeyboardInterrupt("after backup rename")
        return result

    monkeypatch.setattr(export_service.os, "replace", backup_then_raise)

    with pytest.raises(KeyboardInterrupt):
        ExportService().export(
            _request(
                destination,
                jpeg,
                raw,
                conflict=ConflictPolicy.OVERWRITE,
            ),
            lambda: False,
        )

    assert jpeg_target.read_bytes() == b"old-jpeg"
    assert raw_target.read_bytes() == b"old-raw"
    assert list(destination.glob("*.backup")) == []


def test_overwrite_promotion_completed_then_raised_restores_targets(
    tmp_path, monkeypatch
) -> None:
    jpeg, raw = _sources(tmp_path)
    destination = tmp_path / "out"
    destination.mkdir()
    jpeg_target = destination / jpeg.name
    raw_target = destination / raw.name
    jpeg_target.write_bytes(b"old-jpeg")
    raw_target.write_bytes(b"old-raw")
    original_replace = export_service.os.replace

    def replace_then_raise(source, target):
        original_replace(source, target)
        if (
            Path(source).name.endswith(".partial")
            and Path(target) == raw_target
        ):
            raise KeyboardInterrupt("after promotion")

    monkeypatch.setattr(export_service.os, "replace", replace_then_raise)

    with pytest.raises(KeyboardInterrupt):
        ExportService().export(
            _request(
                destination,
                jpeg,
                raw,
                conflict=ConflictPolicy.OVERWRITE,
            ),
            lambda: False,
        )

    assert jpeg_target.read_bytes() == b"old-jpeg"
    assert raw_target.read_bytes() == b"old-raw"
    assert jpeg.exists() and raw.exists()


def test_missing_source_fails_preflight_before_overwrite_backup(tmp_path) -> None:
    jpeg, raw = _sources(tmp_path)
    raw.unlink()
    destination = tmp_path / "out"
    destination.mkdir()
    jpeg_target = destination / jpeg.name
    jpeg_target.write_bytes(b"old-jpeg")

    report = ExportService().export(
        _request(
            destination,
            jpeg,
            raw,
            conflict=ConflictPolicy.OVERWRITE,
        ),
        lambda: False,
    )

    assert report.unit_results[0].success is False
    assert jpeg_target.read_bytes() == b"old-jpeg"
    assert jpeg.read_bytes() == b"jpeg"
    assert list(destination.glob("*.backup")) == []


def test_mixed_items_and_units_are_rejected_with_results_for_both(
    tmp_path,
) -> None:
    jpeg, raw = _sources(tmp_path)
    extra = tmp_path / "extra.jpg"
    extra.write_bytes(b"extra")
    request = _request(tmp_path / "out", jpeg, raw)
    request = ExportRequest(
        destination=request.destination,
        items=(ExportItem(extra, extra.name),),
        units=request.units,
        mode=request.mode,
    )

    report = ExportService().export(request, lambda: False)

    assert all(not unit.success for unit in report.unit_results)
    assert len(report.results) == 3
    assert not request.destination.exists()
