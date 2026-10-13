import sys
from importlib.resources import files
from pathlib import Path

from PyQt6.QtCore import QStandardPaths
from PyQt6.QtWidgets import QApplication

from src.xuanpian.cache.thumbnail_cache import ThumbnailCache
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.ui.main_window import (
    MainWindow,
    default_project_database_path,
)
from src.xuanpian.ui.icons import application_icon


def _apply_macos_dark_appearance() -> None:
    """Force the macOS dark appearance for source runs (cosmetic only).

    The packaged app forces dark chrome through Info.plist
    (NSUserInterfaceStyle=Dark) so the native title bar blends with the dark
    UI instead of rendering as a light strip above the toolbar.  Source runs
    need the same nudge; the call is fully guarded and never raises.
    """
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return
    try:
        import ctypes

        c_void_p = ctypes.c_void_p
        libobjc = "/usr/lib/libobjc.A.dylib"
        # Keep the 2-arg and 3-arg msgSend call patterns on separate handles
        # so their argtypes never collide on the shared objc_msgSend symbol.
        msg2 = ctypes.cdll.LoadLibrary(libobjc).objc_msgSend
        msg2.restype = c_void_p
        msg2.argtypes = [c_void_p, c_void_p]
        msg3 = ctypes.cdll.LoadLibrary(libobjc).objc_msgSend
        msg3.restype = c_void_p
        msg3.argtypes = [c_void_p, c_void_p, c_void_p]

        ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AppKit.framework/AppKit"
        )
        meta = ctypes.cdll.LoadLibrary(libobjc)
        get_class = meta.objc_getClass
        get_class.restype = c_void_p
        get_class.argtypes = [ctypes.c_char_p]
        selname = meta.sel_registerName
        selname.restype = c_void_p
        selname.argtypes = [ctypes.c_char_p]

        ns_app = msg2(
            get_class(b"NSApplication"), selname(b"sharedApplication")
        )
        if not ns_app:
            return
        dark_name = msg3(
            get_class(b"NSString"),
            selname(b"stringWithUTF8String:"),
            ctypes.c_char_p(b"NSAppearanceNameDarkAqua"),
        )
        dark = msg3(
            get_class(b"NSAppearance"),
            selname(b"appearanceNamed:"),
            dark_name,
        )
        if dark:
            msg3(ns_app, selname(b"setAppearance:"), dark)
    except Exception:
        # Appearance is cosmetic; never block startup on platform quirks.
        pass


def _load_stylesheet(app: QApplication) -> None:
    qss = files("src.xuanpian.ui").joinpath("styles.qss")
    if qss.is_file():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
        return
    print(
        "WARNING: styles.qss not found; UI falls back to default theme",
        file=sys.stderr,
    )


def main() -> None:
    from multiprocessing import freeze_support

    freeze_support()
    app = QApplication(sys.argv)
    _apply_macos_dark_appearance()
    app.setOrganizationName("Xuanpian")
    app.setApplicationName("Xuanpian")
    app.setWindowIcon(application_icon())
    _load_stylesheet(app)
    cache_root = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
    ) / "thumbnails"
    window = MainWindow(
        thumbnail_cache=ThumbnailCache(
            cache_root,
            memory_limit_bytes=128 * 1024 * 1024,
        ),
        project_path_resolver=default_project_database_path,
        project_store_factory=lambda path, root: ProjectStore.open(
            path,
            root_path=root,
        ),
    )

    from src.xuanpian.ui.keyboard_manager import KeyboardManager
    km = KeyboardManager()
    window.install_keyboard_manager(km)

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
