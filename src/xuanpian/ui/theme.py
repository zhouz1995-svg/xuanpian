"""Graphite-blue darkroom design tokens shared by QSS and painted widgets."""

from __future__ import annotations

from PyQt6.QtGui import QColor

# Surfaces
BACKGROUND = QColor("#0D0F12")   # window / app background
SIDEBAR = QColor("#13161A")      # sidebar / recessed areas
SURFACE = QColor("#181C22")      # cards, list items
ELEVATED = QColor("#20252D")     # hover / selected cards, overlays

# Lines
BORDER = QColor("#2B3038")       # default borders
BORDER_STRONG = QColor("#3A414C")  # hover borders, neutral badges

# Text
TEXT = QColor("#D1D6DC")
MUTED = QColor("#7F8996")

# Interaction
ACCENT = QColor("#5B7FE5")

# Semantics
SUCCESS = QColor("#79B992")
WARNING = QColor("#D7AA62")
DANGER = QColor("#D27880")
