from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from src.xuanpian.engine.metadata_runtime import resolve_metadata_runtime


def inputs(tmp_path, extra=None):
    archive = tmp_path / "vendor.zip"
    entries = {"exiftool(-k).exe": b"MZ launcher", "exiftool_files/perl.exe": b"MZ perl",
               "exiftool_files/perl532.dll": b"MZ dll", "exiftool_files/exiftool.pl": b"script"}
    with ZipFile(archive, "w") as z:
        for name, data in entries.items():
            z.writestr("exiftool-13.59_64/" + name, data)
        if extra:
            z.writestr(*extra)
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps({"schema_version": 1,
        "target": {"platform": "windows", "architecture": "x86_64"},
        "exiftool": {"version": "13.59", "url": "https://example.invalid/vendor.zip",
                     "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}))
    return archive, manifest


def test_windows_staging_preserves_payload_and_binds_manifest(tmp_path):
    from scripts.prepare_windows_runtime import prepare_runtime
    archive, source = inputs(tmp_path)
    output = tmp_path / "资源 空格" / "metadata-runtime"
    prepare_runtime(source, archive, output)
    result = resolve_metadata_runtime(bundle_root=output.parent, development_executable=None)
    assert result.command_prefix[0] == str(output / "exiftool/exiftool.exe")
    assert not (output / "exiftool/exiftool(-k).exe").exists()
    data = json.loads((output / "manifest.json").read_text())
    assert data["source_manifest_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (output / "exiftool/exiftool_files/perl.exe").read_bytes() == b"MZ perl"


def test_windows_staging_refuses_bad_digest_and_existing_output(tmp_path):
    from scripts.prepare_windows_runtime import prepare_runtime
    archive, source = inputs(tmp_path)
    archive.write_bytes(b"wrong")
    output = tmp_path / "metadata-runtime"
    with pytest.raises(ValueError, match="digest"):
        prepare_runtime(source, archive, output)
    assert not output.exists()
    output.mkdir()
    marker = output / "preserve.txt"
    marker.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        prepare_runtime(source, archive, output)
    assert marker.read_bytes() == b"keep"


@pytest.mark.parametrize("name", ["../outside", "/absolute", "exiftool-13.59_64/../outside",
    "exiftool-13.59_64/C:drive", "exiftool-13.59_64/a:stream", "exiftool-13.59_64/NUL.txt",
    "exiftool-13.59_64/exiftool_files/PERL.exe"])
def test_windows_staging_rejects_unsafe_zip_names(tmp_path, name):
    from scripts.prepare_windows_runtime import prepare_runtime
    archive, source = inputs(tmp_path, (name, b"bad"))
    with pytest.raises(ValueError):
        prepare_runtime(source, archive, tmp_path / "metadata-runtime")
    assert not (tmp_path / "outside").exists()


def test_windows_staging_rejects_zip_symlink(tmp_path):
    from scripts.prepare_windows_runtime import prepare_runtime
    member = ZipInfo("exiftool-13.59_64/escape")
    member.create_system = 3
    member.external_attr = 0o120777 << 16
    archive, source = inputs(tmp_path, (member, b"../outside"))
    with pytest.raises(ValueError):
        prepare_runtime(source, archive, tmp_path / "metadata-runtime")


@pytest.mark.parametrize("machine", [0x8664, 0x14c])
def test_pe_machine_reads_actual_header(tmp_path, machine):
    from scripts.verify_windows_bundle import pe_machine
    path = tmp_path / "native.dll"
    header = bytearray(128)
    header[:2] = b"MZ"
    header[60:64] = (64).to_bytes(4, "little")
    header[64:68] = b"PE\0\0"
    header[68:70] = machine.to_bytes(2, "little")
    path.write_bytes(header)
    assert pe_machine(path) == machine


def test_pe_parser_rejects_non_pe_file(tmp_path):
    from scripts.verify_windows_bundle import pe_machine
    path = tmp_path / "bad.dll"
    path.write_bytes(b"MZ not a PE header")
    with pytest.raises(ValueError):
        pe_machine(path)


def test_windows_builder_never_overwrites_output(tmp_path):
    from scripts.build_windows import build_app
    output = tmp_path / "candidate"
    output.mkdir()
    (output / "keep").write_bytes(b"data")
    with pytest.raises(FileExistsError):
        build_app(runtime_root=tmp_path / "missing", output=output, iscc=tmp_path / "missing.exe")
    assert (output / "keep").read_bytes() == b"data"


def test_vendor_dll_cannot_satisfy_application_import(tmp_path):
    from scripts.verify_windows_bundle import dll_visible
    app = tmp_path / "app"
    vendor = app / "_internal/metadata-runtime/exiftool/exiftool_files"
    vendor.mkdir(parents=True)
    (vendor / "example.dll").write_bytes(b"dll")
    assert not dll_visible("example.dll", app / "XuanPian.exe", app)
    assert dll_visible("example.dll", vendor / "perl.exe", app)
    assert dll_visible("example.dll", vendor / "lib/auto/POSIX/POSIX.xs.dll", app)


def test_package_dll_directory_is_not_globally_visible(tmp_path):
    from scripts.verify_windows_bundle import dll_visible
    app = tmp_path / "app"
    libs = app / "_internal/pillow.libs"
    libs.mkdir(parents=True)
    (libs / "example.dll").write_bytes(b"dll")
    assert dll_visible("example.dll", app / "_internal/PIL/_imaging.pyd", app)
    assert not dll_visible("example.dll", app / "_internal/PyQt6/QtCore.pyd", app)


def test_msvc_runtime_is_collected_from_approved_qt_wheel_not_system(tmp_path):
    from scripts.build_windows import stage_msvc_runtime
    internal, qt = tmp_path / "_internal", tmp_path / "wheel/Qt6/bin"
    internal.mkdir()
    qt.mkdir(parents=True)
    for name in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll"):
        (qt / name).write_bytes(name.encode())
    (internal / "vcruntime140.dll").write_bytes(b"already collected")
    stage_msvc_runtime(internal, qt)
    assert (internal / "msvcp140_1.dll").read_bytes() == b"msvcp140_1.dll"
    assert (internal / "vcruntime140.dll").read_bytes() == b"already collected"


def test_installer_language_rejects_unapproved_download_without_writing(tmp_path):
    from scripts.build_windows import stage_installer_language
    target = tmp_path / "ChineseSimplified.isl"
    with pytest.raises(ValueError, match="language digest"):
        stage_installer_language(target, b"unapproved translation")
    assert not target.exists()


def test_failed_frozen_probe_terminates_its_workers_before_pool_shutdown():
    from concurrent.futures import ProcessPoolExecutor, TimeoutError
    import multiprocessing as mp
    import time
    from scripts.check_windows_candidate import abort_frozen_workers
    pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    try:
        future = pool.submit(time.sleep, 60)
        with pytest.raises(TimeoutError):
            future.result(timeout=0.1)
        workers = list(pool._processes.values())
        abort_frozen_workers(pool)
        assert all(not worker.is_alive() for worker in workers)
    finally:
        for worker in (pool._processes or {}).values():
            if worker.is_alive():
                worker.terminate()
        pool.shutdown(wait=True, cancel_futures=True)


def test_database_lock_backend_excludes_another_open_handle(tmp_path):
    from src.xuanpian.store.database_lock import _lock_file
    path = tmp_path / "项目.lock"
    with path.open("a+b") as first:
        _lock_file(first)
        with path.open("a+b") as second:
            with pytest.raises(OSError):
                _lock_file(second)
    with path.open("a+b") as reopened:
        _lock_file(reopened)


def test_desktop_entrypoint_imports_on_the_native_platform():
    from src.main import main
    assert callable(main)


def test_database_lock_excludes_other_process_and_releases_on_close(tmp_path):
    import subprocess
    import sys
    from src.xuanpian.store.database_lock import ProjectDatabaseLock
    path = tmp_path / "中文项目.sqlite3"
    probe = """
import sys
from pathlib import Path
from src.xuanpian.store.database_lock import ProjectDatabaseLock, ProjectDatabaseLockedError
try:
    with ProjectDatabaseLock.acquire(Path(sys.argv[1])):
        pass
except ProjectDatabaseLockedError:
    sys.exit(23)
"""
    command = [sys.executable, "-c", probe, str(path)]
    with ProjectDatabaseLock.acquire(path):
        busy = subprocess.run(command, capture_output=True)
        assert busy.returncode == 23, busy.stderr
    reopened = subprocess.run(command, capture_output=True)
    assert reopened.returncode == 0, reopened.stderr


def test_launcher_utf8_manifest_preserves_long_path_setting():
    from xml.etree import ElementTree as ET
    from scripts.prepare_windows_runtime import utf8_process_manifest
    original = b'''<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
<longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
</windowsSettings></application></assembly>'''
    root = ET.fromstring(utf8_process_manifest(original))
    assert root.find(".//{http://schemas.microsoft.com/SMI/2019/WindowsSettings}activeCodePage").text == "UTF-8"
    assert root.find(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}longPathAware").text == "true"
    assert root.tag == "{urn:schemas-microsoft-com:asm.v1}assembly"
