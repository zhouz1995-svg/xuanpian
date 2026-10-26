from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


@dataclass(frozen=True)
class MetadataRuntime:
    command_prefix: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    identity: str


class RuntimeUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_ENVIRONMENT_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
_VERIFIED_FILES: dict[str, tuple[tuple[int, ...], str]] = {}


def _environment() -> tuple[tuple[str, str], ...]:
    if sys.platform == "win32":
        root = os.environ.get("SystemRoot", os.environ.get("WINDIR", ""))
        if not root or not PureWindowsPath(root).is_absolute():
            raise RuntimeUnavailable("metadata_runtime_invalid")
        values = {"SystemRoot": root, "WINDIR": root,
                  "PATH": str(PureWindowsPath(root) / "System32")}
        values.update((key, os.environ[key]) for key in ("TEMP", "TMP") if key in os.environ)
        return tuple(sorted(values.items()))
    values = {"PATH": _SYSTEM_PATH}
    values.update(
        (name, os.environ[name])
        for name in _ENVIRONMENT_KEYS
        if name in os.environ
    )
    return tuple(sorted(values.items()))


def _relative_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.drive or windows.root or any(
        part in ("", ".", "..") or part.endswith((".", " "))
        or PureWindowsPath(part).is_reserved() or any(c in '<>:"|?*' or ord(c) < 32 for c in part)
        for part in value.split("/")
    ):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    return path


def _contained(root: Path, path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeUnavailable("metadata_runtime_invalid") from None
    return resolved


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise RuntimeUnavailable("metadata_runtime_invalid") from None
    return digest.hexdigest()


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_link(path: Path) -> bool:
    """Reject NTFS junctions/reparse entries as well as POSIX symlinks."""
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)


def _validate_manifest(data: Any) -> tuple[dict[str, Any], dict[str, str]]:
    if isinstance(data, dict) and type(data.get("schema_version")) is int and data["schema_version"] == 2:
        return _validate_windows_manifest(data)
    if not isinstance(data, dict) or set(data) != {
        "schema_version", "versions", "command_prefix", "files", "source_manifest_sha256",
    } or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    versions = data["versions"]
    if not isinstance(versions, dict) or set(versions) != {"perl", "exiftool"} or not all(
        isinstance(value, str) and value for value in versions.values()
    ) or not _is_digest(data["source_manifest_sha256"]):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    if not all(re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", value) for value in versions.values()):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    command = data["command_prefix"]
    if not isinstance(command, dict) or set(command) != {
        "perl", "module_dirs", "script", "fixed_args",
    } or command["fixed_args"] != ["-config", ""]:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    if command["perl"] != "perl/bin/perl" or command["script"] != "exiftool/exiftool":
        raise RuntimeUnavailable("metadata_runtime_invalid")
    modules = command["module_dirs"]
    if not isinstance(modules, list) or len(modules) != 3 or not all(
        isinstance(value, str) for value in modules
    ) or len(modules) != len(set(modules)):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    perl_lib = f"perl/lib/{versions['perl']}"
    arch_prefix = f"{perl_lib}/darwin-"
    if modules[1:] != [perl_lib, "exiftool/lib"] or not modules[0].startswith(arch_prefix) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", modules[0][len(arch_prefix):]
    ):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    files = data["files"]
    if not isinstance(files, dict) or not files or not all(
        _is_digest(value) for value in files.values()
    ):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    for name in files:
        _relative_path(name)
    for name in (command["perl"], command["script"]):
        if name not in files:
            raise RuntimeUnavailable("metadata_runtime_invalid")
    for name in modules:
        _relative_path(name)
    return command, files


def _validate_windows_manifest(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    if set(data) != {"schema_version", "target", "versions", "command_prefix", "files",
                     "source_manifest_sha256"} or data["target"] != {
                         "platform": "windows", "architecture": "x86_64"}:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    versions = data["versions"]
    if not isinstance(versions, dict) or set(versions) != {"exiftool"} or not isinstance(
        versions["exiftool"], str
    ) or not re.fullmatch(r"[0-9]+\.[0-9]+", versions["exiftool"]):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    command = data["command_prefix"]
    if command != {"executable": "exiftool/exiftool.exe",
                   "fixed_args": ["-config", "", "-charset", "filename=UTF8"]}:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    files = data["files"]
    required = {"exiftool/exiftool.exe", "exiftool/exiftool_files/perl.exe",
                "exiftool/exiftool_files/perl532.dll", "exiftool/exiftool_files/exiftool.pl"}
    if (not _is_digest(data["source_manifest_sha256"]) or not isinstance(files, dict)
            or not required <= files.keys() or not all(_is_digest(v) for v in files.values())):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    for name in files:
        _relative_path(name)
    if len({name.casefold() for name in files}) != len(files):
        raise RuntimeUnavailable("metadata_runtime_invalid")
    return command, files


def _file_state(path: Path) -> tuple[int, ...]:
    try:
        stat = path.stat()
    except OSError:
        raise RuntimeUnavailable("metadata_runtime_invalid") from None
    if not path.is_file():
        raise RuntimeUnavailable("metadata_runtime_invalid")
    return (
        stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size,
        stat.st_mtime_ns, stat.st_ctime_ns,
    )


def _bundle_runtime(bundle_root: Path, environment: tuple[tuple[str, str], ...]) -> MetadataRuntime:
    try:
        linked_root = _is_link(bundle_root) or _is_link(bundle_root / "metadata-runtime")
        approved_root = bundle_root.resolve(strict=True)
        root = (bundle_root / "metadata-runtime").resolve(strict=True)
        root.relative_to(approved_root)
    except (OSError, RuntimeError, ValueError):
        raise RuntimeUnavailable("metadata_runtime_missing") from None
    if linked_root:
        raise RuntimeUnavailable("metadata_runtime_invalid")
    if not root.is_dir():
        raise RuntimeUnavailable("metadata_runtime_missing")
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeUnavailable("metadata_runtime_missing")
    manifest_path = _contained(root, manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
        data = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeUnavailable("metadata_runtime_invalid") from None
    command, files = _validate_manifest(data)
    allowed_files = set(files) | {"manifest.json"}

    for entry in root.rglob("*"):
        if _is_link(entry) or (
            entry.is_file()
            and entry.relative_to(root).as_posix() not in allowed_files
        ) or not (entry.is_file() or entry.is_dir()):
            raise RuntimeUnavailable("metadata_runtime_invalid")

    for name, expected in files.items():
        path = _contained(root, root / _relative_path(name))
        state = _file_state(path)
        cache_key = str(root / name)
        cached = _VERIFIED_FILES.get(cache_key)
        if cached is None or cached[0] != state:
            actual = _digest(path)
            if _file_state(path) != state:
                raise RuntimeUnavailable("metadata_runtime_invalid")
            _VERIFIED_FILES[cache_key] = (state, actual)
        else:
            actual = cached[1]
        if actual != expected:
            raise RuntimeUnavailable("metadata_runtime_corrupt")

    if "executable" in command:
        executable = _contained(root, root / command["executable"])
        if not os.access(executable, os.X_OK):
            raise RuntimeUnavailable("metadata_runtime_invalid")
        prefix = (str(executable), *command["fixed_args"])
    else:
        perl = _contained(root, root / command["perl"])
        if not os.access(perl, os.X_OK):
            raise RuntimeUnavailable("metadata_runtime_invalid")
        modules = [_contained(root, root / name) for name in command["module_dirs"]]
        if not all(path.is_dir() for path in modules):
            raise RuntimeUnavailable("metadata_runtime_invalid")
        script = _contained(root, root / command["script"])
        prefix = (str(perl), *(argument for path in modules for argument in ("-I", str(path))),
                  str(script), "-config", "")
    identity = "bundle:" + hashlib.sha256(manifest_bytes).hexdigest()
    return MetadataRuntime(prefix, environment, identity)


def resolve_metadata_runtime(
    *, bundle_root: Path | None, development_executable: str | None
) -> MetadataRuntime:
    environment = _environment()
    if bundle_root is not None:
        return _bundle_runtime(bundle_root, environment)
    executable = shutil.which(development_executable) if development_executable else None
    if executable is None:
        raise RuntimeUnavailable("exiftool_unavailable")
    resolved = str(Path(executable).resolve(strict=False))
    args = ("-config", "", "-charset", "filename=UTF8") if sys.platform == "win32" else ("-config", "")
    return MetadataRuntime((resolved, *args), environment, "development:" + resolved)
