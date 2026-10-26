"""Stage the approved official Windows ExifTool package without executing it."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_metadata_runtime import _download, _sha256, verify_archive
from src.xuanpian.engine.metadata_runtime import RuntimeUnavailable, _relative_path, _bundle_runtime, _environment

UTF8_RECIPE = "active-code-page-utf8-v1"


def utf8_process_manifest(original: bytes) -> bytes:
    """Preserve the upstream settings while opting this process into UTF-8."""
    root = ET.fromstring(original.rstrip(b"\0"))
    namespace = "urn:schemas-microsoft-com:asm.v3"
    application = root.find(f"{{{namespace}}}application")
    if application is None:
        application = ET.SubElement(root, f"{{{namespace}}}application")
    settings = application.find(f"{{{namespace}}}windowsSettings")
    if settings is None:
        settings = ET.SubElement(application, f"{{{namespace}}}windowsSettings")
    tag = "{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage"
    node = settings.find(tag)
    if node is None:
        node = ET.SubElement(settings, tag)
    node.text = "UTF-8"
    ET.register_namespace("", "urn:schemas-microsoft-com:asm.v1")
    ET.register_namespace("asmv3", namespace)
    ET.register_namespace("ws2", "http://schemas.microsoft.com/SMI/2016/WindowsSettings")
    ET.register_namespace("ws3", "http://schemas.microsoft.com/SMI/2019/WindowsSettings")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def read_launcher_manifest(executable: Path) -> bytes:
    from PyInstaller.utils.win32.winresource import get_resources
    # Upstream uses language 1033, not neutral. Read values explicitly rather
    # than relying on the pinned high-level reader's non-neutral fallback.
    resources = get_resources(str(executable), [24])[24][1]
    manifests = list(resources.values())
    if not manifests or any(value != manifests[0] for value in manifests):
        raise ValueError("missing or conflicting launcher manifests")
    return manifests[0]


def enable_utf8_launcher(executable: Path, recipe: dict) -> None:
    if recipe.get("recipe") != UTF8_RECIPE or _sha256(executable) != recipe.get("upstream_sha256"):
        raise ValueError("unapproved launcher transform input")
    if sys.platform != "win32":
        raise RuntimeError("native Windows required for launcher resource transform")
    from PyInstaller.utils.win32.winmanifest import write_manifest_to_executable
    original = read_launcher_manifest(executable)
    manifest = utf8_process_manifest(original)
    write_manifest_to_executable(str(executable), manifest)
    if read_launcher_manifest(executable).rstrip(b"\0") != manifest:
        raise ValueError("launcher manifest resource verification failed")
    root = executable.parent.parent
    (root / "launcher-utf8.manifest").write_bytes(manifest)
    (root / "launcher-transform.json").write_text(json.dumps({
        "recipe": UTF8_RECIPE, "upstream_sha256": recipe["upstream_sha256"],
        "derived_sha256": _sha256(executable), "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    }, indent=2) + "\n", encoding="utf-8")


def prepare_runtime(source_manifest: Path, archive: Path, output: Path) -> Path:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists: {output}")
    source_bytes = source_manifest.read_bytes()
    config = json.loads(source_bytes)
    if (config.get("schema_version") != 1 or config.get("target") != {
            "platform": "windows", "architecture": "x86_64"}):
        raise ValueError("Windows x64 source manifest required")
    source = config["exiftool"]
    version = source["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", version):
        raise ValueError("invalid ExifTool version")
    verify_archive(archive, source["sha256"])
    prefix = f"exiftool-{version}_64/"
    output = output.absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".windows-runtime-", dir=output.parent) as temp:
        runtime = Path(temp) / "metadata-runtime"
        vendor = runtime / "exiftool"
        vendor.mkdir(parents=True)
        with ZipFile(archive) as zipped:
            members = zipped.infolist()
            if len(members) > 3000 or sum(m.file_size for m in members) > 128 * 1024 * 1024:
                raise ValueError("archive exceeds runtime limits")
            validated = []
            names: set[str] = set()
            for member in members:
                if member.filename == prefix:
                    continue
                if not member.filename.startswith(prefix):
                    raise ValueError("unexpected archive root")
                name = member.filename[len(prefix):].rstrip("/")
                try:
                    relative = _relative_path(name)
                except RuntimeUnavailable as error:
                    raise ValueError("unsafe archive path") from error
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or name.casefold() in names:
                    raise ValueError("archive link, special entry or case collision")
                names.add(name.casefold())
                validated.append((member, relative))
            for member, relative in validated:
                target = vendor / relative
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(member) as src, target.open("xb") as dst:
                        import shutil
                        shutil.copyfileobj(src, dst)
        executable = vendor / "exiftool.exe"
        if executable.exists():
            raise ValueError("unexpected pre-renamed launcher")
        (vendor / "exiftool(-k).exe").rename(executable)
        executable.chmod(0o755)
        if config.get("launcher_transform") is not None:
            enable_utf8_launcher(executable, config["launcher_transform"])
        generated = {
            "schema_version": 2,
            "target": config["target"],
            "versions": {"exiftool": version},
            "command_prefix": {"executable": "exiftool/exiftool.exe",
                               "fixed_args": ["-config", "", "-charset", "filename=UTF8"]},
            "files": {p.relative_to(runtime).as_posix(): _sha256(p)
                      for p in sorted(runtime.rglob("*")) if p.is_file()},
            "source_manifest_sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        (runtime / "manifest.json").write_text(json.dumps(generated, indent=2) + "\n", encoding="utf-8")
        _bundle_runtime(runtime.parent, _environment())
        runtime.rename(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "packaging/runtime-windows-sources.json")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.archive is not None:
        print(prepare_runtime(args.manifest, args.archive, args.output))
    else:
        config = json.loads(args.manifest.read_text())
        with TemporaryDirectory(prefix="xuanpian-windows-download-") as temp:
            archive = Path(temp) / "exiftool.zip"
            _download(config["exiftool"]["url"], archive)
            print(prepare_runtime(args.manifest, archive, args.output))


if __name__ == "__main__":
    main()
