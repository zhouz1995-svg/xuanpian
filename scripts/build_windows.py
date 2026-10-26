"""Build a fresh Windows x64 onedir and per-user Inno Setup installer."""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_metadata_runtime import _sha256
from src.xuanpian.engine.metadata_runtime import resolve_metadata_runtime


def build_app(*, runtime_root: Path, output: Path, iscc: Path) -> Path:
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"output already exists: {output}")
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("native Windows x64 host required")
    if not iscc.is_file() or runtime_root.is_symlink():
        raise ValueError("missing compiler or invalid runtime")
    runtime_root, output, iscc = runtime_root.resolve(), output.absolute(), iscc.resolve()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, check=True, capture_output=True, text=True).stdout:
        raise ValueError("Windows build requires a clean source commit")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".windows-app-", dir=output.parent) as temp:
        stage = Path(temp)
        subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--distpath", str(stage / "dist"), "--workpath", str(stage / "work"),
            str(ROOT / "packaging/xuanpian-windows.spec")], cwd=ROOT, check=True)
        delivery = stage / "delivery"
        delivery.mkdir()
        app = delivery / "app"
        shutil.move(str(stage / "dist/XuanPian"), app)
        internal = app / "_internal"
        shutil.copytree(runtime_root, internal / "metadata-runtime")
        resolve_metadata_runtime(bundle_root=internal, development_executable=None)
        compiler = subprocess.run([str(iscc), "/?"], capture_output=True, text=True, errors="replace")
        provenance = {"schema_version": 1, "app_version": version, "architecture": "x86_64",
            "source_revision": revision, "source_tree_dirty": False,
            "python_version": platform.python_version(), "signing": "unsigned",
            "requirements_lock_sha256": _sha256(ROOT / "packaging/requirements-windows-x64.lock"),
            "runtime_manifest_sha256": _sha256(internal / "metadata-runtime/manifest.json"),
            "inno_compiler_sha256": _sha256(iscc), "inno_compiler_banner": compiler.stdout.splitlines()[:3]}
        (internal / "build-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
        inventory = {"schema_version": 1, "files": {p.relative_to(app).as_posix(): _sha256(p)
                      for p in sorted(app.rglob("*")) if p.is_file()}}
        (app / "bundle-files.json").write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
        from scripts.verify_windows_bundle import inspect_bundle
        if issues := inspect_bundle(app):
            raise ValueError(f"Windows bundle rejected: {issues}")
        subprocess.run([str(iscc), f"/DAppDir={app}", f"/DOutputDir={delivery}",
            f"/DProjectRoot={ROOT}", f"/DAppVersion={version}", str(ROOT / "packaging/windows-installer.iss")], check=True)
        installer = delivery / f"XuanPian-{version}-windows-x64-unsigned.exe"
        if not installer.is_file():
            raise ValueError("Inno did not create the expected installer")
        (delivery / (installer.name + ".sha256")).write_text(_sha256(installer) + "  " + installer.name + "\n", encoding="utf-8")
        delivery.rename(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iscc", type=Path, required=True)
    args = parser.parse_args()
    print(build_app(runtime_root=args.runtime, output=args.output, iscc=args.iscc))
