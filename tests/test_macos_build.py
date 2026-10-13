"""Contracts for assembling an isolated Apple Silicon application bundle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import build_macos


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
    process = subprocess.run(
        [sys.executable, "-S", str(build_macos.ROOT / "scripts/build_macos.py"),
         "--runtime", str(runtime), "--output", str(tmp_path / "app")],
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
