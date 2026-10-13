"""Release gate regressions for a relocatable arm64 bundle."""

from pathlib import Path
import json
import subprocess

import pytest

from scripts import verify_macos_bundle as gate


def _native(tmp_path: Path, dependency: str, *, rpath: str = "") -> tuple[Path, Path]:
    app = tmp_path / "选片APP.app"
    binary = app / "Contents/MacOS/选片APP"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"0" * 32)
    return app, binary


@pytest.mark.parametrize("dependency", [
    "/opt/homebrew/lib/libbad.dylib",
    "/Users/synthetic-test-user/private/libbad.dylib",
])
def test_private_native_dependency_is_violation(tmp_path, monkeypatch, dependency):
    app, binary = _native(tmp_path, dependency)
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        (dependency,), (), "arm64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "dependency" and dependency in item["detail"] for item in violations)


def test_unresolved_rpath_is_violation(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "@rpath/libmissing.dylib")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        ("@rpath/libmissing.dylib",), ("@executable_path/../Frameworks",), "arm64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "dependency" and "@rpath" in item["detail"] for item in violations)


def test_inherited_rpath_is_resolved_from_declaring_loader(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    plugin = app / "Contents/Frameworks/plugins/plugin.so"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"\xcf\xfa\xed\xfe")
    library = app / "Contents/Frameworks/libok.dylib"
    library.write_bytes(b"\xcf\xfa\xed\xfe")
    leaf = app / "Contents/Frameworks/libleaf.dylib"
    leaf.write_bytes(b"\xcf\xfa\xed\xfe")
    infos = {
        binary: gate.NativeInfo(("@loader_path/../Frameworks/plugins/plugin.so",), (), "arm64", "11.0"),
        plugin: gate.NativeInfo(("@rpath/libok.dylib",), ("@loader_path/..",), "arm64", "11.0"),
        library: gate.NativeInfo(("@rpath/libleaf.dylib",), (), "arm64", "11.0"),
        leaf: gate.NativeInfo((), (), "arm64", "11.0"),
    }
    monkeypatch.setattr(gate, "_native_metadata", infos.__getitem__)
    assert gate._inspect_native_dependencies(app, binary, list(infos)) == []


@pytest.mark.parametrize("runpaths,should_fail", [
    (("/opt/homebrew/lib", "@executable_path/../Frameworks"), True),
    (("@executable_path/../Frameworks", "/opt/homebrew/lib"), False),
    (("@executable_path/../Frameworks", "/Users/synthetic-test-user/unused-debug"), False),
])
def test_ordered_rpath_rejects_external_shadow_only_before_bundle(
    tmp_path, monkeypatch, runpaths, should_fail,
):
    app, binary = _native(tmp_path, "")
    library = app / "Contents/Frameworks/libok.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe")
    infos = {
        binary: gate.NativeInfo(("@rpath/libok.dylib",), runpaths, "arm64", "11.0"),
        library: gate.NativeInfo((), (), "arm64", "11.0"),
    }
    monkeypatch.setattr(gate, "_native_metadata", infos.__getitem__)
    violations = gate._inspect_native_dependencies(app, binary, list(infos))
    assert any(item["kind"] == "dependency" for item in violations) is should_fail


def test_system_rpath_can_resolve_shared_cache_dependency(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        ("@rpath/libSystem.B.dylib",), ("/usr/lib",), "arm64", "11.0"))
    assert gate._inspect_native_dependencies(app, binary, [binary]) == []


@pytest.mark.parametrize("dependency,runpaths", [
    ("/usr/lib/../../opt/homebrew/lib/libbad.dylib", ()),
    ("@rpath/libbad.dylib", ("/usr/lib/../../opt/homebrew/lib",)),
    ("@rpath/../../opt/homebrew/lib/libbad.dylib", ("/usr/lib",)),
])
def test_system_prefix_traversal_cannot_authorize_private_dependency(
    tmp_path, monkeypatch, dependency, runpaths,
):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        (dependency,), runpaths, "arm64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "dependency" for item in violations)


def test_system_prefix_traversal_cannot_authorize_script_interpreter(tmp_path):
    app, _ = _native(tmp_path, "")
    script = app / "Contents/Resources/tool"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/lib/../../opt/homebrew/bin/python3\n", encoding="utf-8")
    script.chmod(0o755)
    assert any(item["kind"] == "script" for item in gate._inspect_scripts(app))


def test_system_prefix_traversal_cannot_authorize_perl_inc(tmp_path, monkeypatch):
    app, _ = _native(tmp_path, "")
    perl = app / "Contents/Resources/metadata-runtime/perl/bin/perl"
    perl.parent.mkdir(parents=True)
    perl.write_bytes(b"\xcf\xfa\xed\xfe")
    perl.chmod(0o755)
    monkeypatch.setattr(gate.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "/usr/lib/../../opt/homebrew/lib/perl5\n", ""))
    assert any(item["kind"] == "perl_module_path" for item in gate._inspect_perl_paths(app, perl))


def test_absolute_rpath_into_current_bundle_is_nonrelocatable(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    library = app / "Contents/Frameworks/libok.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe")
    infos = {
        binary: gate.NativeInfo(("@rpath/libok.dylib",), (str(library.parent),), "arm64", "11.0"),
        library: gate.NativeInfo((), (), "arm64", "11.0"),
    }
    monkeypatch.setattr(gate, "_native_metadata", infos.__getitem__)
    violations = gate._inspect_native_dependencies(app, binary, list(infos))
    assert any(item["kind"] == "dependency" and "nonrelocatable" in item["detail"]
               for item in violations)


@pytest.mark.parametrize("dependency", [
    "@loader_path/../Frameworks/libok.dylib",
    "/System/Library/Frameworks/AppKit.framework/AppKit",
    "/usr/lib/libSystem.B.dylib",
])
def test_in_bundle_and_system_dependencies_allowed(tmp_path, monkeypatch, dependency):
    app, binary = _native(tmp_path, dependency)
    library = app / "Contents/Frameworks/libok.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\xcf\xfa\xed\xfe" + b"0" * 32)
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        (dependency,) if _path == binary else (), (), "arm64", "11.0"))
    assert gate._inspect_native_dependencies(app, binary, [binary, library]) == []


def test_missing_arm64_slice_is_violation(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path: gate.NativeInfo(
        (), (), "x86_64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "architecture" for item in violations)


def test_otool_parses_load_commands_and_minos(monkeypatch, tmp_path):
    binary = tmp_path / "a.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    def fake_run(argv, **_kwargs):
        if argv[1:] == ["-L", str(binary)]:
            output = f"{binary}:\n\t@rpath/libx.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        elif argv[1:] == ["-l", str(binary)]:
            output = "cmd LC_RPATH\ncmdsize 40\npath @loader_path/../Frameworks (offset 12)\ncmd LC_BUILD_VERSION\ncmdsize 32\nplatform 1\nminos 26.0\nsdk 26.5\n"
        elif argv[1:] == ["-archs", str(binary)]:
            output = "arm64 x86_64\n"
        else:
            pytest.fail(f"unexpected argv {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    assert gate._native_metadata(binary) == gate.NativeInfo(
        ("@rpath/libx.dylib",), ("@loader_path/../Frameworks",), "arm64 x86_64", "26.0")


def test_non_system_script_interpreter_is_violation(tmp_path):
    app, _ = _native(tmp_path, "")
    script = app / "Contents/Resources/tool"
    script.parent.mkdir(parents=True)
    script.write_text("#!/opt/homebrew/bin/python3\n", encoding="utf-8")
    script.chmod(0o755)
    violations = gate._inspect_scripts(app)
    assert any(item["kind"] == "script" and "homebrew" in item["detail"] for item in violations)


def test_perl_module_path_outside_bundle_is_violation(tmp_path, monkeypatch):
    app, _ = _native(tmp_path, "")
    perl = app / "Contents/Resources/metadata-runtime/perl/bin/perl"
    perl.parent.mkdir(parents=True)
    perl.write_bytes(b"\xcf\xfa\xed\xfe")
    perl.chmod(0o755)
    monkeypatch.setattr(gate.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, "/opt/homebrew/lib/perl5\n", ""))
    violations = gate._inspect_perl_paths(app, perl)
    assert any(item["kind"] == "perl_module_path" for item in violations)


def test_model_manifest_cannot_replace_expected_models_with_empty_list(tmp_path):
    app, _ = _native(tmp_path, "")
    models = app / gate.MODELS
    models.mkdir(parents=True)
    (models / "manifest.json").write_text(json.dumps({"schema_version": 1, "models": {}}))
    assert any(item["kind"] == "model_manifest" for item in gate._inspect_models(app))
