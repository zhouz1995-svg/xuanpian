from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.build_metadata_runtime import (
    _absolute_output,
    _clean_build_environment,
    _load_source_manifest,
    build_runtime,
    safe_extract,
    verify_archive,
)


def test_wrong_digest_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    archive.write_bytes(b"not approved")

    with pytest.raises(ValueError, match="digest"):
        verify_archive(archive, "0" * 64)


@pytest.mark.parametrize(
    "member_name",
    ("../escaped.txt", "/absolute.txt", "nested/../../escaped.txt"),
)
def test_archive_paths_cannot_escape_destination(
    tmp_path: Path, member_name: str
) -> None:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        payload = b"not allowed"
        member = tarfile.TarInfo(member_name)
        member.size = len(payload)
        output.addfile(member, io.BytesIO(payload))

    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="unsafe archive path"):
        safe_extract(archive, destination)

    assert not destination.exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_archive_symlink_cannot_escape_destination(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("runtime-link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../outside"
        output.addfile(member)

    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="unsafe archive link"):
        safe_extract(archive, destination)

    assert not destination.exists()


def test_archive_device_entry_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("device")
        member.type = tarfile.CHRTYPE
        output.addfile(member)

    destination = tmp_path / "runtime"
    with pytest.raises(ValueError, match="unsupported archive entry"):
        safe_extract(archive, destination)

    assert not destination.exists()


def test_build_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "runtime"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        build_runtime(tmp_path / "unused-manifest.json", output)

    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_build_refuses_dangling_output_symlink(tmp_path: Path) -> None:
    target = tmp_path / "missing-runtime"
    output = tmp_path / "runtime"
    output.symlink_to(target)

    with pytest.raises(FileExistsError, match="already exists"):
        build_runtime(tmp_path / "unused-manifest.json", output)

    assert output.is_symlink()
    assert output.readlink() == target
    assert not target.exists()


def test_build_environment_selects_verified_sdk_and_removes_external_perl_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SDKROOT", "/untrusted/sdk")
    monkeypatch.setenv("PERL5LIB", "/untrusted/perl")

    environment = _clean_build_environment("/verified/MacOSX.sdk", "12.0")

    assert environment["SDKROOT"] == "/verified/MacOSX.sdk"
    assert "PERL5LIB" not in environment


def test_build_target_overrides_host_and_removes_compiler_overrides(monkeypatch):
    monkeypatch.setenv("MACOSX_DEPLOYMENT_TARGET", "26.0")
    for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "LD"):
        monkeypatch.setenv(key, "untrusted -mmacosx-version-min=26.0")

    environment = _clean_build_environment("/verified/MacOSX.sdk", "12.0")

    assert environment["MACOSX_DEPLOYMENT_TARGET"] == "12.0"
    for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "LD"):
        assert key not in environment


@pytest.mark.parametrize("target", [None, "", "12", "12.0 -bad", 12.0, "10.15"])
def test_manifest_rejects_invalid_arm64_deployment_target(tmp_path, target):
    manifest = json.loads((Path(__file__).parents[1] / "packaging/runtime-sources.json").read_text())
    manifest["build"]["deployment_target"] = target
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError, match="deployment_target"):
        _load_source_manifest(path)


def test_relative_output_is_resolved_before_creating_staging_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _absolute_output(Path("build/runtime")) == tmp_path / "build" / "runtime"
