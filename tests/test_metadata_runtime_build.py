from __future__ import annotations

import io
import ctypes
import errno
import json
import shutil
import subprocess
import sys
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
from scripts import build_metadata_runtime as builder


def test_intel_source_profile_keeps_approved_sources_and_toolchain() -> None:
    packaging = Path(__file__).parents[1] / "packaging"
    arm = _load_source_manifest(packaging / "runtime-sources.json")
    intel = _load_source_manifest(packaging / "runtime-sources-macos-x86_64.json")
    assert intel["target"] == {"platform": "macos", "architecture": "x86_64"}
    assert intel["sources"] == arm["sources"]
    assert intel["build"]["deployment_target"] == "12.0"
    assert intel["build"]["sdk"] == {"name": "macosx", "version": "15.5"}
    assert intel["build"]["compiler"]["version"] == "Apple clang version 17.0.0 (clang-1700.0.13.5)"


def test_windows_preparation_helpers_import_without_darwin_sysctl(tmp_path):
    archive = tmp_path / "archive"
    archive.write_bytes(b"test")
    script = """
import ctypes
import sys
from pathlib import Path
def unavailable(*args, **kwargs):
    raise AssertionError('Darwin sysctl must remain lazy')
ctypes.CDLL = unavailable
from scripts.prepare_windows_runtime import verify_archive
from scripts.build_metadata_runtime import _sha256
print(_sha256(Path(sys.argv[1])))
"""
    result = subprocess.run([sys.executable, "-c", script, str(archive)],
                            cwd=Path(__file__).parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


@pytest.mark.parametrize("explicit", [False, True])
def test_runtime_cli_retains_manifest_target_and_accepts_matching_architecture(tmp_path, monkeypatch, explicit):
    manifest = Path(__file__).parents[1] / "packaging/runtime-sources-macos-x86_64.json"
    def build(source, output):
        assert source == manifest
        return output / "manifest.json"
    monkeypatch.setattr(builder, "build_runtime", build)
    arguments = ["--manifest", str(manifest), "--output", str(tmp_path / "runtime")]
    if explicit:
        arguments += ["--architecture", "x86_64"]
    assert builder.main(arguments) == 0


def test_runtime_cli_rejects_architecture_disagreeing_with_source_before_build(tmp_path, monkeypatch, capsys):
    manifest = Path(__file__).parents[1] / "packaging/runtime-sources.json"
    monkeypatch.setattr(builder, "build_runtime", lambda *args: pytest.fail("must not build mismatched target"))
    with pytest.raises(SystemExit) as error:
        builder.main(["--manifest", str(manifest), "--output", str(tmp_path / "runtime"),
                      "--architecture", "x86_64"])
    assert error.value.code == 2
    assert "source manifest" in capsys.readouterr().err


@pytest.mark.parametrize("architecture", ["universal2", "amd64", "", None])
def test_source_profile_rejects_unknown_architecture(tmp_path, architecture):
    manifest = json.loads((Path(__file__).parents[1] / "packaging/runtime-sources.json").read_text())
    manifest["target"]["architecture"] = architecture
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="target"):
        _load_source_manifest(path)


@pytest.mark.parametrize("architecture,host", [("x86_64", "arm64"), ("arm64", "x86_64"), ("universal2", "universal2")])
def test_runtime_host_must_match_supported_target(monkeypatch, architecture, host):
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: host)
    with pytest.raises((RuntimeError, ValueError), match="architecture|host|supported"):
        builder._validate_host({}, architecture=architecture)


def test_runtime_accepts_native_intel_host_with_pinned_sdk(tmp_path, monkeypatch):
    sdk = tmp_path / "MacOSX.sdk"
    sdk.mkdir()
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(builder, "_is_translated_process", lambda: False, raising=False)
    version = "Apple clang version 17.0.0 (clang-1700.0.13.5)"
    answers = {
        ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"): "15.5\n",
        ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"): str(sdk) + "\n",
        ("/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"): "/approved/clang\n",
        ("/approved/clang", "--version"): version + "\nTarget: x86_64-apple-darwin\n",
    }
    monkeypatch.setattr(builder.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, answers[tuple(argv)], ""))
    assert builder._validate_host({"sdk": {"name": "macosx", "version": "15.5"},
                                  "compiler": {"version": version}}, architecture="x86_64") == (
        "/approved/clang", str(sdk))


@pytest.mark.parametrize("translated,sysctl_error", [(0, 0), (1, 0), (0, errno.ENOENT),
                                                     (0, errno.EPERM), (0, errno.EIO), (2, 0)])
def test_runtime_host_distinguishes_native_intel_from_rosetta_and_query_failures(
    tmp_path, monkeypatch, translated, sysctl_error,
):
    sdk = tmp_path / "MacOSX.sdk"
    sdk.mkdir()
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    def sysctl(name, value, size, new_value, new_size):
        assert name == b"sysctl.proc_translated"
        assert new_value is None and new_size == 0
        ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value = translated
        ctypes.set_errno(sysctl_error)
        return -1 if sysctl_error else 0
    class Library:
        sysctlbyname = staticmethod(sysctl)
    monkeypatch.setattr(ctypes, "CDLL", lambda name, *, use_errno: Library())
    version = "Apple clang version 17.0.0 (clang-1700.0.13.5)"
    answers = {
        ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"): "15.5\n",
        ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"): str(sdk) + "\n",
        ("/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"): "/approved/clang\n",
        ("/approved/clang", "--version"): version + "\n",
    }
    monkeypatch.setattr(builder.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, answers[tuple(argv)], ""))
    build = {"sdk": {"name": "macosx", "version": "15.5"}, "compiler": {"version": version}}
    if translated == 1:
        with pytest.raises(RuntimeError, match="Rosetta|translated"):
            builder._validate_host(build, architecture="x86_64")
    elif sysctl_error not in (0, errno.ENOENT):
        with pytest.raises(OSError) as failure:
            builder._validate_host(build, architecture="x86_64")
        assert failure.value.errno == sysctl_error
    elif translated not in (0, 1):
        with pytest.raises(RuntimeError, match="translation"):
            builder._validate_host(build, architecture="x86_64")
    else:
        assert builder._validate_host(build, architecture="x86_64") == ("/approved/clang", str(sdk))


def test_intel_runtime_build_uses_target_for_all_upstream_launches(tmp_path, monkeypatch):
    manifest = json.loads((Path(__file__).parents[1] / "packaging/runtime-sources.json").read_text())
    manifest["target"]["architecture"] = "x86_64"
    source = tmp_path / "sources.json"
    source.write_text(json.dumps(manifest))
    host_targets = []
    def validate(build, architecture="arm64"):
        host_targets.append(architecture)
        return "/approved/clang", "/approved/SDK"
    monkeypatch.setattr(builder, "_validate_host", validate)
    def download(url, destination):
        with tarfile.open(destination, "w") as archive:
            names = ["perl/Artistic", "perl/Copying"] if "perl-" in url else [
                "exiftool/exiftool", "exiftool/README", "exiftool/lib/ExifTool.pm"]
            for name in names:
                item = tarfile.TarInfo(name)
                item.size = 4
                archive.addfile(item, io.BytesIO(b"test"))
    monkeypatch.setattr(builder, "_download", download)
    monkeypatch.setattr(builder, "verify_archive", lambda *args: None)
    launches = []
    def run(command, *, cwd, environment):
        launches.append(command)
        if "Configure" in command:
            prefix = Path(next(arg.removeprefix("-Dprefix=") for arg in command if arg.startswith("-Dprefix=")))
            (prefix / "bin").mkdir(parents=True)
            (prefix / "bin/perl").write_bytes(b"synthetic native perl")
            (prefix / "lib/5.44.0/darwin-thread-multi-2level").mkdir(parents=True)
    monkeypatch.setattr(builder, "_run", run)
    result = build_runtime(source, tmp_path / "runtime")
    assert host_targets == ["x86_64"]
    assert len(launches) == 6
    assert all(command[:2] == ["/usr/bin/arch", "-x86_64"] for command in launches)
    assert any(command[2:] == ["/usr/bin/make", "test"] for command in launches)
    generated = json.loads(result.read_text())
    assert set(generated) == {"schema_version", "versions", "command_prefix", "files", "source_manifest_sha256"}


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
    for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "LD", "ARCHFLAGS"):
        monkeypatch.setenv(key, "untrusted -mmacosx-version-min=26.0")

    environment = _clean_build_environment("/verified/MacOSX.sdk", "12.0")

    assert environment["MACOSX_DEPLOYMENT_TARGET"] == "12.0"
    assert [key for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "LD", "ARCHFLAGS")
            if key in environment] == []


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_inherited_make_overrides_cannot_raise_link_deployment_target(monkeypatch):
    monkeypatch.setenv("MAKEFLAGS", "LDDLFLAGS=-mmacosx-version-min=26.0")
    monkeypatch.setenv("MFLAGS", "")
    # Unlike dictionary-only tests, this invokes the real toolchain launcher:
    # full-Xcode runners reject a fabricated SDK even for a make printf rule.
    sdkroot = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() if sys.platform == "darwin" else "/"
    environment = _clean_build_environment(sdkroot, "12.0")

    result = subprocess.run(
        [shutil.which("make"), "-f", "-", "print"],
        input="LDDLFLAGS = -mmacosx-version-min=12.0\nprint:\n\t@printf '%s' '$(LDDLFLAGS)'\n",
        env=environment, text=True, capture_output=True, check=True,
    )

    assert result.stdout == "-mmacosx-version-min=12.0"


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
