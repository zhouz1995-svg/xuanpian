import pytest
from PyQt6.QtWidgets import QApplication

from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser


class TestThumbnailBrowser:
    def test_import(self, qapp: QApplication) -> None:
        widget = ThumbnailBrowser()
        assert widget is not None


class TestMainWindow:
    def test_import(self, qapp: QApplication) -> None:
        window = MainWindow()
        assert window is not None
        assert window.windowTitle() == "选片工作台"
        assert window.width() == 1200
        assert window.height() == 800
