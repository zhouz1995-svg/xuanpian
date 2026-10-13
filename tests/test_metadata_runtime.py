from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.xuanpian.engine.metadata_runtime import (
    RuntimeUnavailable,
    resolve_metadata_runtime,
)


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "bundle with 空格" / "metadata-runtime"
    contents = {
        "perl/bin/perl": b"perl executable",
        "perl/lib/5.44.0/darwin-2level/Config.pm": b"arch module",
        "perl/lib/5.44.0/Module.pm": b"module",
        "exiftool/lib/Image/ExifTool.pm": b"exif module",
        "exiftool/exiftool": b"exif script",
    }
    for name, data in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (root / "perl/bin/perl").chmod(0o755)
    manifest = {
        "schema_version": 1,
        "versions": {"perl": "5.44.0", "exiftool": "13.59"},
        "command_prefix": {
            "perl": "perl/bin/perl",
            "module_dirs": ["perl/lib/5.44.0/darwin-2level", "perl/lib/5.44.0", "exiftool/lib"],
            "script": "exiftool/exiftool",
            "fixed_args": ["-config", ""],
        },
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
        "source_manifest_sha256": "a" * 64,
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root.parent, manifest_path


def _edit_manifest(path: Path, change: object) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_bundle_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: pytest.fail("which called in frozen mode"))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=tmp_path, development_executable="/fake/exiftool")
    assert exc.value.reason == "metadata_runtime_missing"


def test_valid_bundle_uses_single_path_arguments_and_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_root, _ = _bundle(tmp_path)
    for name in (
        "PERL5LIB", "PERLLIB", "PERL5OPT", "PERL_LOCAL_LIB_ROOT", "PERL_MB_OPT", "PERL_MM_OPT",
    ):
        monkeypatch.setenv(name, "/malicious")
    monkeypatch.setenv("PATH", "/developer/bin")
    monkeypatch.setenv("TMPDIR", "/tmp/runtime-test")
    runtime = resolve_metadata_runtime(bundle_root=bundle_root, development_executable="/fake")
    root = bundle_root / "metadata-runtime"
    assert runtime.command_prefix == (
        str(root / "perl/bin/perl"), "-I", str(root / "perl/lib/5.44.0/darwin-2level"),
        "-I", str(root / "perl/lib/5.44.0"), "-I", str(root / "exiftool/lib"), str(root / "exiftool/exiftool"),
        "-config", "",
    )
    environment = dict(runtime.environment)
    assert environment["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin"
    assert environment["TMPDIR"] == "/tmp/runtime-test"
    assert not any(name in environment for name in (
        "PERL5LIB", "PERLLIB", "PERL5OPT", "PERL_LOCAL_LIB_ROOT", "PERL_MB_OPT", "PERL_MM_OPT",
    ))
    assert runtime.identity


def test_corrupt_payload_is_rejected_even_after_prior_validation(tmp_path: Path) -> None:
    bundle_root, _ = _bundle(tmp_path)
    resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    (bundle_root / "metadata-runtime/exiftool/lib/Image/ExifTool.pm").write_bytes(b"corrupt")
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_corrupt"


@pytest.mark.parametrize("unsafe", ["../outside", "/tmp/outside", "perl/../outside"])
def test_manifest_command_paths_cannot_escape(tmp_path: Path, unsafe: str) -> None:
    bundle_root, manifest = _bundle(tmp_path)
    _edit_manifest(manifest, lambda data: data["command_prefix"].update(perl=unsafe))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_symlink_payload_cannot_escape_root(tmp_path: Path) -> None:
    bundle_root, _ = _bundle(tmp_path)
    external = tmp_path / "outside.pm"
    external.write_bytes(b"module")
    payload = bundle_root / "metadata-runtime/perl/lib/5.44.0/Module.pm"
    payload.unlink()
    payload.symlink_to(external)
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_unlisted_module_cannot_be_loaded_from_verified_directory(tmp_path: Path) -> None:
    bundle_root, _ = _bundle(tmp_path)
    (bundle_root / "metadata-runtime/exiftool/lib/Surprise.pm").write_bytes(b"unlisted module")
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_manifest_payload_name_cannot_traverse(tmp_path: Path) -> None:
    bundle_root, manifest = _bundle(tmp_path)
    _edit_manifest(manifest, lambda data: data["files"].update({"../outside": "a" * 64}))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_cache_does_not_trust_same_size_replacement_with_restored_mtime(tmp_path: Path) -> None:
    bundle_root, _ = _bundle(tmp_path)
    target = bundle_root / "metadata-runtime/exiftool/exiftool"
    resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    old = target.stat()
    target.write_bytes(b"evil script")
    os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_corrupt"


def test_manifest_rejects_arbitrary_switches_and_unlisted_command_files(tmp_path: Path) -> None:
    bundle_root, manifest = _bundle(tmp_path)
    _edit_manifest(manifest, lambda data: data["command_prefix"].update(fixed_args=["-config", "/evil"]))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_manifest_rejects_unexpected_module_search_directories(tmp_path: Path) -> None:
    bundle_root, manifest = _bundle(tmp_path)
    _edit_manifest(manifest, lambda data: data["command_prefix"].update(module_dirs=["perl/bin"]))
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_nonexecutable_perl_is_rejected(tmp_path: Path) -> None:
    bundle_root, _ = _bundle(tmp_path)
    (bundle_root / "metadata-runtime/perl/bin/perl").chmod(0o644)
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=bundle_root, development_executable=None)
    assert exc.value.reason == "metadata_runtime_invalid"


def test_development_mode_uses_explicit_system_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/exiftool" if name == "exiftool" else None)
    runtime = resolve_metadata_runtime(bundle_root=None, development_executable="exiftool")
    assert runtime.command_prefix == ("/usr/local/bin/exiftool", "-config", "")
    assert runtime.identity


def test_development_missing_tool_has_distinct_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeUnavailable) as exc:
        resolve_metadata_runtime(bundle_root=None, development_executable="exiftool")
    assert exc.value.reason == "exiftool_unavailable"
