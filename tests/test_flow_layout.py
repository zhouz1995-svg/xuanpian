from PyQt6.QtWidgets import QLabel, QWidget

from src.xuanpian.ui.flow_layout import FlowLayout


def test_flow_layout_wraps_when_width_shrinks(qapp) -> None:
    host = QWidget()
    layout = FlowLayout(host, spacing=8)
    for _ in range(3):
        label = QLabel("card")
        label.setFixedSize(100, 60)
        layout.addWidget(label)

    assert layout.heightForWidth(340) == 60
    assert layout.heightForWidth(220) == 128


def test_empty_flow_layout_has_margin_height(qapp) -> None:
    host = QWidget()
    layout = FlowLayout(host, spacing=8)
    layout.setContentsMargins(6, 6, 6, 6)

    assert layout.heightForWidth(200) == 12
