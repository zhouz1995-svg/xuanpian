from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.xuanpian.domain.technical_analysis import Rect
from src.xuanpian.engine import af_metadata, metadata_runtime
from src.xuanpian.engine.metadata_runtime import MetadataRuntime, RuntimeUnavailable


def _metadata(**changes: object) -> dict[str, object]:
    data: dict[str, object] = {
        "FileType": "JPEG",
        "Make": "NIKON CORPORATION",
        "Model": "NIKON Z50_2",
        "AFInfo2Version": "0402",
        "AFCoordinatesAvailable": 1,
        "AFImageWidth": 400,
        "AFImageHeight": 200,
        "AFAreaXPosition": 100,
        "AFAreaYPosition": 60,
        "AFAreaWidth": 40,
        "AFAreaHeight": 20,
        "AFPointsUsed": "00 00 00 00",
        "Orientation": 1,
        "FocusResult": 1,
    }
    data.update(changes)
    return data


@pytest.mark.parametrize(
    ("orientation", "decoded_size", "expected"),
    [
        (1, (400, 200), (.2, .25, .1, .1)),
        (2, (400, 200), (.7, .25, .1, .1)),
        (3, (400, 200), (.7, .65, .1, .1)),
        (4, (400, 200), (.2, .65, .1, .1)),
        (5, (200, 400), (.25, .2, .1, .1)),
        (6, (200, 400), (.65, .2, .1, .1)),
        (7, (200, 400), (.65, .7, .1, .1)),
        (8, (200, 400), (.25, .7, .1, .1)),
    ],
)
def test_parse_af_metadata_transforms_all_exif_orientations(
    orientation: int,
    decoded_size: tuple[int, int],
    expected: tuple[float, float, float, float],
) -> None:
    hint = af_metadata.parse_af_metadata(
        _metadata(Orientation=orientation), decoded_size
    )

    assert hint.reason == "available"
    assert hint.bounds is not None
    assert (
        hint.bounds.x,
        hint.bounds.y,
        hint.bounds.width,
        hint.bounds.height,
    ) == pytest.approx(expected)


def test_camera_focus_result_does_not_change_the_validated_region() -> None:
    focused = af_metadata.parse_af_metadata(_metadata(FocusResult=1), (400, 200))
    not_focused = af_metadata.parse_af_metadata(_metadata(FocusResult=0), (400, 200))

    assert focused == not_focused == af_metadata.AFHint(
        Rect(.2, .25, .1, .1), "available"
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"Make": "NIKON"}, "unsupported_camera"),
        ({"Model": "NIKON Z50"}, "unsupported_camera"),
        ({"AFInfo2Version": "0401"}, "unsupported_af_version"),
    ],
)
def test_parse_rejects_unproven_camera_formats(
    changes: dict[str, object], reason: str
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(**changes), (400, 200)
    ) == af_metadata.AFHint(reason=reason)


@pytest.mark.parametrize(
    ("file_type", "reason"),
    [
        ("NEF", "raw_mapping_unavailable"),
        ("TIFF", "unsupported_file_type"),
        (None, "unsupported_file_type"),
        ([], "unsupported_file_type"),
        ({}, "unsupported_file_type"),
    ],
)
def test_parse_rejects_raw_non_jpeg_or_missing_file_types(
    file_type: object, reason: str
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(FileType=file_type), (400, 200)
    ) == af_metadata.AFHint(reason=reason)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"AFCoordinatesAvailable": 0}, "coordinates_unavailable"),
        (
            {"AFCoordinatesAvailable": 0, "AFPointsUsed": "00 00 10 00"},
            "points_only",
        ),
        ({"AFCoordinatesAvailable": 2}, "invalid_coordinate_flag"),
        ({"AFCoordinatesAvailable": True}, "invalid_coordinate_flag"),
    ],
)
def test_parse_rejects_missing_point_only_or_invalid_coordinate_flags(
    changes: dict[str, object], reason: str
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(**changes), (400, 200)
    ) == af_metadata.AFHint(reason=reason)


@pytest.mark.parametrize(
    "changes",
    [
        {"AFImageWidth": 0},
        {"AFImageHeight": -1},
        {"AFImageWidth": 400.5},
        {"AFImageHeight": True},
        {"AFImageWidth": float("inf")},
    ],
)
def test_parse_rejects_non_positive_or_non_integer_af_dimensions(
    changes: dict[str, object]
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(**changes), (400, 200)
    ) == af_metadata.AFHint(reason="invalid_af_dimensions")


@pytest.mark.parametrize(
    "changes",
    [
        {"AFAreaXPosition": float("nan")},
        {"AFAreaYPosition": float("inf")},
        {"AFAreaWidth": 0},
        {"AFAreaHeight": -1},
        {"AFAreaWidth": "40"},
        {"AFAreaHeight": True},
    ],
)
def test_parse_rejects_nonfinite_non_numeric_or_empty_af_areas(
    changes: dict[str, object]
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(**changes), (400, 200)
    ) == af_metadata.AFHint(reason="invalid_af_area")


@pytest.mark.parametrize(
    "changes",
    [
        {"AFAreaXPosition": 10},
        {"AFAreaXPosition": 390},
        {"AFAreaYPosition": 5},
        {"AFAreaYPosition": 195},
        {"AFAreaWidth": 500},
        {"AFAreaHeight": 300},
    ],
)
def test_parse_rejects_af_areas_whose_corners_leave_the_image(
    changes: dict[str, object]
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(**changes), (400, 200)
    ) == af_metadata.AFHint(reason="af_area_out_of_bounds")


@pytest.mark.parametrize("orientation", [0, 9, 1.5, True, "8"])
def test_parse_rejects_invalid_exif_orientations(orientation: object) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(Orientation=orientation), (400, 200)
    ) == af_metadata.AFHint(reason="invalid_orientation")


@pytest.mark.parametrize(
    ("decoded_size", "reason"),
    [
        ((200, 400), "decoded_dimensions_mismatch"),
        ((0, 200), "invalid_decoded_dimensions"),
        ((400.0, 200), "invalid_decoded_dimensions"),
        ((True, 200), "invalid_decoded_dimensions"),
    ],
)
def test_parse_requires_exact_positive_integer_decoded_dimensions(
    decoded_size: tuple[object, object], reason: str
) -> None:
    assert af_metadata.parse_af_metadata(
        _metadata(), decoded_size  # type: ignore[arg-type]
    ) == af_metadata.AFHint(reason=reason)


def test_read_af_hint_uses_safe_bounded_stat_cache_and_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    calls: list[tuple[list[str], dict[str, object]]] = []

    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda name: "/opt/test/exiftool")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, json.dumps([_metadata()]), ""
        )

    monkeypatch.setattr(af_metadata.subprocess, "run", fake_run)

    first = af_metadata.read_af_hint(str(photo), (400, 200))
    second = af_metadata.read_af_hint(str(photo), (400, 200))

    assert first == second == af_metadata.AFHint(Rect(.2, .25, .1, .1), "available")
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[0:3] == ["/opt/test/exiftool", "-config", ""]
    assert command[-1] == str(photo.resolve())
    assert "-FocusResult" not in command
    assert kwargs == {
        "capture_output": True,
        "text": True,
        "timeout": 5,
        "check": False,
        "env": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **{
            name: os.environ[name]
            for name in ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
            if name in os.environ
        }},
    }

    photo.write_bytes(b"jpeg changed")
    af_metadata.read_af_hint(str(photo), (400, 200))
    assert len(calls) == 2


def test_read_af_hint_frozen_missing_runtime_does_not_use_system_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda _: pytest.fail("system lookup"))
    monkeypatch.setattr(af_metadata.subprocess, "run", lambda *a, **k: pytest.fail("subprocess"))
    assert af_metadata.read_af_hint(str(photo), (400, 200)) == af_metadata.AFHint(
        reason="metadata_runtime_missing"
    )


def test_frozen_app_selects_only_its_resource_root(tmp_path: Path) -> None:
    app = tmp_path / "选片APP.app"
    frameworks = app / "Contents/Frameworks"
    resources = app / "Contents/Resources"
    frameworks.mkdir(parents=True)
    resources.mkdir()
    assert af_metadata._frozen_runtime_root(frameworks) == resources


def test_frozen_app_rejects_missing_resources(tmp_path: Path) -> None:
    frameworks = tmp_path / "选片APP.app/Contents/Frameworks"
    frameworks.mkdir(parents=True)
    with pytest.raises(RuntimeUnavailable, match="metadata_runtime_missing"):
        af_metadata._frozen_runtime_root(frameworks)


def test_frozen_app_rejects_resource_symlink_escape(tmp_path: Path) -> None:
    frameworks = tmp_path / "选片APP.app/Contents/Frameworks"
    frameworks.mkdir(parents=True)
    (frameworks.parent / "Resources").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(RuntimeUnavailable, match="metadata_runtime_invalid"):
        af_metadata._frozen_runtime_root(frameworks)


def test_read_af_hint_uses_runtime_prefix_environment_and_identity_in_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    runtime = MetadataRuntime(("/bundle/perl", "-I", "/bundle/lib", "/bundle/exiftool", "-config", ""),
                              (("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),), "revision-1")
    monkeypatch.setattr(af_metadata, "resolve_metadata_runtime", lambda **_: runtime, raising=False)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps([_metadata()]), "")

    monkeypatch.setattr(af_metadata.subprocess, "run", fake_run)
    assert af_metadata.read_af_hint(str(photo), (400, 200)).reason == "available"
    assert af_metadata.read_af_hint(str(photo), (400, 200)).reason == "available"
    assert len(calls) == 1
    assert calls[0][0][:8] == ["/bundle/perl", "-I", "/bundle/lib", "/bundle/exiftool", "-config", "", "-j", "-n"]
    assert calls[0][1]["env"] == {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    runtime = MetadataRuntime(runtime.command_prefix, runtime.environment, "revision-2")
    assert af_metadata.read_af_hint(str(photo), (400, 200)).reason == "available"
    assert len(calls) == 2


def test_read_af_hint_preserves_runtime_failure_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")

    def unavailable(**_: object) -> MetadataRuntime:
        raise RuntimeUnavailable("metadata_runtime_corrupt")

    monkeypatch.setattr(af_metadata, "resolve_metadata_runtime", unavailable, raising=False)
    assert af_metadata.read_af_hint(str(photo), (400, 200)) == af_metadata.AFHint(
        reason="metadata_runtime_corrupt"
    )


def test_read_af_hint_rejects_raw_before_running_exiftool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "photo.nef"
    raw.write_bytes(b"raw")
    monkeypatch.setattr(
        af_metadata.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("ExifTool must not run for RAW"),
    )

    assert af_metadata.read_af_hint(str(raw), (400, 200)) == af_metadata.AFHint(
        reason="raw_mapping_unavailable"
    )


def test_read_af_hint_rejects_renamed_raw_reported_by_exiftool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    renamed_raw = tmp_path / "renamed-raw.jpg"
    renamed_raw.write_bytes(b"raw with a misleading suffix")
    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda name: "/test/exiftool")

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert "-FileType" in command
        return subprocess.CompletedProcess(
            command, 0, json.dumps([_metadata(FileType="NEF")]), ""
        )

    monkeypatch.setattr(af_metadata.subprocess, "run", fake_run)

    assert af_metadata.read_af_hint(
        str(renamed_raw), (400, 200)
    ) == af_metadata.AFHint(reason="raw_mapping_unavailable")


def test_read_af_hint_treats_oversized_af_number_as_invalid_optional_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    photo = tmp_path / "oversized-af-number.jpg"
    photo.write_bytes(b"jpeg")
    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda name: "/test/exiftool")
    monkeypatch.setattr(
        af_metadata.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps([_metadata(AFAreaXPosition=10**400)]),
            "",
        ),
    )

    assert af_metadata.read_af_hint(
        str(photo), (400, 200)
    ) == af_metadata.AFHint(reason="invalid_af_area")


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (subprocess.TimeoutExpired("exiftool", 5), "metadata_timeout"),
        (OSError("cannot execute"), "metadata_read_error"),
    ],
)
def test_read_af_hint_gracefully_handles_process_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
    reason: str,
) -> None:
    photo = tmp_path / f"{reason}.jpg"
    photo.write_bytes(b"jpeg")
    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda name: "/test/exiftool")

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(af_metadata.subprocess, "run", fail)

    assert af_metadata.read_af_hint(str(photo), (400, 200)) == af_metadata.AFHint(
        reason=reason
    )


@pytest.mark.parametrize(
    ("stdout", "returncode", "reason"),
    [
        ("not json", 0, "metadata_invalid_json"),
        ("[]", 0, "metadata_missing"),
        ("{}", 0, "metadata_invalid_json"),
        ("", 1, "metadata_read_error"),
    ],
)
def test_read_af_hint_gracefully_handles_invalid_exiftool_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    returncode: int,
    reason: str,
) -> None:
    photo = tmp_path / f"result-{reason}-{returncode}.jpg"
    photo.write_bytes(b"jpeg")
    monkeypatch.setattr(metadata_runtime.shutil, "which", lambda name: "/test/exiftool")
    monkeypatch.setattr(
        af_metadata.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout, "failure"
        ),
    )

    assert af_metadata.read_af_hint(str(photo), (400, 200)) == af_metadata.AFHint(
        reason=reason
    )
