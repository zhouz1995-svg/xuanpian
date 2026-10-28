"""Native macOS candidate gates; no Intel execution on an arm64 test host."""
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import hashlib
import json
import multiprocessing as mp
import multiprocessing.spawn
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


def test_candidate_rejects_non_native_host_before_output(tmp_path, monkeypatch):
    from scripts import check_macos_candidate as gate
    from scripts import build_metadata_runtime as runtime
    monkeypatch.setattr(runtime.platform, "machine", lambda: "arm64")
    with pytest.raises(RuntimeError, match="native"):
        gate.check(tmp_path, architecture="x86_64")
    assert not (tmp_path / "verification.json").exists()


def test_candidate_preserves_prior_verification(tmp_path, monkeypatch):
    from scripts import check_macos_candidate as gate
    monkeypatch.setattr(gate, "_require_native_host", lambda arch: None)
    output = tmp_path / "verification.json"
    output.write_bytes(b"prior evidence")
    with pytest.raises(FileExistsError):
        gate.check(tmp_path)
    assert output.read_bytes() == b"prior evidence"


def test_candidate_rejects_bad_dmg_checksum_without_launching(tmp_path, monkeypatch):
    from scripts import check_macos_candidate as gate
    monkeypatch.setattr(gate, "_require_native_host", lambda arch: None)
    (tmp_path / "选片APP.app").mkdir()
    dmg = tmp_path / "XuanPian-0.1.0-macos12-x86_64-local-ad-hoc.dmg"
    dmg.write_bytes(b"image")
    dmg.with_suffix(".dmg.sha256").write_text("0" * 64 + "  " + dmg.name + "\n")
    with pytest.raises(ValueError, match="checksum"):
        gate.check(tmp_path)
    assert not (tmp_path / "verification.json").exists()


def test_module_containment_rejects_source_checkout(tmp_path):
    from scripts.check_macos_candidate import validate_frozen_probe
    result = {"pillow": "13.0.0", "modules": {
        "src.xuanpian.services.analysis_service": str(tmp_path / "source/analysis_service.py")},
        "project_store": "PASS", "algorithm": "technical-v8", "portrait_assessed": True}
    with pytest.raises(ValueError, match="module"):
        validate_frozen_probe(result, tmp_path / "选片APP.app/Contents/Frameworks")


def test_failed_frozen_probe_terminates_only_owned_workers():
    from scripts.check_macos_candidate import abort_frozen_workers
    pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    unrelated = mp.get_context("spawn").Process(target=time.sleep, args=(60,))
    unrelated.start()
    try:
        future = pool.submit(time.sleep, 60)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.1)
        workers = list(pool._processes.values())
        abort_frozen_workers(pool)
        assert all(not worker.is_alive() for worker in workers)
        assert unrelated.is_alive()
    finally:
        for worker in (pool._processes or {}).values():
            if worker.is_alive():
                worker.kill()
        pool.shutdown(wait=True, cancel_futures=True)
        unrelated.terminate()
        unrelated.join(timeout=5)


def test_frozen_probe_restores_posix_executables_on_timeout(tmp_path, monkeypatch):
    from scripts import check_macos_candidate as gate
    original = sys.executable, mp.spawn.get_executable(), mp.spawn.get_preparation_data
    original_frozen = getattr(sys, "frozen", None)
    executable = tmp_path / "选片APP.app/Contents/MacOS/选片APP"
    frameworks = executable.parents[1] / "Frameworks"
    observed = []
    class FailedPool:
        _processes = {}
        def __init__(self, **kwargs):
            observed.append((sys.executable, mp.spawn.get_executable()))
        def submit(self, *args):
            return self
        def result(self, timeout):
            raise TimeoutError("owned probe timeout")
        def shutdown(self, **kwargs):
            pass
    monkeypatch.setattr(gate, "ProcessPoolExecutor", FailedPool)
    with pytest.raises(TimeoutError):
        gate.frozen_analysis(executable, frameworks, tmp_path / "synthetic.jpg")
    assert observed == [(str(executable), str(executable).encode())]
    assert (sys.executable, mp.spawn.get_executable(), mp.spawn.get_preparation_data) == original
    assert getattr(sys, "frozen", None) == original_frozen


def test_frozen_spawn_preparation_uses_canonical_framework_path_for_directory_alias(tmp_path, monkeypatch):
    from scripts import check_macos_candidate as gate
    real = tmp_path / "canonical"
    real_frameworks = real / "中文 安装位置/选片APP.app/Contents/Frameworks"
    real_frameworks.mkdir(parents=True)
    alias = tmp_path / "directory-alias"
    alias.symlink_to(real, target_is_directory=True)
    frameworks = alias / "中文 安装位置/选片APP.app/Contents/Frameworks"
    prepared_paths = []
    def stopped_before_launch(**kwargs):
        prepared_paths.extend(mp.spawn.get_preparation_data("path-alias-regression")["sys_path"])
        raise TimeoutError("stop before launching any worker")
    monkeypatch.setattr(gate, "ProcessPoolExecutor", stopped_before_launch)
    with pytest.raises(TimeoutError, match="stop before launching"):
        gate.frozen_analysis(frameworks.parent / "MacOS/选片APP",
                             frameworks, tmp_path / "synthetic.jpg")
    assert prepared_paths == [str(real_frameworks), str(real_frameworks / "base_library.zip")]


def test_owned_mount_gets_bounded_force_detach_if_normal_detach_times_out(tmp_path, monkeypatch):
    import subprocess
    from scripts import check_macos_candidate as gate
    mount = tmp_path / "owned-mount"
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert 0 < kwargs["timeout"] <= 120
        if command[:2] == ["/usr/bin/ditto", "--rsrc"]:
            raise RuntimeError("copy failed")
        if command[:2] == ["/usr/bin/hdiutil", "detach"] and "-force" not in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(gate.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="copy failed"):
        gate.relocate_dmg(tmp_path / "input.dmg", tmp_path)
    assert calls[0] == ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse",
                        "-mountpoint", str(mount), str(tmp_path / "input.dmg")]
    assert calls[-1] == ["/usr/bin/hdiutil", "detach", "-force", str(mount)]


@pytest.mark.parametrize("mutation", ["pillow", "algorithm", "portrait", "store",
                                     "source_module", "missing_module",
                                     "null_module", "relative_module"])
def test_frozen_result_rejects_incomplete_or_wrong_runtime_evidence(tmp_path, mutation):
    from scripts.check_macos_candidate import PROBE_MODULES, validate_frozen_probe
    frameworks = tmp_path / "选片APP.app/Contents/Frameworks"
    modules = {name: str(frameworks / (name.replace(".", "/") + ".py"))
               for name in PROBE_MODULES}
    result = {"pillow": "13.0.0", "modules": modules, "project_store": "PASS",
              "algorithm": "technical-v8", "portrait_assessed": True}
    validate_frozen_probe(result, frameworks)
    if mutation == "pillow":
        result["pillow"] = "11.3.0"
    elif mutation == "algorithm":
        result["algorithm"] = "technical-v7"
    elif mutation == "portrait":
        result["portrait_assessed"] = False
    elif mutation == "store":
        result["project_store"] = "FAIL"
    elif mutation == "source_module":
        modules["PIL"] = str(tmp_path / "source/PIL/__init__.py")
    elif mutation == "null_module":
        modules["onnxruntime"] = None
    elif mutation == "relative_module":
        modules["onnxruntime"] = "onnxruntime/__init__.py"
    else:
        del modules["PIL"]
    with pytest.raises(ValueError):
        validate_frozen_probe(result, frameworks)


def test_app_inspection_is_bounded_before_boot(tmp_path, monkeypatch):
    import subprocess
    from scripts import check_macos_candidate as gate
    app = tmp_path / "选片APP.app"
    app.mkdir()
    def run(command, **kwargs):
        assert kwargs.get("timeout") == 180
        raise subprocess.TimeoutExpired(command, 180)
    monkeypatch.setattr(gate, "_run_inspection", run)
    with pytest.raises(subprocess.TimeoutExpired):
        gate._exercise_app(app, tmp_path / "photo.jpg", architecture="x86_64",
                           home=tmp_path / "home")


def _process_is_running(pid):
    status = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=3)
    return status.returncode == 0 and bool(status.stdout.strip()) and not status.stdout.strip().startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="Mac inspector owns a POSIX session")
@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_inspection_failure_stops_owned_descendant_but_not_unrelated_process(tmp_path, failure):
    # Replacing group cleanup with parent-only termination must leave this child alive.
    from scripts import check_macos_candidate as gate
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"while not Path({str(child_pid_file)!r}).exists(): time.sleep(0.01)\n"
        + ("time.sleep(60)\n" if failure == "timeout" else
           "print('inspection diagnostic', file=sys.stderr); sys.exit(7)\n")
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        command = [sys.executable, "-c", parent_code]
        if failure == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                gate._run_inspection(command, timeout=5)
        else:
            result = gate._run_inspection(command, timeout=5)
            assert result.returncode == 7
            assert "inspection diagnostic" in result.stderr
        assert child_pid_file.is_file(), "descendant must actually start before failure"
        assert not _process_is_running(int(child_pid_file.read_text()))
        assert unrelated.poll() is None
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text())
            if _process_is_running(child_pid):
                os.kill(child_pid, signal.SIGKILL)
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_inspection_returns_real_successful_output():
    from scripts import check_macos_candidate as gate
    result = gate._run_inspection([sys.executable, "-c", "print('inspection passed')"], timeout=5)
    assert result.returncode == 0
    assert result.stdout == "inspection passed\n"
    assert result.stderr == ""
