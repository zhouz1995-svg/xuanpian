from __future__ import annotations

from copy import deepcopy
from importlib import import_module


def _valid_report() -> dict[str, object]:
    return {
        "files": {"jpeg": 1000, "raw": 400, "total": 1400},
        "updates": {
            "candidates": 1800,
            "batches": 36,
            "metadata_results": 1400,
        },
        "pairs": {"initial": 400, "reopened": 400},
        "transactions": {"count": 40, "schema_version": 3},
        "cache": {
            "entries": 999,
            "memory_bytes": 1,
            "memory_limit_bytes": 2,
            "disk_bytes": 3,
            "disk_limit_bytes": 4,
            "decoder_first": 1000,
            "decoder_reopen": 0,
            "max_workers": 4,
            "max_unfinished": 8,
        },
        "reopen": {
            "rows": 999,
            "mark": "未处理",
            "pair_ids_preserved": True,
            "tombstone_preserved": True,
        },
        "keys": {
            "grid_navigation": True,
            "preview_navigation": True,
            "preview_marks_unchanged": True,
            "grid_key_1_marks_unchanged": True,
            "grid_key_2_marks_unchanged": True,
            "grid_key_3_marks_unchanged": True,
            "delete": True,
            "escape": True,
            "ambiguous_warnings": 0,
            "widgets_bounded": True,
            "navigation_model_resets": 0,
            "zoom_changed": True,
            "rotation": 90,
            "info_filename": "photo-0010.jpg",
        },
    }


def test_e2e_acceptance_validator_requires_complete_real_chain_schema() -> None:
    acceptance = import_module("scripts.acceptance_e2e_1000")
    report = _valid_report()

    assert acceptance.validate_report(report) == []

    for section in (
        "files",
        "updates",
        "pairs",
        "transactions",
        "cache",
        "reopen",
        "keys",
    ):
        broken = deepcopy(report)
        broken.pop(section)
        assert acceptance.validate_report(broken), section


def test_e2e_acceptance_validator_rejects_incomplete_or_incorrect_chain() -> None:
    acceptance = import_module("scripts.acceptance_e2e_1000")
    report = _valid_report()
    report["cache"]["decoder_reopen"] = 1  # type: ignore[index]
    report["reopen"]["rows"] = 1000  # type: ignore[index]
    report["keys"]["ambiguous_warnings"] = 1  # type: ignore[index]

    errors = acceptance.validate_report(report)

    assert any("decoder_reopen" in error for error in errors)
    assert any("rows" in error for error in errors)
    assert any("ambiguous" in error for error in errors)


def test_e2e_acceptance_validator_requires_exact_real_batch_count() -> None:
    acceptance = import_module("scripts.acceptance_e2e_1000")

    missing = _valid_report()
    missing["updates"].pop("batches")  # type: ignore[union-attr]
    assert acceptance.validate_report(missing)

    for batch_count in (1, 35, 37):
        report = _valid_report()
        report["updates"]["batches"] = batch_count  # type: ignore[index]
        assert acceptance.validate_report(report), batch_count


def test_e2e_acceptance_validator_rejects_non_culling_shortcut_mutation() -> None:
    acceptance = import_module("scripts.acceptance_e2e_1000")

    for key in (
        "preview_marks_unchanged",
        "grid_key_1_marks_unchanged",
        "grid_key_2_marks_unchanged",
        "grid_key_3_marks_unchanged",
    ):
        report = _valid_report()
        report["keys"][key] = False  # type: ignore[index]

        errors = acceptance.validate_report(report)

        assert any(key in error for error in errors)


def test_e2e_acceptance_validator_requires_each_grid_shortcut_result() -> None:
    acceptance = import_module("scripts.acceptance_e2e_1000")

    for key in (
        "grid_key_1_marks_unchanged",
        "grid_key_2_marks_unchanged",
        "grid_key_3_marks_unchanged",
    ):
        report = _valid_report()
        report["keys"].pop(key)  # type: ignore[union-attr]

        errors = acceptance.validate_report(report)

        assert any(key in error for error in errors)
