"""Key QSS selectors and widget object names required by the region UI."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
QSS = REPO_ROOT / "src" / "xuanpian" / "ui" / "styles.qss"
BROWSER = REPO_ROOT / "src" / "xuanpian" / "ui" / "thumbnail_browser.py"


def test_stylesheet_declares_region_selectors() -> None:
    text = QSS.read_text(encoding="utf-8")
    expected = (
        "#viewToggle",
        "#statusFilter",
        "#analysisProgress",
        "groupNavBtn",
        "primaryAction",
        "mainToolBar",
        "mainStatusBar",
        "#thumbnailListView",
        "#thumbnailView",
    )
    for selector in expected:
        assert selector in text, f"missing QSS selector: {selector}"


def test_sidebar_edge_tab_object_name_set_in_code() -> None:
    text = BROWSER.read_text(encoding="utf-8")
    assert 'setObjectName("sidebarEdgeTab")' in text
