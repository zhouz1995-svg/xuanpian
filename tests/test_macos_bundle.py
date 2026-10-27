"""Release gate regressions for a relocatable arm64 bundle."""

from pathlib import Path
import json
import subprocess
import hashlib
import plistlib
import sys

import pytest

from scripts import verify_macos_bundle as gate
from scripts import build_macos


@pytest.mark.parametrize("actual,expected,violation", [
    ("x86_64", "x86_64", False), ("arm64", "x86_64", True),
    ("arm64 x86_64", "x86_64", False), ("x86_64", "arm64", True),
])
def test_native_gate_requires_selected_slice(tmp_path, monkeypatch, actual, expected, violation):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
                        gate.NativeInfo((), (), actual, "12.0"))
    issues = gate._inspect_native_dependencies(app, binary, [binary], architecture=expected)
    assert any(item["kind"] == "architecture" for item in issues) is violation


def _identity(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    resources = app / "Contents/Resources"
    runtime = app / gate.RUNTIME
    runtime.mkdir(parents=True)
    packaging = tmp_path / "source/packaging"
    packaging.mkdir(parents=True)
    lock = packaging / "requirements-macos-x86_64.lock"
    source = packaging / "runtime-sources-macos-x86_64.json"
    lock.write_bytes(b"Intel lock fixture")
    source.write_bytes(b"Intel source fixture")
    (packaging.parent / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    runtime_manifest = {"source_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    (runtime / "manifest.json").write_text(json.dumps(runtime_manifest))
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "0.1.0", "LSMinimumSystemVersion": "12.0",
    }))
    provenance = {
        "app_version": "0.1.0", "source_tree_dirty": False, "source_revision": "a" * 40,
        "signing": "local-ad-hoc", "architecture": "x86_64",
        "requirements_lock": "packaging/requirements-macos-x86_64.lock",
        "requirements_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "runtime_source_manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "runtime_final_manifest_sha256": hashlib.sha256((runtime / "manifest.json").read_bytes()).hexdigest(),
    }
    path = resources / "build-provenance.json"
    path.write_text(json.dumps(provenance))
    monkeypatch.setattr(build_macos, "ROOT", packaging.parent)
    monkeypatch.setattr(gate.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, "", "Signature=adhoc\nTeamIdentifier=not set\n"))
    return app, binary, path, provenance


def test_intel_identity_accepts_matching_selected_profile(tmp_path, monkeypatch):
    app, binary, _, _ = _identity(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
                        gate.NativeInfo((), (), "arm64 x86_64", "12.0" if architecture == "x86_64" else "26.0"))
    assert gate._inspect_identity(app, [binary], architecture="x86_64") == []


@pytest.mark.parametrize("field,value", [
    ("architecture", "arm64"),
    ("requirements_lock", "packaging/requirements-macos-arm64.lock"),
    ("requirements_lock_sha256", "0" * 64),
    ("runtime_source_manifest_sha256", "0" * 64),
])
def test_intel_identity_rejects_foreign_profile_provenance(tmp_path, monkeypatch, field, value):
    app, _, path, provenance = _identity(tmp_path, monkeypatch)
    provenance[field] = value
    path.write_text(json.dumps(provenance))
    assert any(item["kind"] == "provenance" for item in
               gate._inspect_identity(app, [], architecture="x86_64"))


def test_intel_identity_rejects_runtime_from_foreign_source_even_with_matching_final_hash(tmp_path, monkeypatch):
    app, _, path, provenance = _identity(tmp_path, monkeypatch)
    manifest = app / gate.RUNTIME / "manifest.json"
    manifest.write_text(json.dumps({"source_manifest_sha256": "0" * 64}))
    provenance["runtime_final_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    path.write_text(json.dumps(provenance))
    assert any(item["kind"] == "provenance" for item in
               gate._inspect_identity(app, [], architecture="x86_64"))


def test_intel_identity_uses_selected_slice_minimum_os(tmp_path, monkeypatch):
    app, binary, _, _ = _identity(tmp_path, monkeypatch)
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
                        gate.NativeInfo((), (), "arm64 x86_64", "13.0" if architecture == "x86_64" else "11.0"))
    assert any(item["kind"] == "minimum_os" and "13.0" in item["detail"] for item in
               gate._inspect_identity(app, [binary], architecture="x86_64"))


def test_otool_selects_requested_slice_for_dependencies_and_minos(tmp_path, monkeypatch):
    binary = tmp_path / "universal.dylib"
    def run(argv, **kwargs):
        if argv == ["/usr/bin/otool", "-arch", "x86_64", "-L", str(binary)]:
            output = f"{binary}:\n\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"
        elif argv == ["/usr/bin/otool", "-arch", "x86_64", "-l", str(binary)]:
            output = "cmd LC_VERSION_MIN_MACOSX\nversion 12.0\n"
        elif argv == ["/usr/bin/lipo", "-archs", str(binary)]:
            output = "arm64 x86_64\n"
        else:
            pytest.fail(f"unexpected tool/slice: {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")
    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate._native_metadata(binary, architecture="x86_64") == gate.NativeInfo(
        ("/usr/lib/libSystem.B.dylib",), (), "arm64 x86_64", "12.0")


def test_inspection_rejects_unsupported_architecture(tmp_path):
    with pytest.raises(ValueError, match="architecture"):
        gate.inspect_bundle(tmp_path / "missing.app", architecture="universal2")


def test_bundle_inspection_propagates_intel_profile_to_native_and_identity_checks(tmp_path, monkeypatch):
    app, binary, _, _ = _identity(tmp_path, monkeypatch)
    binary.chmod(0o755)
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
                        gate.NativeInfo((), (), "x86_64", "12.0" if architecture == "x86_64" else "26.0"))
    monkeypatch.setattr(gate, "resolve_metadata_runtime", lambda **kwargs: None)
    issues = gate.inspect_bundle(app, architecture="x86_64")
    assert not any(item["kind"] in {"architecture", "provenance", "minimum_os", "identity"} for item in issues)


def test_inspection_cli_passes_requested_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["verify_macos_bundle", str(tmp_path / "candidate.app"),
                                     "--architecture", "x86_64"])
    def inspect(path, *, architecture="arm64"):
        assert architecture == "x86_64"
        return []
    monkeypatch.setattr(gate, "inspect_bundle", inspect)
    assert gate.main() == 0


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
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
        (dependency,), (), "arm64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "dependency" and dependency in item["detail"] for item in violations)


def test_unresolved_rpath_is_violation(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "@rpath/libmissing.dylib")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
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
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64": infos[path])
    assert gate._inspect_native_dependencies(app, binary, list(infos)) == []


@pytest.mark.parametrize("external", [None, "/opt/homebrew/lib/libbad.dylib"])
def test_circular_native_loads_terminate_without_skipping_external_edges(
    tmp_path, monkeypatch, external,
):
    app, binary = _native(tmp_path, "")
    first = app / "Contents/Frameworks/libfirst.dylib"
    second = app / "Contents/Frameworks/libsecond.dylib"
    first.parent.mkdir(parents=True)
    for library in (first, second):
        library.write_bytes(b"\xcf\xfa\xed\xfe")
    infos = {
        binary: gate.NativeInfo(("@rpath/libfirst.dylib",),
                               ("@executable_path/../Frameworks",), "arm64", "11.0"),
        first: gate.NativeInfo(("@rpath/libsecond.dylib",), ("@loader_path",), "arm64", "11.0"),
        second: gate.NativeInfo(("@rpath/libfirst.dylib",) + ((external,) if external else ()),
                                ("@loader_path",), "arm64", "11.0"),
    }
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64": infos[path])

    violations = gate._inspect_native_dependencies(app, binary, list(infos))

    if external:
        assert any(item["kind"] == "dependency" and external in item["detail"]
                   for item in violations)
    else:
        assert violations == []


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
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64": infos[path])
    violations = gate._inspect_native_dependencies(app, binary, list(infos))
    assert any(item["kind"] == "dependency" for item in violations) is should_fail


def test_system_rpath_can_resolve_shared_cache_dependency(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
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
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
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
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64": infos[path])
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
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
        (dependency,) if _path == binary else (), (), "arm64", "11.0"))
    assert gate._inspect_native_dependencies(app, binary, [binary, library]) == []


def test_missing_arm64_slice_is_violation(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda _path, architecture="arm64": gate.NativeInfo(
        (), (), "x86_64", "11.0"))
    violations = gate._inspect_native_dependencies(app, binary, [binary])
    assert any(item["kind"] == "architecture" for item in violations)


def test_otool_parses_load_commands_and_minos(monkeypatch, tmp_path):
    binary = tmp_path / "a.dylib"
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    def fake_run(argv, **_kwargs):
        if argv[1:] == ["-arch", "arm64", "-L", str(binary)]:
            output = f"{binary}:\n\t@rpath/libx.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
        elif argv[1:] == ["-arch", "arm64", "-l", str(binary)]:
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
