from __future__ import annotations

import pytest

from src.xuanpian.ui.icons import application_icon, ui_icon


def test_application_icon_loads_from_package(qapp) -> None:
    assert not application_icon().isNull()


def test_all_named_icons_are_available(qapp) -> None:
    names = {
        "panel",
        "folder",
        "analyze",
        "export",
        "back",
        "pick",
        "pending",
        "reject",
        "rotate_left",
        "rotate_right",
        "zoom_out",
        "fit",
        "zoom_in",
    }

    assert all(not ui_icon(name).isNull() for name in names)


def test_unknown_icon_is_rejected(qapp) -> None:
    with pytest.raises(KeyError, match="unknown UI icon"):
        ui_icon("camera")
