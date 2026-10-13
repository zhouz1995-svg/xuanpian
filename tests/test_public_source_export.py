"""The public candidate is an exact, non-destructive committed-blob export."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts.export_public_source import export_commit


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "public.py").write_text("print('committed')\n", encoding="utf-8")
    (repo / "private-report.md").write_text("private\n", encoding="utf-8")
    _git(repo, "add", "public.py", "private-report.md")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
        check=True,
    )
    return repo


def _allowlist(path: Path, paths: list[str]) -> Path:
    path.write_text(json.dumps({"paths": paths}), encoding="utf-8")
    return path


def test_exports_only_committed_blobs_with_manifest(tmp_path: Path, committed_repo: Path) -> None:
    (committed_repo / "public.py").write_text("print('dirty')\n", encoding="utf-8")
    (committed_repo / "private-report.md").write_text("modified private\n", encoding="utf-8")
    destination = tmp_path / "candidate"
    manifest_path = export_commit(
        committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["public.py"]), destination
    )

    expected = b"print('committed')\n"
    assert (destination / "public.py").read_bytes() == expected
    assert not (destination / "private-report.md").exists()
    assert manifest_path == destination / "export-manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "source_commit": _git(committed_repo, "rev-parse", "HEAD"),
        "files": [{"path": "public.py", "sha256": hashlib.sha256(expected).hexdigest()}],
    }


def test_replace_ref_cannot_change_exported_commit_blobs(tmp_path: Path, committed_repo: Path) -> None:
    original = _git(committed_repo, "rev-parse", "HEAD")
    (committed_repo / "public.py").write_text("print('replacement')\n", encoding="utf-8")
    _git(committed_repo, "add", "public.py")
    subprocess.run(
        ["git", "-C", str(committed_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "replacement fixture"],
        check=True,
    )
    replacement = _git(committed_repo, "rev-parse", "HEAD")
    _git(committed_repo, "replace", original, replacement)

    destination = tmp_path / "candidate"
    manifest_path = export_commit(
        committed_repo, original, _allowlist(tmp_path / "files.json", ["public.py"]), destination
    )

    assert (destination / "public.py").read_bytes() == b"print('committed')\n"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["source_commit"] == original


def test_missing_allowlisted_path_fails_without_destination(tmp_path: Path, committed_repo: Path) -> None:
    destination = tmp_path / "candidate"
    with pytest.raises(ValueError, match="missing"):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["missing.py"]), destination)
    assert not destination.exists()


@pytest.mark.parametrize(
    "unsafe",
    [".git/config", ".GIT/config", "src/.GiT/file", "/absolute.py", "../escape.py", "src/../public.py", "C:/secret", "C:\\secret", "src\\secret", "public.py/", "public.py.", "CON.py", "a//b", "a/./b"],
)
def test_unsafe_allowlist_path_is_rejected(tmp_path: Path, committed_repo: Path, unsafe: str) -> None:
    destination = tmp_path / "candidate"
    with pytest.raises(ValueError):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", [unsafe]), destination)
    assert not destination.exists()


def test_symlink_and_gitlink_entries_are_rejected(tmp_path: Path, committed_repo: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("not for export", encoding="utf-8")
    (committed_repo / "link").symlink_to(outside)
    _git(committed_repo, "add", "link")
    _git(committed_repo, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",submodule")
    subprocess.run(
        ["git", "-C", str(committed_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "entries"],
        check=True,
    )
    for path in ("link", "submodule"):
        destination = tmp_path / f"candidate-{path}"
        with pytest.raises(ValueError, match="regular"):
            export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", [path]), destination)
        assert not destination.exists()


def test_existing_destination_is_preserved(tmp_path: Path, committed_repo: Path) -> None:
    target = tmp_path / "candidate"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("user data")
    with pytest.raises(FileExistsError):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["public.py"]), target)
    assert marker.read_text() == "user data"


def test_existing_symlink_destination_is_preserved(tmp_path: Path, committed_repo: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    destination = tmp_path / "candidate"
    destination.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(FileExistsError):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["public.py"]), destination)
    assert destination.is_symlink()
    assert list(elsewhere.iterdir()) == []


def test_destination_created_during_export_is_not_replaced(tmp_path: Path, committed_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.export_public_source as exporter

    destination = tmp_path / "candidate"
    original = exporter._rename_new_directory

    def simulate_race(source: Path, target: Path) -> None:
        target.mkdir()
        (target / "keep.txt").write_text("user data", encoding="utf-8")
        original(source, target)

    monkeypatch.setattr(exporter, "_rename_new_directory", simulate_race)
    with pytest.raises(FileExistsError):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["public.py"]), destination)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert not list(tmp_path.glob(".candidate.stage-*"))


def test_executable_bit_is_preserved(tmp_path: Path, committed_repo: Path) -> None:
    executable = committed_repo / "run.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    _git(committed_repo, "add", "run.sh")
    subprocess.run(
        ["git", "-C", str(committed_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "executable"],
        check=True,
    )
    destination = tmp_path / "candidate"
    export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["run.sh"]), destination)
    assert os.stat(destination / "run.sh").st_mode & 0o111


def test_duplicate_or_case_colliding_paths_fail(tmp_path: Path, committed_repo: Path) -> None:
    for paths in (["public.py", "public.py"], ["public.py", "PUBLIC.PY"]):
        destination = tmp_path / "candidate"
        with pytest.raises(ValueError):
            export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", paths), destination)
        assert not destination.exists()


def test_manifest_name_alias_is_rejected(tmp_path: Path, committed_repo: Path) -> None:
    (committed_repo / "EXPORT-MANIFEST.JSON").write_text("not a manifest\n", encoding="utf-8")
    _git(committed_repo, "add", "EXPORT-MANIFEST.JSON")
    subprocess.run(
        ["git", "-C", str(committed_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "alias"],
        check=True,
    )
    destination = tmp_path / "candidate"
    with pytest.raises(ValueError):
        export_commit(committed_repo, "HEAD", _allowlist(tmp_path / "files.json", ["EXPORT-MANIFEST.JSON"]), destination)
    assert not destination.exists()


def test_case_colliding_parent_directories_are_rejected(tmp_path: Path, committed_repo: Path) -> None:
    blob = _git(committed_repo, "rev-parse", "HEAD:public.py")
    for path in ("Sources/one.py", "sources/two.py"):
        _git(committed_repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    subprocess.run(
        ["git", "-C", str(committed_repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "colliding directories"],
        check=True,
    )
    destination = tmp_path / "candidate"
    with pytest.raises(ValueError, match="colliding"):
        export_commit(
            committed_repo,
            "HEAD",
            _allowlist(tmp_path / "files.json", ["Sources/one.py", "sources/two.py"]),
            destination,
        )
    assert not destination.exists()
