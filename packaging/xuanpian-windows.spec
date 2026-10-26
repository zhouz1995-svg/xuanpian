"""Windows x64 onedir build; invoked by scripts/build_windows.py."""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from scripts.build_wheel import notice_payload

datas = [
    (str(ROOT / "src/xuanpian/ui/styles.qss"), "src/xuanpian/ui"),
    (str(ROOT / "src/xuanpian/resources/icons/app-icon.png"), "src/xuanpian/resources/icons"),
    (str(ROOT / "src/xuanpian/resources/models"), "src/xuanpian/resources/models"),
]
datas += [(str(ROOT / name), "legal/" + str(Path(name).parent)) for name in notice_payload(ROOT)]
a = Analysis([str(ROOT / "src/main.py")], pathex=[str(ROOT)],
    binaries=collect_dynamic_libs("onnxruntime"), datas=datas,
    hiddenimports=collect_submodules("onnxruntime") + ["rawpy"],
    hookspath=[], runtime_hooks=[], excludes=["cv2", "mediapipe"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="XuanPian", console=False,
          debug=False, strip=False, upx=False, contents_directory="_internal",
          icon=str(ROOT / "assets/icons/app-icon.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="XuanPian")
