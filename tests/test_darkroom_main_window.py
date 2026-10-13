from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QPushButton

from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.ui import main_window
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow


def test_darkroom_toolbar_and_sidebar_contract(qapp) -> None:
    window = MainWindow()

    assert window._toolbar.objectName() == "mainToolBar"
    assert window._thumbnail_browser._sidebar.objectName() == "leftSidebar"
    assert window._brand_label.text() == "选片工作台"
    assert window._brand_icon.pixmap().isNull() is False
    assert window._analyze_btn.text() == "技术分析"
    assert window._analyze_btn.property("primaryAction") is not True
    assert window._cull_btn.text() == "开始筛选"
    assert window._stack.indexOf(window._culling_workspace) >= 0
    assert window._culling_workspace is not window._preview_placeholder
    assert window._export_btn.text() == "导出照片"
    assert window._export_btn.property("primaryAction") is True
    assert window._import_btn.text() == "导入照片"
    window.close()


def test_load_exif_for_models_is_nonblocking(qapp, monkeypatch) -> None:
    window = MainWindow()
    good = ImageFileModel("/fake/good.jpg")
    bad = ImageFileModel("/fake/bad.jpg")
    window.thumbnail_browser.set_models([good, bad])

    def fake_read(path: str) -> ExifData:
        if path.endswith("good.jpg"):
            return ExifData(path=path, camera_model="D850")
        return ExifData(path=path, error=f"{path}: decode failed")

    monkeypatch.setattr(main_window, "read_exif", fake_read)

    window._load_exif_for_models()

    assert good.camera_model == "D850"
    assert good.exif_loaded is True
    assert bad.exif_error.endswith("decode failed")
    window.close()


def test_sidebar_edge_tab_toggles_sidebar(qapp) -> None:
    window = MainWindow()
    sidebar = window._thumbnail_browser._sidebar

    # 竖排面板标签在展开/收起两种状态下都常驻，是唯一开关
    assert sidebar.is_collapsed is False
    assert sidebar.width() == 200
    assert sidebar._tab.isHidden() is False
    assert sidebar._tab.x() == 179  # 贴住面板右边缘
    assert sidebar._tab.x() + sidebar._tab.width() == sidebar.width() - 1
    # 只看入选按钮已从侧栏移除
    assert not any(
        isinstance(btn, QPushButton) and btn.text() == "只看入选"
        for btn in sidebar.findChildren(QPushButton)
    )

    sidebar._tab.click()
    QTest.qWait(260)
    assert sidebar.is_collapsed is True
    assert sidebar._tab.isHidden() is False
    assert sidebar.width() == 24
    assert sidebar._tab.x() == 3  # 收起后仍贴右边缘
    assert sidebar._tab.x() + sidebar._tab.width() == sidebar.width() - 1

    sidebar._tab.click()
    QTest.qWait(260)
    assert sidebar.is_collapsed is False
    assert sidebar._tab.isHidden() is False
    assert sidebar.width() == 200
    assert sidebar._tab.x() == 179
    assert sidebar._tab.x() + sidebar._tab.width() == sidebar.width() - 1
    window.close()
