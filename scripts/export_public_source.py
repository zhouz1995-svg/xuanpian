"""Export an explicit allowlist of committed blobs into a new local directory.

The returned Path is ``destination/export-manifest.json``. That in-tree
manifest records the resolved commit and SHA-256 of every exported file.
This prepares a local candidate; it does not authorize publication.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unicodedata


_MANIFEST = "export-manifest.json"
_WINDOWS_DEVICE = re.compile(r"(?i)^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        stderr=subprocess.PIPE,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    )


def _validate_paths(allowlist: Path) -> list[str]:
    document = json.loads(allowlist.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"paths"} or not isinstance(document["paths"], list):
        raise ValueError("allowlist must contain only a paths array")
    paths = document["paths"]
    if not paths:
        raise ValueError("allowlist must not be empty")
    aliases: set[str] = set()
    directory_aliases: dict[str, str] = {}
    for path in paths:
        if not isinstance(path, str) or not path or path.casefold() == _MANIFEST:
            raise ValueError("invalid allowlist path")
        parts = path.split("/")
        if any(
            part in {"", ".", ".."}
            or part.casefold() == ".git"
            or part.endswith((".", " "))
            or _WINDOWS_DEVICE.match(part)
            or any(ord(char) < 32 or char in "\\:" for char in part)
            for part in parts
        ) or unicodedata.normalize("NFC", path) != path:
            raise ValueError("unsafe allowlist path")
        alias = path.casefold()
        if alias in aliases or any(alias.startswith(prior + "/") or prior.startswith(alias + "/") for prior in aliases):
            raise ValueError("duplicate or colliding allowlist path")
        for length in range(1, len(parts)):
            prefix = "/".join(parts[:length])
            prior = directory_aliases.setdefault(prefix.casefold(), prefix)
            if prior != prefix:
                raise ValueError("colliding allowlist directories")
        aliases.add(alias)
    return paths


def _tree_blobs(repo: Path, revision: str) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in _git(repo, "ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split(" ")
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("commit contains a non-UTF-8 tree path") from error
        entries[path] = (mode, oid)
    return entries


def _rename_new_directory(source: Path, destination: Path) -> None:
    """Atomically publish a staged directory without replacing any existing name."""
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = library.renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-2, source_bytes, -2, destination_bytes, 0x4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = library.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, source_bytes, -100, destination_bytes, 0x1)  # RENAME_NOREPLACE
    else:
        raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")
    if result:
        number = ctypes.get_errno()
        if number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(number, os.strerror(number), destination)
        raise OSError(number, os.strerror(number), destination)


def export_commit(repo: Path, commit: str, allowlist: Path, destination: Path) -> Path:
    """Export allowlisted regular blobs from one immutable commit into a new tree."""
    repo, destination = Path(repo), Path(destination)
    if not isinstance(commit, str) or not commit or commit.startswith("-") or "\x00" in commit:
        raise ValueError("explicit commit required")
    paths = _validate_paths(Path(allowlist))
    if os.path.lexists(destination):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), destination)
    if not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)

    revision = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("could not resolve immutable commit")
    entries = _tree_blobs(repo, revision)
    for path in paths:
        entry = entries.get(path)
        if entry is None:
            raise ValueError(f"missing allowlisted path: {path}")
        if entry[0] not in {"100644", "100755"}:
            raise ValueError(f"allowlisted path is not a regular blob: {path}")

    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
    files_created: list[Path] = []
    directories_created: set[Path] = set()
    try:
        manifest_files: list[dict[str, str]] = []
        for path in paths:
            mode, oid = entries[path]
            data = _git(repo, "cat-file", "blob", oid)
            target = stage / path
            missing_parents: list[Path] = []
            parent = target.parent
            while parent != stage and not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            for directory in reversed(missing_parents):
                directory.mkdir()
                directories_created.add(directory)
            with target.open("xb") as output:
                files_created.append(target)
                output.write(data)
            target.chmod(0o755 if mode == "100755" else 0o644)
            manifest_files.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
        manifest = stage / _MANIFEST
        with manifest.open("x", encoding="utf-8") as output:
            files_created.append(manifest)
            json.dump({"source_commit": revision, "files": manifest_files}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        _rename_new_directory(stage, destination)
    except BaseException:
        for path in reversed(files_created):
            path.unlink()
        for directory in sorted(directories_created, key=lambda item: len(item.parts), reverse=True):
            directory.rmdir()
        stage.rmdir()
        raise
    return destination / _MANIFEST


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    manifest = export_commit(Path.cwd(), args.commit, args.allowlist, args.destination)
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
