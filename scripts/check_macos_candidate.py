"""Exercise only synthetic data in a native, locally ad-hoc-signed Mac candidate."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing as mp
import multiprocessing.spawn
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.build_metadata_runtime import _require_native_host
from scripts.build_macos import macos_lock, project_version

PROBE_MODULES = (
    "PIL", "numpy", "onnxruntime", "src.xuanpian.services.analysis_service",
    "src.xuanpian.store.project_store", "src.xuanpian.engine.technical_analyzer",
    "src.xuanpian.tasks.contracts",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def abort_frozen_workers(pool: ProcessPoolExecutor) -> None:
    """Stop only this Python-3.11 executor's owned handles before shutdown."""
    workers = list((pool._processes or {}).values())
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
    for worker in workers:
        worker.join(timeout=5)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
        if worker.is_alive():
            raise RuntimeError(f"owned frozen worker did not stop: {worker.pid}")


def validate_frozen_probe(result: dict, frameworks: Path) -> None:
    modules = result.get("modules", {})
    app = frameworks.parent.parent.resolve()
    if set(modules) != set(PROBE_MODULES):
        raise ValueError("frozen module identities missing or unexpected")
    for name, location in modules.items():
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise ValueError(f"invalid frozen module path: {name}")
        if not Path(location).resolve().is_relative_to(app):
            raise ValueError(f"frozen module outside app: {name}: {location}")
    if (result.get("pillow") != "13.0.0" or result.get("project_store") != "PASS"
            or result.get("algorithm") != "technical-v8"
            or result.get("portrait_assessed") is not True):
        raise ValueError(f"frozen engine probe failed: {result}")


def frozen_analysis(executable: Path, frameworks: Path, photo: Path) -> dict:
    """Use the bundled POSIX executable for both spawn and resource tracking."""
    # The bootloader canonicalizes _MEIPASS. Match its importer root rather than
    # injecting macOS's /var alias, which falls through to namespace packages.
    canonical_frameworks = frameworks.resolve()
    original_preparation = mp.spawn.get_preparation_data
    original_mp_executable, original_executable = mp.spawn.get_executable(), sys.executable
    missing = object()
    original_frozen = getattr(sys, "frozen", missing)
    pool = None
    failed = True

    def preparation(name):
        data = original_preparation(name)
        data.pop("init_main_from_path", None)
        data.pop("init_main_from_name", None)
        data["sys_path"] = [str(canonical_frameworks), str(canonical_frameworks / "base_library.zip")]
        return data

    try:
        mp.spawn.get_preparation_data = preparation
        mp.set_executable(str(executable))
        sys.executable = str(executable)
        sys.frozen = True
        pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
        version = pool.submit(eval, "__import__('PIL').__version__").result(timeout=60)
        module_expression = ("{name: __import__(name, fromlist=['__file__']).__file__ "
                             f"for name in {PROBE_MODULES!r}" + "}")
        modules = pool.submit(eval, module_expression).result(timeout=60)
        project = photo.parent / f"冻结项目-{uuid.uuid4().hex}.sqlite3"
        expression = ("__import__('src.xuanpian.store.project_store', fromlist=['ProjectStore'])"
                      f".ProjectStore.open({str(project)!r}).close()")
        pool.submit(eval, expression).result(timeout=60)
        if not project.is_file():
            raise RuntimeError("frozen project store did not create its database")
        from src.xuanpian.services.analysis_service import PhotoAnalysisRequest, analyze_photo
        outcome = pool.submit(analyze_photo, PhotoAnalysisRequest(
            "synthetic", str(photo), ("technical",))).result(timeout=90).outcomes[0]
        if outcome.failure is not None:
            raise RuntimeError(f"frozen analysis failed: {outcome.failure}")
        result = {"pillow": version, "modules": modules, "project_store": "PASS",
                  "algorithm": outcome.result.algorithm_version,
                  "portrait_assessed": outcome.result.portrait_assessed}
        validate_frozen_probe(result, frameworks)
        failed = False
        return result
    finally:
        try:
            if pool is not None:
                if failed:
                    abort_frozen_workers(pool)
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            mp.spawn.get_preparation_data = original_preparation
            mp.set_executable(original_mp_executable)
            sys.executable = original_executable
            if original_frozen is missing:
                del sys.frozen
            else:
                sys.frozen = original_frozen


def boot(executable: Path, synthetic_home: Path) -> None:
    synthetic_home.mkdir()
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       HOME=str(synthetic_home), CFFIXED_USER_HOME=str(synthetic_home))
    # File-backed output cannot deadlock startup on a full stdout/stderr pipe.
    with TemporaryDirectory(prefix="xuanpian-boot-log-") as temporary:
        with (Path(temporary) / "boot.log").open("w+b") as log:
            process = subprocess.Popen([str(executable)], env=environment, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(f"startup exit {process.returncode}: {log.read()[-4000:]!r}")
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.seek(0)
            if b"styles.qss not found" in log.read():
                raise RuntimeError("frozen stylesheet missing")


def _stop_inspection_group(process: subprocess.Popen) -> None:
    """Signal only the private session created for this inspector."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        process.poll()  # Reap our parent even when descendants keep its group alive.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_inspection(command: list[str], *, timeout: float = 180) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        _stop_inspection_group(process)
        process.communicate(timeout=5)
        raise
    if process.returncode:
        _stop_inspection_group(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _exercise_app(app: Path, photo: Path, *, architecture: str, home: Path) -> dict:
    inspection = _run_inspection(
        [sys.executable, str(ROOT / "scripts/verify_macos_bundle.py"),
         "--architecture", architecture, str(app)],
        timeout=180)
    if inspection.returncode:
        raise ValueError(f"bundle inspection failed: {inspection.stdout[-4000:]} "
                         f"{inspection.stderr[-4000:]}")
    evidence = json.loads(inspection.stdout)
    if evidence.get("status") != "PASS" or evidence.get("violations") != []:
        raise ValueError(f"bundle inspection did not return successful evidence: {evidence}")
    boot(app / "Contents/MacOS/选片APP", home)
    result = frozen_analysis(app / "Contents/MacOS/选片APP", app / "Contents/Frameworks", photo)
    from src.xuanpian.engine.metadata_runtime import resolve_metadata_runtime
    from src.xuanpian.engine.af_metadata import run_metadata_command
    runtime = resolve_metadata_runtime(bundle_root=app / "Contents/Resources",
                                       development_executable=None)
    metadata = run_metadata_command(runtime.command_prefix,
        ["-j", "-n", "-ImageWidth", "-ImageHeight"], str(photo), runtime.environment, timeout=15)
    if metadata.returncode:
        raise RuntimeError(f"bundled ExifTool failed: {metadata.stderr!r}")
    record = json.loads(metadata.stdout)[0]
    if (record.get("ImageWidth"), record.get("ImageHeight")) != (640, 480):
        raise ValueError(f"bundled ExifTool dimensions differ: {record}")
    result["metadata"] = "PASS"
    # Preserve containment evidence without leaking runner-specific absolute paths.
    result["modules"] = {name: Path(path).resolve().relative_to(app.resolve()).as_posix()
                         for name, path in result["modules"].items()}
    return result


def relocate_dmg(dmg: Path, temporary: Path) -> Path:
    """Detach only our owned, read-only mount, including on copy failure."""
    mount = temporary / "owned-mount"
    mount.mkdir()
    destination = temporary / "中文 安装位置"
    destination.mkdir()
    app = destination / "选片APP.app"
    attached = False
    try:
        subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
                        "-mountpoint", str(mount), str(dmg)], check=True, timeout=90,
                       capture_output=True)
        attached = True
        subprocess.run(["/usr/bin/ditto", "--rsrc", "--extattr", "--acl",
                        str(mount / app.name), str(app)], check=True, timeout=120)
    finally:
        if attached or os.path.ismount(mount):
            try:
                subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)],
                               check=True, timeout=60, capture_output=True)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                subprocess.run(["/usr/bin/hdiutil", "detach", "-force", str(mount)],
                               check=True, timeout=30, capture_output=True)
    return app


def check(candidate: Path, *, architecture: str = "x86_64") -> dict:
    macos_lock(architecture)
    _require_native_host(architecture)
    candidate = Path(candidate)
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("candidate must be a regular directory")
    output = candidate / "verification.json"
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"prior verification exists: {output}")
    app = candidate / "选片APP.app"
    images = list(candidate.glob("*.dmg"))
    expected = f"XuanPian-{project_version()}-macos12-{architecture}-local-ad-hoc.dmg"
    if len(images) != 1 or images[0].name != expected:
        raise ValueError(f"exactly one selected-architecture DMG required: {expected}")
    dmg = images[0]
    sidecar = dmg.with_suffix(".dmg.sha256")
    if dmg.is_symlink() or not dmg.is_file() or sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError("DMG and checksum must be regular files")
    digest = _sha256(dmg)
    if sidecar.read_text(encoding="utf-8") != f"{digest}  {dmg.name}\n":
        raise ValueError("DMG checksum mismatch")
    if app.is_symlink() or not app.is_dir():
        raise ValueError("candidate app must be a regular directory")
    provenance = json.loads((app / "Contents/Resources/build-provenance.json").read_bytes())
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True, timeout=15).stdout.strip()
    if (not re.fullmatch("[0-9a-f]{40}", revision)
            or provenance.get("source_revision") != revision
            or provenance.get("source_tree_dirty") is not False):
        raise ValueError("candidate source revision/clean-tree provenance mismatch")
    from PIL import Image
    with TemporaryDirectory(prefix="xuanpian-macos-check-") as temporary:
        folder = Path(temporary) / "中文 合成照片"
        folder.mkdir()
        photo = folder / "合成照片.jpg"
        Image.new("RGB", (640, 480), (70, 100, 160)).save(photo)
        before = _sha256(photo)
        try:
            original = _exercise_app(app, photo, architecture=architecture,
                                     home=Path(temporary) / "original-home")
            subprocess.run(["/usr/bin/hdiutil", "verify", str(dmg)], check=True, timeout=120,
                           capture_output=True)
            relocated = relocate_dmg(dmg, Path(temporary))
            copied_provenance = relocated / "Contents/Resources/build-provenance.json"
            if copied_provenance.read_bytes() != (app / "Contents/Resources/build-provenance.json").read_bytes():
                raise ValueError("relocated provenance differs from original app")
            copied = _exercise_app(relocated, photo, architecture=architecture,
                                   home=Path(temporary) / "relocated-home")
        finally:
            if _sha256(photo) != before:
                raise ValueError("synthetic source photo was mutated")
    if _sha256(dmg) != digest:
        raise ValueError("DMG changed during verification")
    result = {"schema_version": 1, "status": "PASS", "architecture": architecture,
              "source_revision": revision, "dmg_sha256": digest, "dmg": dmg.name,
              "source_mutations": 0, "original": original, "relocated": copied,
              "signing": "local-ad-hoc", "rights_status": "unreviewed",
              "macos12_clean_machine_acceptance": "not-tested"}
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"), default="x86_64")
    args = parser.parse_args()
    print(json.dumps(check(args.candidate.absolute(), architecture=args.architecture)))
    return 0


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True)
    raise SystemExit(main())
