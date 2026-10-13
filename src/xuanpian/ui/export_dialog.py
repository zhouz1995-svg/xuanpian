"""Export options dialog with group selection, copy/move mode, and rename template."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from src.xuanpian.services.export_service import ConflictPolicy


class ExportDialog(QDialog):
    """Dialog for configuring export options."""

    def __init__(
        self,
        groups: dict[str, tuple[str, int]],  # key -> (label, count)
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出选项")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Group selection
        group_layout = QFormLayout()
        self._group_combo = QComboBox()
        self._group_combo.addItem("全部", "all")
        for key, (label, count) in groups.items():
            if key != "all":
                self._group_combo.addItem(f"{label} ({count} 张)", key)
        group_layout.addRow("导出范围:", self._group_combo)
        layout.addLayout(group_layout)

        # Mode: copy or move
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("操作模式:"))
        self._copy_radio = QRadioButton("复制文件")
        self._copy_radio.setChecked(True)
        self._move_radio = QRadioButton("移动文件")
        mode_layout.addWidget(self._copy_radio)
        mode_layout.addWidget(self._move_radio)
        mode_layout.addStretch()
        layout.addLayout(mode_layout)

        conflict_layout = QFormLayout()
        self._conflict_combo = QComboBox()
        self._conflict_combo.addItem(
            "自动生成唯一文件名", ConflictPolicy.UNIQUE
        )
        self._conflict_combo.addItem("跳过同名文件", ConflictPolicy.SKIP)
        self._conflict_combo.addItem(
            "覆盖同名文件（谨慎）", ConflictPolicy.OVERWRITE
        )
        conflict_layout.addRow("同名文件:", self._conflict_combo)
        layout.addLayout(conflict_layout)

        # Rename template
        rename_layout = QFormLayout()
        self._prefix_edit = QLineEdit()
        self._prefix_edit.setPlaceholderText("留空保持原文件名")
        rename_layout.addRow("文件名前缀:", self._prefix_edit)

        spin_layout = QHBoxLayout()
        self._start_num = QSpinBox()
        self._start_num.setRange(1, 9999)
        self._start_num.setValue(1)
        spin_layout.addWidget(self._start_num)
        spin_layout.addStretch()
        rename_layout.addRow("起始序号:", spin_layout)
        layout.addLayout(rename_layout)

        # Hint
        hint = QLabel("重命名格式: {前缀}_{序号:03d}{原扩展名}")
        hint.setStyleSheet("color: #7F8996; font-size: 11px;")
        layout.addWidget(hint)

        # Confirm / Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_group(self) -> str:
        return self._group_combo.currentData()

    @property
    def copy_mode(self) -> bool:
        return self._copy_radio.isChecked()

    @property
    def conflict_policy(self) -> ConflictPolicy:
        return ConflictPolicy(self._conflict_combo.currentData())

    @property
    def prefix(self) -> str:
        return self._prefix_edit.text().strip()

    @property
    def start_number(self) -> int:
        return self._start_num.value()
