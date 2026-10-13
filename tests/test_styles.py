from pathlib import Path


QSS = Path("src/xuanpian/ui/styles.qss")


def test_graphite_blue_tokens_and_component_selectors_exist() -> None:
    text = QSS.read_text(encoding="utf-8")
    upper_text = text.upper()
    for token in ("#0D0F12", "#13161A", "#181C22", "#2B3038", "#5B7FE5"):
        assert token in upper_text
    assert "#C4A35A" not in upper_text
    for selector in (
        'QPushButton[groupBarButton="true"]',
        '#leftSidebar QPushButton[filterBtn="true"]',
        "#previewPanel",
        "#previewToolOverlay",
        "#brandLabel",
        "#thumbnailView",
        "QTabBar::tab:selected",
        "QScrollBar::handle:vertical",
    ):
        assert selector in text


def test_styles_do_not_encode_analysis_result_from_numeric_properties() -> None:
    text = QSS.read_text(encoding="utf-8")
    assert "blurScore" not in text
    assert "analysisScore" not in text


def test_culling_workspace_has_neutral_canvas_and_semantic_action_styles() -> None:
    text = QSS.read_text(encoding="utf-8")

    for selector in (
        "#cullingToolBar",
        "#cullingFilmstrip",
        "#cullingCanvas",
        "#cullingInformationPanel",
        "#cullingActionBar",
        'QPushButton[cullingMark="keep"]',
        'QPushButton[cullingMark="hold"]',
        'QPushButton[cullingMark="discard"]',
        "#cullingSummary",
    ):
        assert selector in text
    assert "#202124" in text.upper()
