#!/usr/bin/env python3
"""Build the pinned, relocatable Perl and ExifTool metadata runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
    if target != {"platform": "macos", "architecture": "arm64"}:
        raise ValueError("source manifest target must be macOS arm64")
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


def _clean_build_environment(sdkroot: str) -> dict[str, str]:
    environment = os.environ.copy()
    for key in _PERL_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment.pop("DYLD_LIBRARY_PATH", None)
    environment.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
    environment.pop("LIBRARY_PATH", None)
    environment.pop("CPATH", None)
    environment["SDKROOT"] = sdkroot
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


def _validate_host(build: dict[str, Any]) -> tuple[str, str]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("metadata runtime builds are supported only on macOS arm64")
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
    compiler_path, sdk_path = _validate_host(source_config["build"])
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = _clean_build_environment(sdk_path)

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
        configure = [
            "/usr/bin/arch",
            "-arm64",
            "/bin/sh",
            "Configure",
            "-des",
            f"-Dprefix={perl_root}",
            "-Duserelocatableinc",
            "-Uuseshrplib",
            f"-Dcc={compiler_path}",
            "-Uloclibpth",
            "-Dlibpth=/usr/lib",
            "-Dman1dir=none",
            "-Dman3dir=none",
        ]
        _run(configure, cwd=perl_source, environment=environment)
        _run(
            ["/usr/bin/arch", "-arm64", "/usr/bin/make", "-j4"],
            cwd=perl_source,
            environment=environment,
        )
        _run(
            ["/usr/bin/arch", "-arm64", "/usr/bin/make", "test"],
            cwd=perl_source,
            environment=environment,
        )
        _run(
            ["/usr/bin/arch", "-arm64", "/usr/bin/make", "install"],
            cwd=perl_source,
            environment=environment,
        )

        perl = perl_root / "bin" / "perl"
        exiftool_source = extracted["exiftool"]
        _run([str(perl), "Makefile.PL"], cwd=exiftool_source, environment=environment)
        _run(["/usr/bin/make", "test"], cwd=exiftool_source, environment=environment)

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
    arguments = parser.parse_args(argv)
    generated = build_runtime(arguments.manifest, arguments.output)
    print(generated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
