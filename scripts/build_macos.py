"""Build an isolated native macOS candidate without replacing prior output."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_metadata_runtime import _require_native_host  # noqa: E402

SPEC = ROOT / "packaging" / "xuanpian.spec"
LOCK = ROOT / "packaging" / "requirements-macos-arm64.lock"
APP_NAME = "选片APP.app"


def macos_lock(architecture: str) -> Path:
    if architecture not in {"arm64", "x86_64"}:
        raise ValueError(f"unsupported macOS architecture: {architecture}")
    return LOCK if architecture == "arm64" else ROOT / "packaging/requirements-macos-x86_64.lock"


def macos_runtime_sources(architecture: str) -> Path:
    macos_lock(architecture)
    name = "runtime-sources.json" if architecture == "arm64" else "runtime-sources-macos-x86_64.json"
    return ROOT / "packaging" / name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def project_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["version"]


def _regular_runtime_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("runtime root must be a regular directory")
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"runtime contains symlink: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"runtime contains unsupported entry: {path.relative_to(root)}")
    return sorted(files)


def stage_runtime(source: Path, destination: Path) -> None:
    """Copy the checked input into a fresh, non-cross-linked bundle directory."""
    _regular_runtime_files(source)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"runtime destination exists: {destination}")
    shutil.copytree(source, destination, symlinks=False)


def prune_runtime_tools(staged_runtime: Path) -> None:
    """Keep only the Perl executable used by the app, not developer CLI tools.

    Perl's installed helper scripts retain build-directory shebangs. They are
    unrelated to the manifest's explicit ``perl ... exiftool`` invocation and
    must not be shipped as broken executable entry points.
    """
    bin_dir = staged_runtime / "perl" / "bin"
    perl = bin_dir / "perl"
    if bin_dir.is_symlink() or not bin_dir.is_dir() or perl.is_symlink() or not perl.is_file():
        raise ValueError("staged Perl bin directory is invalid")
    for path in bin_dir.iterdir():
        if path == perl:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"unexpected Perl bin entry: {path.name}")
        path.unlink()


def refresh_runtime_manifest(root: Path) -> None:
    """Hash final payload bytes while preserving the verified source provenance."""
    files = _regular_runtime_files(root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if set(manifest) != {
        "schema_version", "versions", "command_prefix", "files", "source_manifest_sha256",
    }:
        raise ValueError("runtime manifest has unsupported schema")
    manifest["files"] = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in files if path != manifest_path
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _preflight(runtime_root: Path, output: Path, architecture: str = "arm64") -> None:
    source_manifest = macos_runtime_sources(architecture)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists: {output}")
    if not runtime_root.is_dir() or runtime_root.is_symlink():
        raise ValueError("runtime is missing or is not a regular directory")
    _require_native_host(architecture)
    _regular_runtime_files(runtime_root)
    _verify_input_runtime(runtime_root)
    manifest = json.loads((runtime_root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["source_manifest_sha256"] != _sha256(source_manifest):
        raise ValueError(f"runtime source manifest does not match {architecture} profile")


def _verify_input_runtime(runtime_root: Path) -> None:
    """Validate the existing resolver contract without weakening its root rule."""
    from src.xuanpian.engine.metadata_runtime import _bundle_runtime, _environment

    # The source directory may have a versioned basename. Use the checked schema/files
    # through a regular temporary copy named exactly as the frozen resolver expects.
    with tempfile.TemporaryDirectory(prefix="xuanpian-runtime-check-") as temporary:
        staged = Path(temporary) / "metadata-runtime"
        stage_runtime(runtime_root, staged)
        _bundle_runtime(Path(temporary), _environment())


def _native_files(root: Path) -> list[Path]:
    code: list[Path] = []
    for path in _regular_runtime_files(root):
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"}:
            code.append(path)
    return code


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _runtime_destination(app: Path) -> Path:
    return app / "Contents" / "Resources" / "metadata-runtime"


def _provenance(app: Path, runtime: Path, architecture: str = "arm64") -> None:
    lock = macos_lock(architecture)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()
    dirty = bool(subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout)
    sdk = subprocess.run(
        ["/usr/bin/xcrun", "--show-sdk-version"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
    provenance = {
        "schema_version": 1,
        "app_version": project_version(),
        "source_revision": revision,
        "source_tree_dirty": dirty,
        "python_version": platform.python_version(),
        "architecture": architecture,
        "macos_sdk": sdk,
        "requirements_lock": f"packaging/requirements-macos-{architecture}.lock",
        "requirements_lock_sha256": _sha256(lock),
        "runtime_source_manifest_sha256": manifest["source_manifest_sha256"],
        "runtime_final_manifest_sha256": _sha256(runtime / "manifest.json"),
        "signing": "local-ad-hoc",
    }
    (app / "Contents" / "Resources" / "build-provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def build_app(*, runtime_root: Path, output: Path, architecture: str = "arm64") -> Path:
    """Build and return a new candidate .app in ``output``."""
    runtime_root = Path(runtime_root)
    output = Path(output)
    _preflight(runtime_root, output, architecture=architecture)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".xuanpian-app-build-", dir=output.parent) as temporary:
        staging = Path(temporary)
        dist = staging / "dist"
        work = staging / "work"
        _run([
            sys.executable, "-m", "PyInstaller", str(SPEC),
            "--distpath", str(dist), "--workpath", str(work), "--noconfirm", "--clean",
        ])
        app = dist / APP_NAME
        if not app.is_dir() or app.is_symlink():
            raise RuntimeError("PyInstaller did not produce the expected .app")
        plist = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
        if plist["CFBundleShortVersionString"] != project_version():
            raise RuntimeError("bundle version differs from project version")

        # On macOS _MEIPASS is Contents/Frameworks. Keep data in Resources and
        # have the frozen caller select that exact sibling inside the same .app.
        # This avoids Frameworks nested-code signing of Perl text modules.
        framework_runtime = app / "Contents" / "Frameworks" / "metadata-runtime"
        resource_runtime = _runtime_destination(app)
        if framework_runtime.exists() or framework_runtime.is_symlink() or \
                resource_runtime.exists() or resource_runtime.is_symlink():
            raise RuntimeError("unexpected PyInstaller metadata runtime layout")
        stage_runtime(runtime_root, resource_runtime)
        prune_runtime_tools(resource_runtime)
        for native in _native_files(resource_runtime):
            _run(["/usr/bin/codesign", "--force", "--sign", "-", str(native)])
        refresh_runtime_manifest(resource_runtime)

        from src.xuanpian.engine.metadata_runtime import resolve_metadata_runtime
        resolve_metadata_runtime(bundle_root=app / "Contents" / "Resources", development_executable=None)
        _provenance(app, resource_runtime, architecture=architecture)
        _run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)])
        _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)])
        # Outer sealing must not rewrite runtime payload; prove it after signing.
        resolve_metadata_runtime(bundle_root=app / "Contents" / "Resources", development_executable=None)

        delivery = staging / "delivery"
        delivery.mkdir()
        app.rename(delivery / APP_NAME)
        if output.exists() or output.is_symlink():
            raise FileExistsError(f"output appeared during build: {output}")
        delivery.rename(output)
    resolve_metadata_runtime(bundle_root=output / APP_NAME / "Contents" / "Resources",
                             development_executable=None)
    return output / APP_NAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"), default="arm64")
    args = parser.parse_args()
    print(build_app(runtime_root=args.runtime, output=args.output, architecture=args.architecture))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
