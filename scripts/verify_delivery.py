"""Delivery verification: clean wheel, models, assets, and app boot smoke.

Usage:
    QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/verify_delivery.py

Exit code 0 when every gate passes; prints one JSON summary line.
"""

from __future__ import annotations

import json
import sys
import time
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.xuanpian.engine.model_assets import MODEL_KEYS, verified_model_path
from PyQt6.QtWidgets import QApplication

from scripts.build_wheel import build_clean_wheel
from src.xuanpian.ui.icons import application_icon, ui_icon


_APP_INSTANCE: QApplication | None = None


def _app() -> QApplication:
    global _APP_INSTANCE
    if _APP_INSTANCE is None:
        _APP_INSTANCE = QApplication.instance() or QApplication([])
    return _APP_INSTANCE


def _check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        raise SystemExit(f"delivery gate failed: {name}")


def verify_models() -> None:
    for name in sorted(MODEL_KEYS):
        path = verified_model_path(name)
        _check(f"model:{name}", True, path.name)


def verify_clean_wheel() -> None:
    with TemporaryDirectory(prefix="xuanpian-delivery-wheel-") as temporary:
        wheel = build_clean_wheel(Path(temporary))
        _check("wheel:clean-build", wheel.is_file(), wheel.name)


def verify_assets() -> None:
    icon = application_icon()
    _check("app-icon-loads", not icon.isNull())
    _check("ui-icon-render", not ui_icon("tag").isNull())

    resources = Path(str(files("src.xuanpian.resources")))
    icons = resources / "icons"
    for expected in ("app-icon.png",):
        _check(f"asset:{expected}", (icons / expected).is_file())

    repo_root = Path(__file__).resolve().parent.parent
    for expected in ("app-icon.icns", "app-icon.ico", "app-icon-1024.png"):
        _check(
            f"asset:{expected}",
            (repo_root / "assets" / "icons" / expected).is_file(),
        )

    qss = resources.parent / "ui" / "styles.qss"
    _check("asset:styles.qss", qss.is_file() and qss.stat().st_size > 0)


def verify_boot() -> None:
    from src.xuanpian.ui.main_window import MainWindow

    app = _app()
    window = MainWindow()
    window.resize(1100, 720)
    window.show()
    app.processEvents()
    _check("boot:main-window", window.isVisible())
    _check("boot:toolbar", window._toolbar.objectName() == "mainToolBar")
    window.close()
    app.processEvents()


def main() -> int:
    started = time.perf_counter()
    _app()
    verify_clean_wheel()
    verify_models()
    verify_assets()
    verify_boot()
    elapsed = round(time.perf_counter() - started, 3)
    print(json.dumps({"status": "ok", "elapsed_seconds": elapsed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
