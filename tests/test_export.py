"""Tests for ExportDialog and export logic."""

from __future__ import annotations

import pytest

from src.xuanpian.services.export_service import ConflictPolicy
from src.xuanpian.ui.export_dialog import ExportDialog


class TestExportDialog:
    def test_creation_with_groups(self, qapp):
        groups = {
            "all": ("全部", 10),
            "sharp": ("清晰", 5),
            "custom": ("自定义", 3),
        }
        dlg = ExportDialog(groups)
        assert dlg.selected_group == "all"
        assert dlg.copy_mode is True
        assert dlg.prefix == ""
        assert dlg.start_number == 1

    def test_default_group_is_all(self, qapp):
        groups = {"all": ("全部", 10)}
        dlg = ExportDialog(groups)
        assert dlg.selected_group == "all"

    def test_copy_mode_default(self, qapp):
        groups = {"all": ("全部", 10)}
        dlg = ExportDialog(groups)
        assert dlg.copy_mode is True

    def test_default_conflict_policy_is_unique(self, qapp):
        dlg = ExportDialog({"all": ("全部", 2)})
        assert dlg.conflict_policy is ConflictPolicy.UNIQUE

    def test_prefix_empty_by_default(self, qapp):
        groups = {"all": ("全部", 10)}
        dlg = ExportDialog(groups)
        assert dlg.prefix == ""

    def test_start_number_default(self, qapp):
        groups = {"all": ("全部", 10)}
        dlg = ExportDialog(groups)
        assert dlg.start_number == 1

    def test_action_buttons_use_chinese_labels(self, qapp):
        dlg = ExportDialog({"all": ("全部", 2)})

        assert dlg._accept_button.text() == "导出"
        assert dlg._cancel_button.text() == "取消"
