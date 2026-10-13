"""Sidebar group navigation polish: icons, counts, and alignment props."""

from src.xuanpian.ui.thumbnail_browser import GROUP_ICONS, GROUPS, GroupBar, LeftSidebar


def test_group_icons_cover_all_groups() -> None:
    assert set(GROUP_ICONS) == set(GROUPS)
    for key, icon in GROUP_ICONS.items():
        assert isinstance(icon, str) and icon.strip()


def test_group_bar_buttons_carry_nav_props(qapp) -> None:
    bar = GroupBar(vertical=True)
    for key in GROUPS:
        button = bar._buttons[key]
        assert button.property("groupNavBtn") is True
        assert button.property("filterBtn") is True
        assert button.text().startswith(GROUP_ICONS[key])


def test_group_bar_update_counts_formats_text(qapp) -> None:
    bar = GroupBar(vertical=True)
    bar.update_counts({"all": 12, "clear": 3, "portrait": 5})
    assert "(12)" in bar._buttons["all"].text()
    assert "(3)" in bar._buttons["clear"].text()
    assert "(5)" in bar._buttons["portrait"].text()


def test_sidebar_header_is_library(qapp) -> None:
    sidebar = LeftSidebar()
    assert sidebar._header_label.text() == "图库"
