"""DMG creation refuses unsafe targets and stages a relocatable app."""

from pathlib import Path
import os
import subprocess
import sys

import pytest

from scripts import build_dmg
from scripts import verify_macos_bundle as gate


def test_intel_dmg_rejects_universal_main_before_staging(tmp_path, monkeypatch):
    app = tmp_path / "选片APP.app"
    binary = app / "Contents/MacOS/选片APP"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
                        gate.NativeInfo((), (), "arm64 x86_64", "12.0"))
    monkeypatch.setattr(build_dmg, "inspect_bundle", lambda path, *, architecture="arm64":
                        gate._inspect_native_dependencies(path, binary, [binary], architecture=architecture))
    monkeypatch.setattr(build_dmg.subprocess, "run",
                        lambda *args, **kwargs: pytest.fail("must not stage a universal main executable"))
    output = tmp_path / "Intel.dmg"
    with pytest.raises(ValueError, match="failed bundle inspection"):
        build_dmg.build_dmg(app, output, architecture="x86_64")
    assert not output.exists()


def test_intel_dmg_passes_requested_architecture_to_inspection(tmp_path, monkeypatch):
    app = tmp_path / "选片APP.app"
    app.mkdir()
    def inspect(path, *, architecture="arm64"):
        assert architecture == "x86_64"
        return [{"kind": "architecture", "detail": "synthetic rejected Intel input"}]
    monkeypatch.setattr(build_dmg, "inspect_bundle", inspect)
    with pytest.raises(ValueError, match="failed bundle inspection"):
        build_dmg.build_dmg(app, tmp_path / "intel.dmg", architecture="x86_64")
    assert not (tmp_path / "intel.dmg").exists()


def test_intel_dmg_preserves_existing_output(tmp_path):
    app = tmp_path / "选片APP.app"
    app.mkdir()
    output = tmp_path / "intel.dmg"
    output.write_bytes(b"existing image")
    with pytest.raises(FileExistsError):
        build_dmg.build_dmg(app, output, architecture="x86_64")
    assert output.read_bytes() == b"existing image"


def test_dmg_rejects_unsupported_architecture(tmp_path):
    with pytest.raises(ValueError, match="architecture"):
        build_dmg.build_dmg(tmp_path / "missing.app", tmp_path / "intel.dmg", architecture="universal2")


def test_dmg_cli_passes_requested_architecture(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["build_dmg", "--app", str(tmp_path / "candidate.app"),
                                     "--output", str(tmp_path / "intel.dmg"), "--architecture", "x86_64"])
    def build(app, output, *, architecture="arm64"):
        assert architecture == "x86_64"
        return output
    monkeypatch.setattr(build_dmg, "build_dmg", build)
    assert build_dmg.main() == 0


def test_dmg_requires_app(tmp_path):
    with pytest.raises(ValueError, match="app"):
        build_dmg.build_dmg(tmp_path / "missing.app", tmp_path / "candidate.dmg")
    assert not (tmp_path / "candidate.dmg").exists()


def test_direct_script_invocation_reports_missing_app(tmp_path):
    process = subprocess.run(
        [sys.executable, "-S", str(Path(build_dmg.__file__)), "--app",
         str(tmp_path / "missing.app"), "--output", str(tmp_path / "candidate.dmg")],
        cwd=tmp_path, capture_output=True, text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert process.returncode != 0
    assert "app must be" in process.stderr


def test_dmg_rejects_dangling_symlink_output(tmp_path):
    app = tmp_path / "选片APP.app"
    app.mkdir()
    output = tmp_path / "candidate.dmg"
    output.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        build_dmg.build_dmg(app, output)


def test_dmg_stages_app_and_writes_digest(tmp_path, monkeypatch):
    app = tmp_path / "选片APP.app"
    app.mkdir()
    executable = app / "Contents/MacOS/选片APP"
    executable.parent.mkdir(parents=True)
    executable.write_text("app", encoding="utf-8")
    executable.chmod(0o755)
    (app / "link").symlink_to("Contents/MacOS/选片APP")
    output = tmp_path / "选片APP-candidate.dmg"
    monkeypatch.setattr(build_dmg, "inspect_bundle", lambda _app, architecture="arm64": [])

    def fake_run(argv, **_kwargs):
        if argv[0] == "/usr/bin/ditto":
            assert argv[1:4] == ["--rsrc", "--extattr", "--acl"]
            import shutil
            shutil.copytree(argv[-2], argv[-1], symlinks=True)
        elif argv[:2] == ["/usr/bin/hdiutil", "create"]:
            staged = Path(argv[argv.index("-srcfolder") + 1])
            assert (staged / "选片APP.app/link").is_symlink()
            assert (staged / "选片APP.app/Contents/MacOS/选片APP").stat().st_mode & 0o111
            assert (staged / "Applications").is_symlink()
            Path(argv[-1]).write_bytes(b"dmg bytes")
        else:
            pytest.fail(f"unexpected command {argv}")
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(build_dmg.subprocess, "run", fake_run)
    assert build_dmg.build_dmg(app, output) == output
    assert output.with_suffix(".dmg.sha256").read_text().startswith(
        "cef1330f355ae293587f452d6dd2c1dcc461b33fb4b60180e7d43ee03064c872")
    assert "选片APP-candidate.dmg" in output.with_suffix(".dmg.sha256").read_text(encoding="utf-8")


def _publication_paths(tmp_path):
    candidate = tmp_path / "candidate.dmg"
    staged_sidecar = tmp_path / "candidate.dmg.sha256"
    candidate.write_bytes(b"new image")
    staged_sidecar.write_bytes(b"new digest")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    return candidate, staged_sidecar, output_dir / "release.dmg", output_dir / "release.dmg.sha256"


def test_publication_never_replaces_existing_pair(tmp_path):
    candidate, staged_sidecar, output, sidecar = _publication_paths(tmp_path)
    output.write_bytes(b"existing image")
    sidecar.write_bytes(b"existing digest")
    with pytest.raises(FileExistsError):
        build_dmg._publish_pair(candidate, staged_sidecar, output, sidecar)
    assert output.read_bytes() == b"existing image"
    assert sidecar.read_bytes() == b"existing digest"


def test_dmg_race_collision_preserves_competitor(tmp_path, monkeypatch):
    candidate, staged_sidecar, output, sidecar = _publication_paths(tmp_path)
    real_link = os.link

    def collide(source, destination):
        if Path(destination) == output:
            output.write_bytes(b"competitor image")
            raise FileExistsError("racing image")
        return real_link(source, destination)

    monkeypatch.setattr(build_dmg.os, "link", collide)
    with pytest.raises(FileExistsError):
        build_dmg._publish_pair(candidate, staged_sidecar, output, sidecar)
    assert output.read_bytes() == b"competitor image"
    assert not sidecar.exists()


def test_sidecar_race_rolls_back_only_own_image(tmp_path, monkeypatch):
    candidate, staged_sidecar, output, sidecar = _publication_paths(tmp_path)
    real_link = os.link

    def collide(source, destination):
        if Path(destination) == sidecar:
            sidecar.write_bytes(b"competitor digest")
            raise FileExistsError("racing sidecar")
        return real_link(source, destination)

    monkeypatch.setattr(build_dmg.os, "link", collide)
    with pytest.raises(FileExistsError):
        build_dmg._publish_pair(candidate, staged_sidecar, output, sidecar)
    assert not output.exists()
    assert sidecar.read_bytes() == b"competitor digest"


def test_sidecar_race_does_not_remove_replaced_image(tmp_path, monkeypatch):
    candidate, staged_sidecar, output, sidecar = _publication_paths(tmp_path)
    real_link = os.link

    def collide(source, destination):
        if Path(destination) == sidecar:
            output.unlink()
            output.write_bytes(b"replacement image")
            sidecar.write_bytes(b"competitor digest")
            raise FileExistsError("racing sidecar")
        return real_link(source, destination)

    monkeypatch.setattr(build_dmg.os, "link", collide)
    with pytest.raises(FileExistsError):
        build_dmg._publish_pair(candidate, staged_sidecar, output, sidecar)
    assert output.read_bytes() == b"replacement image"
    assert sidecar.read_bytes() == b"competitor digest"
