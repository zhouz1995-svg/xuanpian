"""Inspect a relocatable, locally ad-hoc-signed arm64 macOS app."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_macos import LOCK, project_version  # noqa: E402
from scripts.build_wheel import CleanWheelBuildError, notice_payload  # noqa: E402
from src.xuanpian.engine.metadata_runtime import RuntimeUnavailable, resolve_metadata_runtime  # noqa: E402

MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
SYSTEM = ("/System/Library/", "/usr/lib/")
RUNTIME = Path("Contents/Resources/metadata-runtime")
MODELS = Path("Contents/Resources/src/xuanpian/resources/models")


@dataclass(frozen=True)
class NativeInfo:
    dependencies: tuple[str, ...]
    rpaths: tuple[str, ...]
    architectures: str
    minos: str


def _native_metadata(path: Path) -> NativeInfo:
    libraries = subprocess.run(["/usr/bin/otool", "-L", str(path)], check=True,
                               capture_output=True, text=True).stdout.splitlines()[1:]
    commands = subprocess.run(["/usr/bin/otool", "-l", str(path)], check=True,
                              capture_output=True, text=True).stdout
    architectures = subprocess.run(["/usr/bin/lipo", "-archs", str(path)], check=True,
                                   capture_output=True, text=True).stdout.strip()
    dependencies = [line.strip().split(" (compatibility version", 1)[0].split(" (current version", 1)[0]
                    for line in libraries if line.strip()]
    rpaths: list[str] = []
    command = minos = dylib_id = ""
    for line in commands.splitlines():
        value = line.strip()
        if value.startswith("cmd "):
            command = value[4:]
        elif command == "LC_RPATH" and value.startswith("path "):
            rpaths.append(value[5:].split(" (offset ", 1)[0])
        elif command == "LC_ID_DYLIB" and value.startswith("name "):
            dylib_id = value[5:].split(" (offset ", 1)[0]
        elif command == "LC_BUILD_VERSION" and value.startswith("minos "):
            minos = value[6:]
        elif command == "LC_VERSION_MIN_MACOSX" and value.startswith("version "):
            minos = value[8:]
    if dependencies and dependencies[0] == dylib_id:
        dependencies.pop(0)
    return NativeInfo(tuple(dependencies), tuple(rpaths), architectures, minos)


def _issue(kind: str, app: Path, path: Path, detail: str) -> dict[str, str]:
    try:
        relative = path.relative_to(app).as_posix()
    except ValueError:
        relative = str(path)
    return {"kind": kind, "path": relative, "detail": detail}


def _inside(app: Path, path: Path, *, directory: bool = False) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(app.resolve(strict=True))
        if resolved.is_dir() if directory else resolved.is_file():
            return resolved
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _token(value: str, loader: Path, executable: Path) -> Path | None:
    if value == "@loader_path" or value.startswith("@loader_path/"):
        return loader.parent / value.removeprefix("@loader_path").lstrip("/")
    if value == "@executable_path" or value.startswith("@executable_path/"):
        return executable.parent / value.removeprefix("@executable_path").lstrip("/")
    return None


def _rpath_base(value: str, loader: Path, executable: Path) -> Path | None:
    expanded = _token(value, loader, executable)
    if expanded is not None:
        return expanded.resolve(strict=False)
    if value.startswith("/"):
        return Path(value)
    return None


def _system_path(value: str | Path) -> bool:
    """Recognize system paths only after lexical and symlink normalization."""
    path = Path(value)
    if not path.is_absolute():
        return False
    try:
        normalized = os.path.normpath(str(path))
        canonical = str(path.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return any(normalized.startswith(prefix) for prefix in SYSTEM) and any(
        canonical.startswith(prefix) for prefix in SYSTEM
    )


def _unique(violations: list[dict[str, str]]) -> list[dict[str, str]]:
    return list({(item["kind"], item["path"], item["detail"]): item for item in violations}.values())


def _inspect_native_dependencies(app: Path, executable: Path, objects: list[Path]) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    info: dict[Path, NativeInfo] = {}
    original = {path.resolve(): path for path in objects}
    for path in objects:
        try:
            info[path.resolve()] = _native_metadata(path)
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            violations.append(_issue("native_metadata", app, path, str(error)))
    for resolved, item in info.items():
        path = original[resolved]
        if "arm64" not in item.architectures.split():
            violations.append(_issue("architecture", app, path, f"missing arm64 slice: {item.architectures}"))
        if not item.minos:
            violations.append(_issue("minimum_os", app, path, "missing macOS deployment target"))
    reached: set[Path] = set()
    visited: set[tuple[Path, tuple[tuple[Path, bool], ...]]] = set()

    def walk(loader: Path, inherited: tuple[tuple[Path, bool], ...]) -> None:
        resolved = loader.resolve()
        if resolved not in info or (resolved, inherited) in visited:
            return
        loader = original[resolved]
        visited.add((resolved, inherited))
        reached.add(resolved)
        item = info[resolved]
        paths = tuple((base, rpath.startswith("/")) for rpath in item.rpaths
                      if (base := _rpath_base(rpath, loader, executable)) is not None) + inherited
        # Unused runpaths may be historical or optional. Required loads below
        # still must resolve to an existing in-bundle or system object.
        for dependency in item.dependencies:
            if _system_path(dependency):
                continue
            if dependency.startswith("@rpath/"):
                found = None
                shadow = None
                for base, literal_absolute in paths:
                    target = base / dependency[len("@rpath/"):]
                    if _system_path(target):
                        found = target  # dyld shared-cache entries need not exist on disk.
                        break
                    if literal_absolute:
                        shadow = target
                        break  # Absolute app or private-build runpath cannot relocate.
                    try:
                        target.resolve(strict=False).relative_to(app.resolve(strict=True))
                    except (OSError, RuntimeError, ValueError):
                        shadow = target
                        break  # Earlier private search path could shadow later bundled code.
                    if _inside(app, target) is not None:
                        found = target
                        break
                if shadow is not None:
                    violations.append(_issue("dependency", app, loader,
                                             f"external or nonrelocatable @rpath search: {shadow}"))
                    continue
                if found is not None and _system_path(found):
                    continue
            else:
                found = _token(dependency, loader, executable)
                if found is not None and _inside(app, found) is None:
                    found = None
            if found is None:
                violations.append(_issue("dependency", app, loader, f"unresolved or external: {dependency}"))
            elif found.resolve() not in info:
                violations.append(_issue("dependency", app, loader, f"not a checked Mach-O: {dependency}"))
            else:
                walk(found, paths)

    walk(executable, ())
    # PyInstaller loads extensions dynamically, so inspect disconnected objects too.
    main_rpaths = tuple((base, rpath.startswith("/"))
                        for rpath in info.get(executable.resolve(), NativeInfo((), (), "", "")).rpaths
                        if (base := _rpath_base(rpath, executable, executable)) is not None)
    for path in objects:
        if path.resolve() not in reached:
            walk(path, main_rpaths)
    return _unique(violations)


def _inspect_scripts(app: Path) -> list[dict[str, str]]:
    violations = []
    exiftool = app / RUNTIME / "exiftool/exiftool"
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
            continue
        with path.open("rb") as stream:
            first = stream.readline(4096)
        if not first.startswith(b"#!"):
            continue
        try:
            words = shlex.split(first[2:].decode("utf-8").strip())
        except (UnicodeDecodeError, ValueError):
            words = []
        if not words:
            violations.append(_issue("script", app, path, "invalid shebang"))
            continue
        interpreter = words[0]
        if path == exiftool and words == ["/usr/bin/env", "perl"]:
            # Only this manifest-bound script is explicitly passed to bundled Perl.
            continue
        if _system_path(interpreter) or interpreter in ("/bin/sh", "/bin/bash"):
            continue
        violations.append(_issue("script", app, path, f"external or nonrelocatable interpreter: {interpreter}"))
    return violations


def _inspect_perl_paths(app: Path, perl: Path) -> list[dict[str, str]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PERL")}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    try:
        output = subprocess.run([str(perl), "-e", 'print join("\\n", @INC)'], check=True,
                                capture_output=True, text=True, env=env).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        return [_issue("perl_module_path", app, perl, f"@INC query failed: {error}")]
    return [_issue("perl_module_path", app, perl, f"@INC outside bundle/system: {line}")
            for line in output.splitlines() if not _system_path(line)
            and _inside(app, Path(line), directory=True) is None]


def _inspect_notices(app: Path) -> list[dict[str, str]]:
    destination = app / "Contents/Resources/legal"
    try:
        expected = notice_payload(ROOT)
        actual = notice_payload(destination)
        if actual != expected:
            raise CleanWheelBuildError("bundle notices differ from reviewed source")
    except CleanWheelBuildError as error:
        return [_issue("notice", app, destination, str(error))]
    return []


def inspect_bundle(app: Path) -> list[dict[str, str]]:
    app = Path(app)
    if app.is_symlink() or not app.is_dir() or app.suffix != ".app":
        return [_issue("bundle", app, app, "missing regular .app directory")]
    executable = app / "Contents/MacOS/选片APP"
    violations: list[dict[str, str]] = []
    for path in app.rglob("*"):
        if path.is_symlink() and _inside(app, path) is None and _inside(app, path, directory=True) is None:
            violations.append(_issue("symlink", app, path, f"dangling or external: {os.readlink(path)}"))
    for relative in (
        "Contents/Resources/src/xuanpian/ui/styles.qss",
        "Contents/Resources/src/xuanpian/resources/icons/app-icon.png",
        "Contents/Resources/build-provenance.json",
        "Contents/Resources/metadata-runtime/manifest.json",
        "Contents/Resources/src/xuanpian/resources/models/manifest.json",
    ):
        path = app / relative
        if not path.is_file() or path.is_symlink():
            violations.append(_issue("resource", app, path, "missing regular resource"))
    violations.extend(_inspect_models(app))
    violations.extend(_inspect_notices(app))
    try:
        resolve_metadata_runtime(bundle_root=app / "Contents/Resources", development_executable=None)
    except RuntimeUnavailable as error:
        violations.append(_issue("runtime", app, app / RUNTIME, error.reason))
    native = []
    for path in app.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MAGICS:
                native.append(path)
    if executable in native and os.access(executable, os.X_OK):
        violations.extend(_inspect_native_dependencies(app, executable, native))
    else:
        violations.append(_issue("executable", app, executable, "missing executable Mach-O"))
    violations.extend(_inspect_scripts(app))
    perl = app / RUNTIME / "perl/bin/perl"
    if perl.is_file() and os.access(perl, os.X_OK):
        violations.extend(_inspect_perl_paths(app, perl))
    violations.extend(_inspect_identity(app, native))
    return _unique(violations)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_models(app: Path) -> list[dict[str, str]]:
    models = app / MODELS
    violations = []
    try:
        data = json.loads((models / "manifest.json").read_text(encoding="utf-8"))
        expected = json.loads((ROOT / "src/xuanpian/resources/models/manifest.json").read_text(encoding="utf-8"))
        if data != expected or data["schema_version"] != 1 or not isinstance(data["models"], dict):
            raise ValueError("model manifest differs from pinned source manifest")
        for entry in data["models"].values():
            name = entry["filename"]
            if Path(name).name != name:
                raise ValueError("nonlocal model filename")
            path = models / name
            if not path.is_file() or path.is_symlink() or _sha256(path) != entry["sha256"]:
                violations.append(_issue("model", app, path, "missing or checksum mismatch"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        violations.append(_issue("model_manifest", app, models / "manifest.json", str(error)))
    return violations


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _inspect_identity(app: Path, native: list[Path]) -> list[dict[str, str]]:
    violations = []
    plist_path = app / "Contents/Info.plist"
    provenance_path = app / "Contents/Resources/build-provenance.json"
    try:
        plist = plistlib.loads(plist_path.read_bytes())
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        version = project_version()
        if (plist.get("CFBundleShortVersionString") != version or plist.get("CFBundleVersion") != version
                or provenance.get("app_version") != version):
            violations.append(_issue("version", app, plist_path, "bundle/provenance differs from project version"))
        floor = plist.get("LSMinimumSystemVersion")
        if not isinstance(floor, str) or not re.fullmatch(r"\d+(?:\.\d+)+", floor):
            violations.append(_issue("minimum_os", app, plist_path, "missing valid macOS floor"))
        else:
            for path in native:
                try:
                    minos = _native_metadata(path).minos
                    if minos and _version(minos) > _version(floor):
                        violations.append(_issue("minimum_os", app, path, f"native minOS {minos} > bundle {floor}"))
                except (OSError, subprocess.CalledProcessError, ValueError):
                    pass  # Reported by the native metadata pass.
        if provenance.get("source_tree_dirty") is not False or not re.fullmatch(r"[0-9a-f]{40}", str(provenance.get("source_revision", ""))):
            violations.append(_issue("provenance", app, provenance_path, "source must be a clean commit"))
        if provenance.get("signing") != "local-ad-hoc" or provenance.get("architecture") != "arm64":
            violations.append(_issue("provenance", app, provenance_path, "not arm64 local ad-hoc"))
        if provenance.get("requirements_lock_sha256") != _sha256(LOCK):
            violations.append(_issue("provenance", app, provenance_path, "lock digest mismatch"))
        if provenance.get("runtime_final_manifest_sha256") != _sha256(app / RUNTIME / "manifest.json"):
            violations.append(_issue("provenance", app, provenance_path, "final runtime digest mismatch"))
        for key, value in provenance.items():
            if isinstance(value, str) and value.startswith(("/Users/", "/opt/homebrew/", "/private/var/")):
                violations.append(_issue("provenance", app, provenance_path, f"private path in {key}"))
    except (OSError, ValueError, KeyError, TypeError) as error:
        violations.append(_issue("identity", app, plist_path, str(error)))
    try:
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                       check=True, capture_output=True, text=True)
        result = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", str(app)],
                                check=True, capture_output=True, text=True)
        if "Signature=adhoc" not in result.stderr or "TeamIdentifier=not set" not in result.stderr:
            violations.append(_issue("signature", app, app, "not locally ad-hoc signed"))
    except (OSError, subprocess.CalledProcessError) as error:
        violations.append(_issue("signature", app, app, str(error)))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    violations = inspect_bundle(args.app)
    print(json.dumps({"status": "FAIL" if violations else "PASS", "violations": violations},
                     indent=2, ensure_ascii=False))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
