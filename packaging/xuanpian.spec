# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Xuanpian macOS app (P3 Task 5).

Build:
    .venv/bin/pyinstaller packaging/xuanpian.spec --noconfirm

Output:
    dist/选片APP.app

Runtime assets are declared explicitly so the packaged app is self-contained:
styles.qss, the offline ONNX models (with their manifest), and the app
icon PNG. ONNX Runtime and rawpy are binary-heavy dependencies
and are collected explicitly as hidden imports.
"""

import os
import tomllib
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
sys.path.insert(0, ROOT)
from scripts.build_wheel import notice_payload
with open(os.path.join(ROOT, "pyproject.toml"), "rb") as project_file:
    VERSION = tomllib.load(project_file)["project"]["version"]

datas = [
    (os.path.join(ROOT, "src", "xuanpian", "ui", "styles.qss"),
     "src/xuanpian/ui"),
    (os.path.join(ROOT, "src", "xuanpian", "resources", "icons",
                  "app-icon.png"),
     "src/xuanpian/resources/icons"),
    (os.path.join(ROOT, "src", "xuanpian", "resources", "models"),
     "src/xuanpian/resources/models"),
]
datas += [(os.path.join(ROOT, name), "legal/" + str(Path(name).parent))
          for name in notice_payload(Path(ROOT))]

hiddenimports = (
    list(collect_submodules("onnxruntime"))
    + ["rawpy"]
)
onnx_binaries = collect_dynamic_libs("onnxruntime", search_patterns=["*.dylib", "*.dll", "*.so"])

a = Analysis(
    [os.path.join(ROOT, "src", "main.py")],
    pathex=[ROOT],
    binaries=onnx_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["cv2", "mediapipe"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="选片APP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    target_arch="arm64",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="选片APP",
)

app = BUNDLE(
    coll,
    name="选片APP.app",
    icon=os.path.join(ROOT, "assets", "icons", "app-icon.icns"),
    bundle_identifier="com.xuanpian.app",
    info_plist={
        "CFBundleName": "选片APP",
        "CFBundleDisplayName": "选片工作台",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        # Rebuilt metadata runtime and target-selected wheels must pass the
        # native minimum-OS gate before delivery; this is not OS acceptance.
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # Force dark native chrome so the macOS title bar (traffic lights
        # row) blends with the dark UI instead of a light strip.
        "NSUserInterfaceStyle": "Dark",
    },
)
