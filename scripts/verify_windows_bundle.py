"""Inspect the exact Windows onedir candidate or installed application."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_metadata_runtime import _sha256
from scripts.build_wheel import notice_payload
from src.xuanpian.engine.metadata_runtime import _relative_path, resolve_metadata_runtime, _is_link


def dll_visible(name: str, loader: Path, app: Path) -> bool:
    """Check required imports in loader/bootstrap or scoped wheel DLL directories.

    Delvewheel package directories are not global search paths. In particular,
    the ExifTool Perl directory cannot satisfy imports of the main application.
    Optional delay-load imports are not classified as required imports here.
    """
    if any(c in name for c in ("/", "\\", ":")):
        return False
    internal = app / "_internal"
    directories = [loader.parent, internal, app]
    perl_directory = internal / "metadata-runtime/exiftool/exiftool_files"
    if loader.is_relative_to(perl_directory):
        directories.append(perl_directory)  # Perl child executable / preloaded perl532.dll.
    try:
        package = loader.relative_to(internal).parts[0]
    except ValueError:
        package = ""
    scopes = {
        "PIL": ("pillow.libs", "PIL/.libs", "PIL/.dylibs"),
        "numpy": ("numpy.libs",), "rawpy": ("rawpy.libs",),
        "pillow_heif": ("pillow_heif.libs",),
        "PyQt6": ("PyQt6/Qt6/bin",), "onnxruntime": ("onnxruntime/capi",),
    }
    if package.startswith("_pillow_heif"):
        package = "pillow_heif"
    directories.extend(internal / path for path in scopes.get(package, ()))
    return any((directory / name).is_file() for directory in directories)


def pe_machine(path: Path) -> int:
    with path.open("rb") as source:
        header = source.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError("missing DOS header")
        offset = int.from_bytes(header[60:64], "little")
        if offset < 64 or offset > path.stat().st_size - 6:
            raise ValueError("invalid PE offset")
        source.seek(offset)
        pe = source.read(6)
    if pe[:4] != b"PE\0\0":
        raise ValueError("missing PE header")
    return int.from_bytes(pe[4:6], "little")


def inspect_bundle(app: Path) -> list[str]:
    problems = []
    try:
        root = app.resolve(strict=True)
        internal = app / "_internal"
        if _is_link(app) or _is_link(internal) or not (app / "XuanPian.exe").is_file():
            raise ValueError("missing or linked application entry point")
        inventory = json.loads((app / "bundle-files.json").read_text(encoding="utf-8"))
        if set(inventory) != {"schema_version", "files"} or inventory["schema_version"] != 1:
            raise ValueError("invalid bundle inventory")
        files = inventory["files"]
        observed = set()
        native = []
        for path in app.rglob("*"):
            if _is_link(path):
                raise ValueError("bundle contains symlink")
            path.resolve(strict=True).relative_to(root)
            if not path.is_file():
                continue
            relative = path.relative_to(app).as_posix()
            if relative == "bundle-files.json" or re.fullmatch(r"unins[0-9]{3}\.(exe|dat|msg)", relative):
                continue  # Inno's installed uninstaller is not part of the source onedir.
            observed.add(relative)
            if files.get(relative) != _sha256(path):
                problems.append("file hash: " + relative)
            with path.open("rb") as source:
                if source.read(2) == b"MZ":
                    native.append(path)
                    if pe_machine(path) != 0x8664:
                        problems.append("not x64 PE: " + relative)
        if observed != set(files):
            problems.append("bundle file set differs from inventory")
        for name in files:
            _relative_path(name)
        resolve_metadata_runtime(bundle_root=internal, development_executable=None)
        metadata_root = internal / "metadata-runtime"
        transform = json.loads((metadata_root / "launcher-transform.json").read_text())
        recipe = json.loads((ROOT / "packaging/runtime-windows-sources.json").read_text())["launcher_transform"]
        if (transform.get("recipe") != recipe["recipe"]
                or transform.get("upstream_sha256") != recipe["upstream_sha256"]
                or transform.get("derived_sha256") != _sha256(metadata_root / "exiftool/exiftool.exe")
                or transform.get("manifest_sha256") != _sha256(metadata_root / "launcher-utf8.manifest")):
            problems.append("invalid launcher transform provenance")
        if sys.platform == "win32":
            from scripts.prepare_windows_runtime import read_launcher_manifest
            embedded = read_launcher_manifest(metadata_root / "exiftool/exiftool.exe")
            if embedded.rstrip(b"\0") != (metadata_root / "launcher-utf8.manifest").read_bytes():
                problems.append("launcher embedded manifest mismatch")
        for name, expected in notice_payload(ROOT).items():
            if (internal / "legal" / name).read_bytes() != expected:
                problems.append("notice mismatch: " + name)
        model_root = internal / "src/xuanpian/resources/models"
        approved = ROOT / "src/xuanpian/resources/models/manifest.json"
        if (model_root / "manifest.json").read_bytes() != approved.read_bytes():
            problems.append("model manifest mismatch")
        for item in json.loads(approved.read_text())["models"].values():
            path = model_root / item["filename"]
            if _sha256(path) != item["sha256"]:
                problems.append("model hash: " + item["filename"])
        for name in ("src/xuanpian/ui/styles.qss", "src/xuanpian/resources/icons/app-icon.png"):
            if not (internal / name).is_file():
                problems.append("missing resource: " + name)
        provenance = json.loads((internal / "build-provenance.json").read_text())
        if (provenance.get("source_tree_dirty") is not False
                or not re.fullmatch(r"[0-9a-f]{40}", provenance.get("source_revision", ""))
                or provenance.get("requirements_lock_sha256") != _sha256(ROOT / "packaging/requirements-windows-x64.lock")):
            problems.append("invalid source provenance")
        if sys.platform == "win32":
            import pefile
            system = Path(os.environ["SystemRoot"]) / "System32"
            for path in native:
                pe = pefile.PE(str(path), fast_load=True)
                try:
                    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
                    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", ()):
                        dll = entry.dll.decode("ascii").casefold()
                        if dll_visible(dll, path, app) or dll.startswith(("api-ms-", "ext-ms-")):
                            continue
                        if dll.startswith(("vcruntime", "msvcp", "concrt", "vcomp")) or not (system / dll).is_file():
                            problems.append("unbundled native import: " + path.name + " -> " + dll)
                finally:
                    pe.close()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        problems.append(str(error))
    return sorted(set(problems))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    result = inspect_bundle(parser.parse_args().app)
    print(json.dumps({"status": "FAIL" if result else "PASS", "problems": result}, ensure_ascii=False))
    raise SystemExit(bool(result))
