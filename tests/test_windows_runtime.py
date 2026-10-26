from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.xuanpian.engine import af_metadata, metadata_runtime as runtime


def windows_bundle(tmp_path):
    root = tmp_path / "程序 with spaces" / "_internal" / "metadata-runtime"
    files = {
        "exiftool/exiftool.exe": b"MZ launcher",
        "exiftool/exiftool_files/perl.exe": b"MZ perl",
        "exiftool/exiftool_files/perl532.dll": b"MZ dll",
        "exiftool/exiftool_files/exiftool.pl": b"script",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
    manifest = {
        "schema_version": 2,
        "target": {"platform": "windows", "architecture": "x86_64"},
        "versions": {"exiftool": "13.59"},
        "command_prefix": {"executable": "exiftool/exiftool.exe",
                           "fixed_args": ["-config", "", "-charset", "filename=UTF8"]},
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "source_manifest_sha256": "a" * 64,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest))
    return root.parent, path


def test_windows_manifest_uses_bundled_launcher_and_unicode_arguments(tmp_path):
    root, _ = windows_bundle(tmp_path)
    result = runtime.resolve_metadata_runtime(bundle_root=root, development_executable=None)
    assert result.command_prefix == (
        str(root / "metadata-runtime/exiftool/exiftool.exe"),
        "-config", "", "-charset", "filename=UTF8",
    )


@pytest.mark.parametrize("change", ["corrupt", "extra"])
def test_windows_runtime_rejects_changed_or_unlisted_payload(tmp_path, change):
    root, _ = windows_bundle(tmp_path)
    runtime.resolve_metadata_runtime(bundle_root=root, development_executable=None)
    target = root / "metadata-runtime/exiftool/exiftool_files"
    if change == "corrupt":
        (target / "perl532.dll").write_bytes(b"MZ evil")
    else:
        (target / "unlisted.pm").write_bytes(b"injection")
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime.resolve_metadata_runtime(bundle_root=root, development_executable=None)


@pytest.mark.parametrize("name", ["C:evil", "x/file:stream", "x/NUL.txt", "x/aux", "x/trailing.", "x/a ", "x/../bad"])
def test_windows_manifest_rejects_windows_unsafe_paths(tmp_path, name):
    root, path = windows_bundle(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["files"][name] = "b" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime._validate_manifest(manifest)


def test_windows_environment_keeps_system_root_not_perl_or_user_path(monkeypatch):
    monkeypatch.setattr(runtime, "sys", SimpleNamespace(platform="win32"), raising=False)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PATH", r"D:\injected")
    monkeypatch.setenv("PERL5OPT", "-MInjected")
    monkeypatch.setenv("TEMP", r"C:\Temp")
    values = dict(runtime._environment())
    assert values["SystemRoot"] == r"C:\Windows"
    assert values["PATH"] == r"C:\Windows\System32"
    assert values["TEMP"] == r"C:\Temp"
    assert "PERL5OPT" not in values


def test_windows_frozen_root_must_belong_to_executable(tmp_path, monkeypatch):
    app = tmp_path / "选片 程序"
    internal = app / "_internal"
    internal.mkdir(parents=True)
    exe = app / "XuanPian.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(af_metadata, "sys", SimpleNamespace(platform="win32", executable=str(exe)))
    assert af_metadata._frozen_runtime_root(internal) == internal
    unrelated = tmp_path / "unrelated" / "_internal"
    unrelated.mkdir(parents=True)
    with pytest.raises(runtime.RuntimeUnavailable):
        af_metadata._frozen_runtime_root(unrelated)


def test_windows_af_reader_uses_utf8_and_hides_console(tmp_path, monkeypatch):
    photo = tmp_path / "照片.jpg"
    photo.write_bytes(b"image")
    monkeypatch.setattr(af_metadata, "sys", SimpleNamespace(platform="win32", frozen=False))
    monkeypatch.setattr(af_metadata, "resolve_metadata_runtime", lambda **_: runtime.MetadataRuntime(
        ("XuanPian/exiftool.exe", "-config", "", "-charset", "filename=UTF8"), (), "test-windows",
    ))
    def run(command, **kwargs):
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["creationflags"] == 0x08000000
        assert command[-1] == str(photo.resolve())
        return subprocess.CompletedProcess(command, 0, '[{"FileType":"JPEG"}]', "")
    monkeypatch.setattr(af_metadata.subprocess, "run", run)
    assert af_metadata.read_af_hint(str(photo), (400, 200)).reason != "metadata_read_error"


def test_reparse_attribute_is_a_link_even_when_is_symlink_is_false():
    path = SimpleNamespace(is_symlink=lambda: False,
                           lstat=lambda: SimpleNamespace(st_file_attributes=0x400))
    assert runtime._is_link(path)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junction integration")
def test_windows_runtime_rejects_in_tree_junction(tmp_path):
    root, _ = windows_bundle(tmp_path)
    empty = root / "metadata-runtime/empty"
    empty.mkdir()
    link = root / "metadata-runtime/alias"
    subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(empty)],
                   check=True, capture_output=True)
    with pytest.raises(runtime.RuntimeUnavailable):
        runtime.resolve_metadata_runtime(bundle_root=root, development_executable=None)
