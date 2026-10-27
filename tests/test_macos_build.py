"""Contracts for assembling an isolated Apple Silicon application bundle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import hashlib
import runpy
import types
import plistlib

import pytest

from scripts import build_macos
from scripts import build_metadata_runtime


@pytest.mark.parametrize("architecture", ["arm64", "x86_64"])
def test_profile_selectors_choose_matching_lock_and_sources(tmp_path, monkeypatch, architecture):
    monkeypatch.setattr(build_macos, "ROOT", tmp_path)
    monkeypatch.setattr(build_macos, "LOCK", tmp_path / "legacy-arm.lock")
    if architecture == "arm64":
        assert build_macos.macos_lock(architecture) == tmp_path / "legacy-arm.lock"
        assert build_macos.macos_runtime_sources(architecture) == tmp_path / "packaging/runtime-sources.json"
    else:
        assert build_macos.macos_lock(architecture) == tmp_path / "packaging/requirements-macos-x86_64.lock"
        assert build_macos.macos_runtime_sources(architecture) == tmp_path / "packaging/runtime-sources-macos-x86_64.json"


@pytest.mark.parametrize("architecture", ["universal2", "amd64", ""])
def test_unknown_build_profile_fails_before_output(tmp_path, architecture):
    with pytest.raises(ValueError, match="architecture"):
        build_macos.build_app(runtime_root=tmp_path / "missing", output=tmp_path / "out", architecture=architecture)
    for select in (build_macos.macos_lock, build_macos.macos_runtime_sources):
        with pytest.raises(ValueError, match="architecture"):
            select(architecture)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("host", ["arm64", "amd64"])
def test_intel_build_rejects_foreign_host(tmp_path, monkeypatch, host):
    runtime = _runtime(tmp_path / "runtime")
    monkeypatch.setattr(build_macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: host)
    with pytest.raises(RuntimeError, match="x86_64"):
        build_macos.build_app(runtime_root=runtime, output=tmp_path / "app", architecture="x86_64")


@pytest.mark.parametrize("query_error", [False, True])
def test_intel_app_rejects_rosetta_before_runtime_staging(tmp_path, monkeypatch, query_error):
    runtime = _runtime(tmp_path / "runtime")
    monkeypatch.setattr(build_macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: "x86_64")
    def translated():
        if query_error:
            raise OSError("translation query failed")
        return True
    monkeypatch.setattr(build_metadata_runtime, "_is_translated_process", translated, raising=False)
    monkeypatch.setattr(build_macos, "_verify_input_runtime",
                        lambda root: pytest.fail("must reject translated host before staging runtime"))
    with pytest.raises(OSError if query_error else RuntimeError, match="translation|Rosetta|translated"):
        build_macos.build_app(runtime_root=runtime, output=tmp_path / "app", architecture="x86_64")
    assert not (tmp_path / "app").exists()


@pytest.mark.parametrize("existing", ["directory", "symlink"])
def test_intel_existing_output_is_preserved(tmp_path, existing):
    output = tmp_path / "app"
    if existing == "directory":
        output.mkdir()
    else:
        output.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        build_macos.build_app(runtime_root=tmp_path / "unused", output=output, architecture="x86_64")
    assert output.is_dir() if existing == "directory" else output.is_symlink()


@pytest.mark.parametrize("matching", [True, False])
def test_intel_preflight_requires_selected_runtime_profile(tmp_path, monkeypatch, matching):
    runtime = _runtime(tmp_path / "runtime")
    source = tmp_path / "packaging/runtime-sources-macos-x86_64.json"
    source.parent.mkdir()
    source.write_bytes(b"approved Intel source manifest")
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_manifest_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest() if matching else "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(build_macos, "ROOT", tmp_path)
    monkeypatch.setattr(build_macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(build_metadata_runtime, "_is_translated_process", lambda: False, raising=False)
    monkeypatch.setattr(build_macos, "_verify_input_runtime", lambda root: None)
    if matching:
        build_macos._preflight(runtime, tmp_path / "app", architecture="x86_64")
    else:
        with pytest.raises(ValueError, match="source.*manifest"):
            build_macos._preflight(runtime, tmp_path / "app", architecture="x86_64")


def test_intel_provenance_uses_selected_lock(tmp_path, monkeypatch):
    app = tmp_path / "选片APP.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    runtime = _runtime(resources / "metadata-runtime")
    lock = tmp_path / "packaging/requirements-macos-x86_64.lock"
    lock.parent.mkdir()
    lock.write_bytes(b"approved Intel dependency lock")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    monkeypatch.setattr(build_macos, "ROOT", tmp_path)
    def run(argv, **kwargs):
        result = "a" * 40 if argv[1:3] == ["rev-parse", "HEAD"] else ""
        if argv[0] == "/usr/bin/xcrun":
            result = "15.5"
        return subprocess.CompletedProcess(argv, 0, result, "")
    monkeypatch.setattr(build_macos.subprocess, "run", run)
    build_macos._provenance(app, runtime, architecture="x86_64")
    data = json.loads((resources / "build-provenance.json").read_text())
    assert data["architecture"] == "x86_64"
    assert data["requirements_lock"] == "packaging/requirements-macos-x86_64.lock"
    assert data["requirements_lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()


def test_intel_app_build_publishes_selected_profile_provenance(tmp_path, monkeypatch):
    from src.xuanpian.engine import metadata_runtime
    runtime = _runtime(tmp_path / "runtime")
    packaging = tmp_path / "source/packaging"
    packaging.mkdir(parents=True)
    (packaging.parent / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (packaging / "requirements-macos-x86_64.lock").write_bytes(b"Intel lock")
    source = packaging / "runtime-sources-macos-x86_64.json"
    source.write_bytes(b"Intel sources")
    manifest_path = runtime / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["source_manifest_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(build_macos, "ROOT", packaging.parent)
    monkeypatch.setattr(build_macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(build_metadata_runtime, "_is_translated_process", lambda: False, raising=False)
    monkeypatch.setattr(build_macos, "_verify_input_runtime", lambda root: None)
    monkeypatch.setattr(metadata_runtime, "resolve_metadata_runtime", lambda **kwargs: None)
    def run(command):
        if "PyInstaller" in command:
            app = Path(command[command.index("--distpath") + 1]) / build_macos.APP_NAME
            (app / "Contents/Resources").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.1.0"}))
        else:
            assert command[0] == "/usr/bin/codesign"
    monkeypatch.setattr(build_macos, "_run", run)
    def provenance_run(command, **kwargs):
        output = "15.5" if command[0] == "/usr/bin/xcrun" else (
            "a" * 40 if command[1:3] == ["rev-parse", "HEAD"] else "")
        return subprocess.CompletedProcess(command, 0, output, "")
    monkeypatch.setattr(build_macos.subprocess, "run", provenance_run)
    output = tmp_path / "delivery"
    app = build_macos.build_app(runtime_root=runtime, output=output, architecture="x86_64")
    data = json.loads((app / "Contents/Resources/build-provenance.json").read_text())
    assert app == output / build_macos.APP_NAME
    assert data["architecture"] == "x86_64"
    assert data["requirements_lock"] == "packaging/requirements-macos-x86_64.lock"


def test_app_cli_passes_requested_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build_macos", "--runtime", str(tmp_path / "runtime"),
                                     "--output", str(tmp_path / "out"), "--architecture", "x86_64"])
    def build(*, runtime_root, output, architecture="arm64"):
        assert architecture == "x86_64"
        return output / "candidate.app"
    monkeypatch.setattr(build_macos, "build_app", build)
    assert build_macos.main() == 0


@pytest.mark.parametrize("host", ["arm64", "x86_64"])
def test_pyinstaller_spec_targets_native_host_not_environment(monkeypatch, host):
    monkeypatch.setattr(build_macos.platform, "machine", lambda: host)
    monkeypatch.setenv("PYINSTALLER_TARGET_ARCH", "universal2")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_dynamic_libs = lambda *args, **kwargs: []
    hooks.collect_submodules = lambda *args: []
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    class Analysis:
        def __init__(self, *args, **kwargs):
            self.pure = self.scripts = self.binaries = self.datas = []
    values = runpy.run_path(str(build_macos.SPEC), init_globals={
        "SPECPATH": str(build_macos.SPEC.parent), "Analysis": Analysis,
        "PYZ": lambda *args: None, "EXE": lambda *args, **kwargs: kwargs,
        "COLLECT": lambda *args, **kwargs: None, "BUNDLE": lambda *args, **kwargs: None,
    })
    assert values["exe"]["target_arch"] == host


def _runtime(root: Path) -> Path:
    root.mkdir()
    (root / "perl/bin").mkdir(parents=True)
    (root / "perl/bin/perl").write_bytes(b"perl")
    (root / "exiftool").mkdir()
    (root / "exiftool/exiftool").write_bytes(b"exiftool")
    (root / "licenses").mkdir()
    (root / "licenses/notice").write_text("license", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "versions": {"perl": "5.44.0", "exiftool": "13.59"},
        "command_prefix": {
            "perl": "perl/bin/perl",
            "module_dirs": ["perl/lib/5.44.0/darwin-arm64", "perl/lib/5.44.0", "exiftool/lib"],
            "script": "exiftool/exiftool", "fixed_args": ["-config", ""],
        },
        "files": {}, "source_manifest_sha256": "0" * 64,
    }), encoding="utf-8")
    return root


def test_missing_runtime_blocks_build(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="runtime"):
        build_macos.build_app(runtime_root=tmp_path / "missing", output=tmp_path / "app")
    assert not (tmp_path / "app").exists()


def test_non_arm64_blocks_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path / "runtime")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: "x86_64")
    with pytest.raises(RuntimeError, match="arm64"):
        build_macos.build_app(runtime_root=runtime, output=tmp_path / "app")


@pytest.mark.parametrize("existing", ["directory", "symlink"])
def test_existing_output_blocks_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str,
) -> None:
    runtime = _runtime(tmp_path / "runtime")
    output = tmp_path / "app"
    if existing == "directory":
        output.mkdir()
    else:
        output.symlink_to(tmp_path / "missing-target")
    monkeypatch.setattr(build_macos.platform, "machine", lambda: "arm64")
    with pytest.raises(FileExistsError):
        build_macos.build_app(runtime_root=runtime, output=output)


def test_project_version_is_authoritative() -> None:
    assert build_macos.project_version() == "0.1.0"


def test_final_manifest_hashes_actual_bundle_payload(tmp_path: Path) -> None:
    source = _runtime(tmp_path / "source")
    final = tmp_path / "bundle" / "metadata-runtime"
    build_macos.stage_runtime(source, final)
    (final / "perl/bin/perl").write_bytes(b"rewritten native payload")
    build_macos.refresh_runtime_manifest(final)
    manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["perl/bin/perl"] == (
        "ca5bfd9d0c22029d4f1f11ec034a74e843fc1e6313090017e83a998739e606bf"
    )
    assert manifest["source_manifest_sha256"] == "0" * 64
    assert "licenses/notice" in manifest["files"]


def test_runtime_symlink_is_rejected_before_staging(tmp_path: Path) -> None:
    source = _runtime(tmp_path / "source")
    (source / "extra").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="symlink"):
        build_macos.stage_runtime(source, tmp_path / "destination")


def test_app_stage_omits_ancillary_perl_tools_without_mutating_source(tmp_path: Path) -> None:
    source = _runtime(tmp_path / "source")
    extra = source / "perl/bin/cpan"
    extra.write_text("#!/old/build/path/perl\n", encoding="utf-8")
    extra.chmod(0o755)
    destination = tmp_path / "bundle" / "metadata-runtime"
    build_macos.stage_runtime(source, destination)
    build_macos.prune_runtime_tools(destination)
    assert (destination / "perl/bin/perl").is_file()
    assert not (destination / "perl/bin/cpan").exists()
    assert extra.is_file()


def test_direct_script_invocation_can_import_runtime_validator(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "runtime")
    # Reach runtime validation on either native Mac host, not the foreign-host guard.
    process = subprocess.run(
        [sys.executable, "-S", str(build_macos.ROOT / "scripts/build_macos.py"),
         "--runtime", str(runtime), "--output", str(tmp_path / "app"),
         "--architecture", build_macos.platform.machine()],
        cwd=tmp_path, capture_output=True, text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert process.returncode != 0
    assert "metadata_runtime_invalid" in process.stderr


def test_bundle_runtime_destination_is_resources_not_frameworks(tmp_path: Path) -> None:
    app = tmp_path / "选片APP.app"
    assert build_macos._runtime_destination(app) == (
        app / "Contents/Resources/metadata-runtime"
    )


def test_provenance_discloses_uncommitted_build_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = tmp_path / "选片APP.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    runtime = _runtime(resources / "metadata-runtime")

    # Existing arm64 consumers may replace LOCK with a fixture outside ROOT.
    lock = tmp_path / "arm64-fixture.lock"
    lock.write_bytes(b"arm64 dependency fixture")
    monkeypatch.setattr(build_macos, "LOCK", lock)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["git", "rev-parse", "HEAD"]:
            value = "a" * 40 + "\n"
        elif command == ["git", "status", "--porcelain", "--untracked-files=normal"]:
            value = " M scripts/build_macos.py\n"
        elif command == ["/usr/bin/xcrun", "--show-sdk-version"]:
            value = "26.5\n"
        else:
            pytest.fail(f"unexpected process: {command}")
        return subprocess.CompletedProcess(command, 0, value, "")

    monkeypatch.setattr(build_macos.subprocess, "run", fake_run)
    build_macos._provenance(app, runtime)
    data = json.loads((resources / "build-provenance.json").read_text(encoding="utf-8"))
    assert data["source_revision"] == "a" * 40
    assert data["source_tree_dirty"] is True
    assert data["requirements_lock"] == "packaging/requirements-macos-arm64.lock"
    assert data["requirements_lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
