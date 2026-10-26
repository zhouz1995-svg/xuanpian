"""Exercise an unsigned candidate on a disposable Windows runner, not user photos."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing as mp
import multiprocessing.spawn
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.verify_windows_bundle import inspect_bundle
from src.xuanpian.engine.metadata_runtime import resolve_metadata_runtime
from src.xuanpian.services.analysis_service import PhotoAnalysisRequest, analyze_photo


def boot(executable: Path):
    environment = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    process = subprocess.Popen([str(executable)], env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                out, err = process.communicate()
                raise RuntimeError(f"startup exit {process.returncode}: {err[-2000:]!r}")
            time.sleep(0.2)
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=15)


def frozen_analysis(app: Path, photo: Path):
    print(f"Frozen analysis: {app}", flush=True)
    internal = app / "_internal"
    original = mp.spawn.get_preparation_data
    original_executable = mp.spawn.get_executable()
    original_frozen = getattr(sys, "frozen", None)
    def preparation(name):
        data = original(name)
        data.pop("init_main_from_path", None)
        data.pop("init_main_from_name", None)
        data["sys_path"] = [str(internal), str(internal / "base_library.zip")]
        return data
    mp.spawn.get_preparation_data = preparation
    mp.set_executable(str(app / "XuanPian.exe"))
    sys.frozen = True
    try:
        with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as pool:
            version = pool.submit(eval, "__import__('PIL').__version__").result(timeout=60)
            print(f"Frozen Pillow loaded: {version}", flush=True)
            assert version == "13.0.0", version
            result = pool.submit(analyze_photo, PhotoAnalysisRequest("synthetic", str(photo), ("technical",))).result(timeout=60)
            outcome = result.outcomes[0]
            assert outcome.failure is None, outcome.failure
            assert outcome.result.algorithm_version == "technical-v8"
            assert outcome.result.portrait_assessed is True
            return {"pillow": version, "algorithm": outcome.result.algorithm_version}
    finally:
        mp.spawn.get_preparation_data = original
        mp.set_executable(original_executable)
        if original_frozen is None:
            del sys.frozen
        else:
            sys.frozen = original_frozen


def check(candidate: Path):
    if sys.platform != "win32":
        raise RuntimeError("native Windows verification required")
    app = candidate / "app"
    if errors := inspect_bundle(app):
        raise RuntimeError(errors)
    installers = list(candidate.glob("XuanPian-*-windows-x64-unsigned.exe"))
    if len(installers) != 1:
        raise ValueError("one installer required")
    installer = installers[0]
    from PIL import Image
    from PyQt6.QtCore import QCoreApplication, QStandardPaths
    core = QCoreApplication.instance() or QCoreApplication([])
    core.setOrganizationName("Xuanpian")
    core.setApplicationName("Xuanpian")
    data_root = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    data_root.mkdir(parents=True, exist_ok=True)
    sentinel = data_root / ("installer-preservation-" + uuid.uuid4().hex + ".txt")
    sentinel.write_text("preserve project data", encoding="utf-8")
    try:
        with TemporaryDirectory(prefix="xuanpian-windows-check-") as temporary:
            folder = Path(temporary) / "中文 路径"
            folder.mkdir()
            photo = folder / "合成照片.jpg"
            Image.new("RGB", (640, 480), (70, 100, 160)).save(photo)
            before = hashlib.sha256(photo.read_bytes()).hexdigest()
            print("Stage: boot onedir", flush=True)
            boot(app / "XuanPian.exe")
            result = frozen_analysis(app, photo)
            print("Stage: bundled ExifTool", flush=True)
            runtime = resolve_metadata_runtime(bundle_root=app / "_internal", development_executable=None)
            metadata = subprocess.run([*runtime.command_prefix, "-j", "-n", "-ImageWidth", "-ImageHeight", str(photo)],
                capture_output=True, text=True, encoding="utf-8", env=dict(runtime.environment), check=True, timeout=15)
            record = json.loads(metadata.stdout)[0]
            assert (record["ImageWidth"], record["ImageHeight"]) == (640, 480)
            installed = folder / "安装位置"
            print("Stage: silent install", flush=True)
            subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                            f"/DIR={installed}", "/LANG=chinesesimp"], check=True, timeout=180)
            print("Stage: inspect installed files", flush=True)
            if errors := inspect_bundle(installed):
                raise RuntimeError(errors)
            print("Stage: boot installed app", flush=True)
            boot(installed / "XuanPian.exe")
            frozen_analysis(installed, photo)
            uninstall = installed / "unins000.exe"
            print("Stage: uninstall and preservation checks", flush=True)
            subprocess.run([str(uninstall), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], check=True, timeout=120)
            assert not (installed / "XuanPian.exe").exists()
            assert sentinel.read_text(encoding="utf-8") == "preserve project data"
            assert hashlib.sha256(photo.read_bytes()).hexdigest() == before
            result.update({"status": "PASS", "metadata": "PASS", "install_uninstall": "PASS",
                           "project_sentinel_preserved": True, "source_mutations": 0,
                           "installer_sha256": hashlib.sha256(installer.read_bytes()).hexdigest()})
    finally:
        sentinel.unlink(missing_ok=True)  # Only this invocation's synthetic marker.
    (candidate / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(90, repeat=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    check(parser.parse_args().candidate.resolve())
