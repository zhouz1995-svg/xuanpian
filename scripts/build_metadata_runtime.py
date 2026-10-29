#!/usr/bin/env python3
"""Build the pinned, relocatable Perl and ExifTool metadata runtime."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


_PERL_ENVIRONMENT_KEYS = (
    "PERL5LIB",
    "PERL5OPT",
    "PERLLIB",
    "PERL_LOCAL_LIB_ROOT",
    "PERL_MB_OPT",
    "PERL_MM_OPT",
)


def _absolute_output(path: Path) -> Path:
    return path.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, sha256: str) -> None:
    """Raise when *path* does not match the approved SHA-256 digest."""

    expected = sha256.lower()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise ValueError("invalid approved SHA-256 digest")
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(
            f"archive digest mismatch for {path.name}: expected {expected}, got {actual}"
        )


def _normalise_archive_path(
    path: PurePosixPath, *, base: PurePosixPath
) -> PurePosixPath:
    parts: list[str] = []
    for part in (*base.parts, *path.parts):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("archive path escapes destination")
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts)


def safe_extract(archive: Path, destination: Path) -> None:
    """Extract a tar archive without traversal, escaping links, or devices."""

    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"destination already exists: {destination}")

    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError(f"unsafe archive path: {member.name}")
            try:
                _normalise_archive_path(member_path, base=PurePosixPath())
            except ValueError as error:
                raise ValueError(f"unsafe archive path: {member.name}") from error
            if member.isdev() or member.isfifo():
                raise ValueError(f"unsupported archive entry: {member.name}")
            if member.issym() or member.islnk():
                link_path = PurePosixPath(member.linkname)
                if link_path.is_absolute():
                    raise ValueError(f"unsafe archive link: {member.name}")
                base = member_path.parent if member.issym() else PurePosixPath()
                try:
                    _normalise_archive_path(link_path, base=base)
                except ValueError as error:
                    raise ValueError(f"unsafe archive link: {member.name}") from error
            elif not (member.isdir() or member.isfile()):
                raise ValueError(f"unsupported archive entry: {member.name}")

        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".{destination.name}-extract-", dir=destination.parent
            )
        )
        try:
            source.extractall(temporary, members=members, filter="data")
            temporary.replace(destination)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def _load_source_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported source manifest schema_version")
    target = data.get("target")
    if target not in ({"platform": "macos", "architecture": "arm64"},
                      {"platform": "macos", "architecture": "x86_64"}):
        raise ValueError("source manifest target must be macOS arm64 or x86_64")
    build = data.get("build")
    deployment_target = build.get("deployment_target") if isinstance(build, dict) else None
    if (not isinstance(deployment_target, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", deployment_target)
            or tuple(map(int, deployment_target.split("."))) < (11, 0)):
        raise ValueError("build.deployment_target must be a macOS version >= 11.0")
    sources = data.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"perl", "exiftool"}:
        raise ValueError("source manifest must contain perl and exiftool sources")
    for name, source in sources.items():
        if not isinstance(source, dict):
            raise ValueError(f"invalid {name} source entry")
        for field in ("version", "url", "sha256"):
            if not isinstance(source.get(field), str) or not source[field]:
                raise ValueError(f"missing {name} source field: {field}")
    return data


def _clean_build_environment(sdkroot: str, deployment_target: str) -> dict[str, str]:
    environment = os.environ.copy()
    for key in _PERL_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.pop("DYLD_LIBRARY_PATH", None)
    environment.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
    environment.pop("LIBRARY_PATH", None)
    environment.pop("CPATH", None)
    for key in ("CFLAGS", "CPPFLAGS", "CXXFLAGS", "LDFLAGS", "CC", "CXX", "LD",
                "MAKEFLAGS", "MFLAGS", "ARCHFLAGS"):
        environment.pop(key, None)
    environment["SDKROOT"] = sdkroot
    # Perl's Darwin hints propagate this into compile and link flags, including
    # XS modules. Never silently inherit the build host's deployment target.
    environment["MACOSX_DEPLOYMENT_TARGET"] = deployment_target
    return environment


def _run(command: Sequence[str], *, cwd: Path, environment: dict[str, str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(list(command), cwd=cwd, env=environment, check=True)


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "xuanpian-runtime-builder/1"}
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("xb") as output:
            shutil.copyfileobj(response, output)


def _single_source_root(extracted: Path, source_name: str) -> Path:
    entries = list(extracted.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise ValueError(f"{source_name} archive must contain one top-level directory")
    return entries[0]


def _runtime_files(runtime: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(runtime.rglob("*")):
        if path.name == "manifest.json" or not path.is_file():
            continue
        relative = path.relative_to(runtime).as_posix()
        files[relative] = _sha256(path)
    return files


def _is_translated_process() -> bool:
    """Query Darwin lazily; only ENOENT is an accepted native fallback."""
    sysctl = ctypes.CDLL(None, use_errno=True).sysctlbyname
    sysctl.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                      ctypes.c_void_p, ctypes.c_size_t]
    sysctl.restype = ctypes.c_int
    translated = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(translated))
    ctypes.set_errno(0)
    if sysctl(b"sysctl.proc_translated", ctypes.byref(translated), ctypes.byref(size), None, 0) != 0:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return False
        raise OSError(error, "sysctl.proc_translated query failed")
    if size.value != ctypes.sizeof(translated) or translated.value not in (0, 1):
        raise RuntimeError("invalid process translation query result")
    return bool(translated.value)


def _require_native_host(architecture: str) -> None:
    if architecture not in {"arm64", "x86_64"}:
        raise ValueError(f"unsupported macOS architecture: {architecture}")
    if platform.system() != "Darwin" or platform.machine() != architecture:
        raise RuntimeError(f"native macOS {architecture} host required")
    if _is_translated_process():
        raise RuntimeError("Rosetta-translated processes cannot build native macOS candidates")


def _validate_host(build: dict[str, Any], architecture: str = "arm64") -> tuple[str, str]:
    _require_native_host(architecture)
    sdk = build.get("sdk")
    compiler = build.get("compiler")
    if not isinstance(sdk, dict) or not isinstance(compiler, dict):
        raise ValueError("source manifest must describe the compiler and SDK")

    sdk_version = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", str(sdk.get("name")), "--show-sdk-version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if sdk_version != sdk.get("version"):
        raise RuntimeError(
            f"SDK version mismatch: expected {sdk.get('version')}, got {sdk_version}"
        )

    sdk_path = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", str(sdk.get("name")), "--show-sdk-path"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not Path(sdk_path).is_dir():
        raise RuntimeError(f"SDK path does not exist: {sdk_path}")

    compiler_path = subprocess.run(
        ["/usr/bin/xcrun", "--sdk", str(sdk.get("name")), "--find", "clang"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    compiler_version = subprocess.run(
        [compiler_path, "--version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    if compiler_version != compiler.get("version"):
        expected = compiler.get("version")
        raise RuntimeError(
            f"compiler version mismatch: expected {expected}, got {compiler_version}"
        )
    return compiler_path, sdk_path


def build_runtime(source_manifest: Path, output: Path) -> Path:
    """Build a verified metadata runtime and return its generated manifest."""

    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists: {output}")
    output = _absolute_output(output)

    manifest_bytes = source_manifest.read_bytes()
    source_config = _load_source_manifest(source_manifest)
    architecture = source_config["target"]["architecture"]
    compiler_path, sdk_path = _validate_host(source_config["build"], architecture=architecture)
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = _clean_build_environment(sdk_path, source_config["build"]["deployment_target"])

    with tempfile.TemporaryDirectory(
        prefix=".metadata-runtime-build-", dir=output.parent
    ) as temporary_name:
        temporary = Path(temporary_name)
        downloads = temporary / "downloads"
        sources = temporary / "sources"
        runtime = temporary / "runtime"
        downloads.mkdir()
        sources.mkdir()
        runtime.mkdir()

        extracted: dict[str, Path] = {}
        for name in ("perl", "exiftool"):
            source = source_config["sources"][name]
            archive = downloads / f"{name}.tar"
            _download(source["url"], archive)
            verify_archive(archive, source["sha256"])
            extracted_dir = sources / name
            safe_extract(archive, extracted_dir)
            extracted[name] = _single_source_root(extracted_dir, name)

        perl_source = extracted["perl"]
        perl_root = runtime / "perl"
        library_options = ["-Uloclibpth", "-Dlibpth=/usr/lib"]
        if architecture == "x86_64":
            # Configure adds glibpth/compiler paths even with an explicit libpth.
            # Empty -U paths prevent local -I/-L flags; a space (not empty) keeps
            # libswanted from falling back to an automatic optional-library scan.
            library_options = [
                "-Uloclibpth=", "-Ulocincpth=", "-Dglibpth=/usr/lib", "-Dlibpth=/usr/lib",
                "-Dlibswanted= ", "-Dlibs= ",
            ]
        configure = [
            "/usr/bin/arch",
            f"-{architecture}",
            "/bin/sh",
            "Configure",
            "-des",
            f"-Dprefix={perl_root}",
            "-Duserelocatableinc",
            "-Uuseshrplib",
            f"-Dcc={compiler_path}",
            *library_options,
            "-Dman1dir=none",
            "-Dman3dir=none",
        ]
        _run(configure, cwd=perl_source, environment=environment)
        _run(
            ["/usr/bin/arch", f"-{architecture}", "/usr/bin/make", "-j4"],
            cwd=perl_source,
            environment=environment,
        )
        _run(
            ["/usr/bin/arch", f"-{architecture}", "/usr/bin/make", "test"],
            cwd=perl_source,
            environment=environment,
        )
        _run(
            ["/usr/bin/arch", f"-{architecture}", "/usr/bin/make", "install"],
            cwd=perl_source,
            environment=environment,
        )

        perl = perl_root / "bin" / "perl"
        exiftool_source = extracted["exiftool"]
        _run(["/usr/bin/arch", f"-{architecture}", str(perl), "Makefile.PL"],
             cwd=exiftool_source, environment=environment)
        _run(["/usr/bin/arch", f"-{architecture}", "/usr/bin/make", "test"],
             cwd=exiftool_source, environment=environment)

        exiftool_root = runtime / "exiftool"
        exiftool_root.mkdir()
        shutil.copy2(exiftool_source / "exiftool", exiftool_root / "exiftool")
        shutil.copytree(exiftool_source / "lib", exiftool_root / "lib")

        licenses = runtime / "licenses"
        (licenses / "perl").mkdir(parents=True)
        (licenses / "exiftool").mkdir()
        shutil.copy2(perl_source / "Artistic", licenses / "perl" / "Artistic")
        shutil.copy2(perl_source / "Copying", licenses / "perl" / "Copying")
        shutil.copy2(exiftool_source / "README", licenses / "exiftool" / "README")

        version = source_config["sources"]["perl"]["version"]
        arch_libraries = sorted((perl_root / "lib" / version).glob("darwin-*"))
        if len(arch_libraries) != 1 or not arch_libraries[0].is_dir():
            raise RuntimeError("unable to identify Perl architecture module directory")

        generated = {
            "schema_version": 1,
            "versions": {
                "perl": version,
                "exiftool": source_config["sources"]["exiftool"]["version"],
            },
            "command_prefix": {
                "perl": "perl/bin/perl",
                "module_dirs": [
                    arch_libraries[0].relative_to(runtime).as_posix(),
                    (perl_root / "lib" / version).relative_to(runtime).as_posix(),
                    "exiftool/lib",
                ],
                "script": "exiftool/exiftool",
                "fixed_args": ["-config", ""],
            },
            "files": _runtime_files(runtime),
            "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        generated_path = runtime / "manifest.json"
        generated_path.write_text(
            json.dumps(generated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        runtime.replace(output)

    return output / "manifest.json"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"))
    arguments = parser.parse_args(argv)
    if arguments.architecture is not None:
        target = _load_source_manifest(arguments.manifest)["target"]["architecture"]
        if arguments.architecture != target:
            parser.error("--architecture differs from source manifest target")
    generated = build_runtime(arguments.manifest, arguments.output)
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
