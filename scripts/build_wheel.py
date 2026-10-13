#!/usr/bin/env python3
"""Build a wheel from immutable blobs in the Git index.

Usage:
    .venv/bin/python scripts/build_wheel.py --outdir dist

The release inputs are staged from Git blobs, never from checkout source paths.
The builder rejects unstaged tracked and non-ignored untracked release inputs
that Git reports. Ignored files do not participate. Git's assume-unchanged and
skip-worktree flags can suppress worktree changes from those checks; wheel bytes
still come from the indexed blobs.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unicodedata
import tomllib
import zipfile


REPO_ROOT = Path(__file__).resolve().parent.parent
_RELEASE_INPUTS = ("pyproject.toml", "src", "LICENSE", "THIRD_PARTY_NOTICES.md",
                   "packaging/licenses", "packaging/notice-manifest.json",
                   "packaging/third-party-components.json")
_ALLOWED_INDEX_MODES = {"100644": 0o644, "100755": 0o755}
_WINDOWS_FORBIDDEN_CHARACTERS = frozenset('<>:"\\|?*')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
        *(f"lpt{suffix}" for suffix in (*range(1, 10), "¹", "²", "³")),
    }
)


class CleanWheelBuildError(RuntimeError):
    """Raised when a clean wheel cannot be produced from indexed inputs."""


def notice_payload(root: Path) -> dict[str, bytes]:
    """Validate the reviewed notice snapshot; this does not clear licensing."""
    manifest_name = "packaging/notice-manifest.json"
    required = {"LICENSE", "THIRD_PARTY_NOTICES.md", "packaging/third-party-components.json"}

    def read_regular(name: str) -> bytes:
        path = root
        for component in PurePosixPath(name).parts:
            path = path / component
            if path.is_symlink():
                raise CleanWheelBuildError("notice contains symlink")
        return path.read_bytes()

    try:
        raw = read_regular(manifest_name)
        manifest = json.loads(raw)
        files = manifest["files"]
        if manifest["schema_version"] != 1 or not isinstance(files, dict) or not required <= files.keys():
            raise CleanWheelBuildError("incomplete notice manifest")
        observed = {p.relative_to(root).as_posix() for p in (root / "packaging/licenses").rglob("*.txt")}
        if not observed or set(files) != required | observed:
            raise CleanWheelBuildError("notice manifest does not cover license texts")
        payload = {manifest_name: raw}
        for name, digest in files.items():
            path = _release_path(os.fsencode(name))
            if name not in required and not (path.parts[:2] == ("packaging", "licenses") and len(path.parts) > 2):
                raise CleanWheelBuildError("unexpected notice path")
            data = read_regular(name)
            if not data or hashlib.sha256(data).hexdigest() != digest:
                raise CleanWheelBuildError("notice missing or hash mismatch: " + name)
            payload[name] = data
        return payload
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CleanWheelBuildError("invalid or missing notice payload") from error


def verify_wheel_notices(wheel: Path, payload: dict[str, bytes]) -> None:
    """Require exactly one unchanged copy of every notice in wheel metadata."""
    with zipfile.ZipFile(wheel) as archive:
        for name, expected in payload.items():
            matches = [item for item in archive.namelist()
                       if item.endswith(".dist-info/licenses/" + name)]
            if len(matches) != 1 or archive.read(matches[0]) != expected:
                raise CleanWheelBuildError("wheel notice missing or changed: " + name)


@dataclass(frozen=True)
class _IndexEntry:
    mode: str
    oid: str
    path: PurePosixPath


def _run_git(repo_root: Path, arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CleanWheelBuildError("cannot inspect release index") from error


def _release_path(raw: bytes) -> PurePosixPath:
    text = os.fsdecode(raw)
    relative = PurePosixPath(text)
    windows = PureWindowsPath(text)
    components = text.split("/")
    if (
        not text
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            for character in text
        )
        or "\\" in text
        or relative.is_absolute()
        or windows.drive
        or windows.root
        or any(part in {"", ".", ".."} for part in components)
        or not any(text == prefix or text.startswith(prefix + "/") for prefix in _RELEASE_INPUTS)
    ):
        raise CleanWheelBuildError("indexed release input has unsafe path")
    for component in components:
        reserved_base = component.split(".", 1)[0].rstrip(". ").casefold()
        if (
            component.endswith((".", " "))
            or reserved_base in _WINDOWS_RESERVED_BASENAMES
            or any(
                character in _WINDOWS_FORBIDDEN_CHARACTERS
                for character in component
            )
        ):
            raise CleanWheelBuildError("indexed release input has unsafe path")
    return relative


def _windows_staging_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(component.rstrip(". ").casefold() for component in path.parts)


def _validate_staging_paths(paths: tuple[PurePosixPath, ...]) -> None:
    keyed_paths: dict[tuple[str, ...], PurePosixPath] = {}
    for path in paths:
        key = _windows_staging_key(path)
        previous = keyed_paths.get(key)
        if previous is not None and previous != path:
            raise CleanWheelBuildError(
                f"Windows staging path collision: {_display_path(path)}"
            )
        keyed_paths[key] = path

    for key, path in keyed_paths.items():
        for prefix_length in range(1, len(key)):
            if key[:prefix_length] in keyed_paths:
                raise CleanWheelBuildError(
                    f"Windows staging file/directory collision: {_display_path(path)}"
                )


def _display_path(path: PurePosixPath) -> str:
    text = path.as_posix()
    if not text.isprintable() or any(character in text for character in "\r\n\t"):
        return "<unsafe-path>"
    return text


def _indexed_release_entries(repo_root: Path) -> tuple[_IndexEntry, ...]:
    result = _run_git(
        repo_root,
        ["ls-files", "--stage", "-z", "--", *_RELEASE_INPUTS],
    )
    if result.returncode != 0:
        raise CleanWheelBuildError("cannot enumerate indexed release inputs")

    entries: list[_IndexEntry] = []
    seen: set[PurePosixPath] = set()
    for raw_entry in result.stdout.split(b"\0"):
        if not raw_entry:
            continue
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            raw_mode, raw_oid, raw_stage = metadata.split()
            mode = raw_mode.decode("ascii")
            oid = raw_oid.decode("ascii")
            stage_number = raw_stage.decode("ascii")
        except (UnicodeDecodeError, ValueError) as error:
            raise CleanWheelBuildError("malformed indexed release entry") from error

        path = _release_path(raw_path)
        display = _display_path(path)
        if stage_number != "0":
            raise CleanWheelBuildError(
                f"indexed release input is not stage 0: {display}"
            )
        if mode not in _ALLOWED_INDEX_MODES:
            raise CleanWheelBuildError(
                f"unsupported indexed release mode {mode}: {display}"
            )
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise CleanWheelBuildError(f"invalid indexed release blob: {display}")
        if path in seen:
            raise CleanWheelBuildError(f"duplicate indexed release input: {display}")
        seen.add(path)
        entries.append(_IndexEntry(mode=mode, oid=oid, path=path))

    _validate_staging_paths(tuple(entry.path for entry in entries))
    if PurePosixPath("pyproject.toml") not in seen:
        raise CleanWheelBuildError("indexed pyproject.toml is required")
    return tuple(entries)


def _first_reportable_path(raw_paths: bytes) -> str | None:
    for raw_path in raw_paths.split(b"\0"):
        if raw_path:
            return _display_path(_release_path(raw_path))
    return None


def _require_complete_release_index(repo_root: Path) -> None:
    unstaged = _run_git(
        repo_root,
        ["diff", "--quiet", "--", *_RELEASE_INPUTS],
    )
    if unstaged.returncode == 1:
        names = _run_git(
            repo_root,
            ["diff", "--name-only", "--no-renames", "-z", "--", *_RELEASE_INPUTS],
        )
        if names.returncode != 0:
            raise CleanWheelBuildError("cannot inspect unstaged release inputs")
        path = _first_reportable_path(names.stdout)
        if path is None:
            raise CleanWheelBuildError("unstaged release input must be staged")
        raise CleanWheelBuildError(
            f"unstaged release input must be staged: {path}"
        )
    if unstaged.returncode != 0:
        raise CleanWheelBuildError("cannot inspect unstaged release inputs")

    untracked = _run_git(
        repo_root,
        [
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *_RELEASE_INPUTS,
        ],
    )
    if untracked.returncode != 0:
        raise CleanWheelBuildError("cannot inspect untracked release inputs")
    path = _first_reportable_path(untracked.stdout)
    if path is not None:
        raise CleanWheelBuildError(f"untracked release input must be staged: {path}")


def _read_index_blob(repo_root: Path, entry: _IndexEntry) -> bytes:
    result = _run_git(repo_root, ["cat-file", "blob", entry.oid])
    if result.returncode != 0:
        raise CleanWheelBuildError(
            f"cannot read indexed release blob: {_display_path(entry.path)}"
        )
    return result.stdout


def _stage_release_inputs(
    repo_root: Path, stage_root: Path, entries: tuple[_IndexEntry, ...]
) -> None:
    for entry in entries:
        destination = stage_root.joinpath(*entry.path.parts)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_read_index_blob(repo_root, entry))
            destination.chmod(_ALLOWED_INDEX_MODES[entry.mode])
        except CleanWheelBuildError:
            raise
        except OSError as error:
            raise CleanWheelBuildError(
                f"cannot stage indexed release input: {_display_path(entry.path)}"
            ) from error


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _publish_wheel(source: Path, destination: Path) -> None:
    try:
        with TemporaryDirectory(
            dir=destination.parent,
            prefix=".xuanpian-wheel-publish-",
        ) as temporary:
            private_root = Path(temporary)
            private_root.chmod(0o700)
            temporary_wheel = private_root / source.name
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(temporary_wheel, flags, 0o600)
            descriptor_open = True
            source_digest = hashlib.sha256()
            source_size = 0
            try:
                with source.open("rb") as payload:
                    with os.fdopen(descriptor, "wb") as target:
                        descriptor_open = False
                        for chunk in iter(lambda: payload.read(1024 * 1024), b""):
                            source_size += len(chunk)
                            source_digest.update(chunk)
                            target.write(chunk)
                        target.flush()
                        os.fsync(target.fileno())
            finally:
                if descriptor_open:
                    os.close(descriptor)

            staged_metadata = temporary_wheel.lstat()
            if not stat.S_ISREG(staged_metadata.st_mode):
                raise CleanWheelBuildError("private wheel output is not a regular file")
            os.replace(temporary_wheel, destination)

            published_metadata = destination.lstat()
            if not stat.S_ISREG(published_metadata.st_mode):
                raise CleanWheelBuildError("published wheel is not a regular file")
            published_size, published_hash = _hash_file(destination)
            if (
                published_size != source_size
                or published_hash != source_digest.hexdigest()
            ):
                raise CleanWheelBuildError("published wheel verification failed")
    except CleanWheelBuildError:
        raise
    except OSError as error:
        raise CleanWheelBuildError("cannot publish wheel") from error


def _build_staged_wheel(repo_root: Path, output_directory: Path) -> Path:
    entries = _indexed_release_entries(repo_root)
    _require_complete_release_index(repo_root)

    try:
        with TemporaryDirectory(prefix="xuanpian-clean-wheel-") as temporary:
            temporary_root = Path(temporary)
            stage_root = temporary_root / "project"
            wheelhouse = temporary_root / "wheelhouse"
            stage_root.mkdir()
            wheelhouse.mkdir()
            _stage_release_inputs(repo_root, stage_root, entries)
            project = tomllib.loads((stage_root / "pyproject.toml").read_text())["project"]
            notices = notice_payload(stage_root) if project["name"] == "xuanpian" else None

            environment = {
                **os.environ,
                "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_CACHE_DIR": str(temporary_root / "pip-cache"),
            }
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "wheel",
                        "--no-deps",
                        "--no-build-isolation",
                        "--wheel-dir",
                        str(wheelhouse),
                        str(stage_root),
                    ],
                    cwd=temporary_root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CleanWheelBuildError("clean wheel build backend unavailable") from error
            if result.returncode != 0:
                raise CleanWheelBuildError("clean wheel build backend failed")

            wheels = tuple(wheelhouse.glob("*.whl"))
            if len(wheels) != 1:
                raise CleanWheelBuildError("clean wheel build did not produce one wheel")
            if notices is not None:
                verify_wheel_notices(wheels[0], notices)
            destination = output_directory / wheels[0].name
            _publish_wheel(wheels[0], destination)
            return destination
    except CleanWheelBuildError:
        raise
    except OSError as error:
        raise CleanWheelBuildError("cannot prepare clean wheel build") from error


def build_clean_wheel(
    output_directory: Path, *, repo_root: Path = REPO_ROOT
) -> Path:
    """Build and return one wheel from complete Git-index release inputs."""
    try:
        trusted_repo_root = repo_root.resolve(strict=True)
        resolved_output = output_directory.resolve()
        resolved_output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CleanWheelBuildError("cannot prepare clean wheel paths") from error
    return _build_staged_wheel(trusted_repo_root, resolved_output)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="build a wheel from complete Git-index release inputs"
    )
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()
    try:
        wheel = build_clean_wheel(args.outdir)
    except CleanWheelBuildError as error:
        print(f"clean wheel build failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"wheel": str(wheel)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
