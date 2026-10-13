"""Behavioral tests for the redacted public-source audit."""

from __future__ import annotations

import json
import hashlib
import re
import sys
import subprocess
from pathlib import Path

import pytest

from scripts.audit_public_source import audit_tree


def _categories(findings: list[dict[str, object]]) -> set[str]:
    return {str(finding["category"]) for finding in findings}


def test_report_redacts_credential_value(tmp_path: Path) -> None:
    token = "ghp_" + "A" * 36
    (tmp_path / "config.txt").write_text("token=" + token, encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert "credential-format" in _categories(findings)
    assert token not in json.dumps(findings)
    assert all(set(finding) <= {"category", "path", "line", "severity"} for finding in findings)
    assert any(finding["path"] == "config.txt" and finding["line"] == 1 for finding in findings)


def test_report_redacts_credential_shaped_filename(tmp_path: Path) -> None:
    token = "ghp_" + "F" * 36
    (tmp_path / f"backup-{token}.txt").write_text(token, encoding="utf-8")

    findings = audit_tree(tmp_path)

    serialized = json.dumps(findings)
    assert token not in serialized
    assert "[REDACTED]" in serialized


def test_report_redacts_private_fragments_from_finding_paths(tmp_path: Path) -> None:
    assignment_value = "fixture-assignment-value"
    assignment = tmp_path / "exports" / f"password={assignment_value}.bin"
    assignment.parent.mkdir()
    assignment.write_bytes(b"\x00binary")
    home = tmp_path / "home" / "fixture-owner" / "sample.jpg"
    home.parent.mkdir(parents=True)
    home.write_bytes(b"\xff\xd8\xfffixture")
    key = tmp_path / "keys" / ("-----BEGIN " + "PRIVATE KEY-----.bin")
    key.parent.mkdir()
    key.write_bytes(b"\x00binary")

    findings = audit_tree(tmp_path)

    serialized = json.dumps(findings)
    assert assignment_value not in serialized
    assert "fixture-owner" not in serialized
    assert "PRIVATE KEY" not in serialized
    assert "exports/password=[REDACTED].bin" in serialized
    assert "home/[REDACTED]/sample.jpg" in serialized
    assert "keys/[REDACTED].bin" in serialized


def test_content_value_is_redacted_from_every_finding_path(tmp_path: Path) -> None:
    sensitive_value = "fixture-" + "content-value"
    private_file = (
        tmp_path
        / ".superpowers"
        / "sdd"
        / f"backup-{sensitive_value}.bin"
    )
    private_file.parent.mkdir(parents=True)
    private_file.write_bytes(
        b"\x00binary\npassword=" + sensitive_value.encode("utf-8") + b"\n"
    )

    findings = audit_tree(tmp_path)

    serialized = json.dumps(findings)
    if sensitive_value in serialized:
        raise AssertionError("serialized findings exposed a sensitive fixture value")
    assert {
        "internal-report",
        "secret-assignment",
        "unexpected-binary",
    } <= _categories(findings)
    assert {
        finding["path"]
        for finding in findings
    } == {".superpowers/sdd/backup-[REDACTED].bin"}


def test_assignment_values_before_inline_comments_redact_all_paths(tmp_path: Path) -> None:
    sensitive_values: list[str] = []
    expected_paths: set[str] = set()
    for label, quote in (("unquoted", b""), ("quoted", b'"')):
        sensitive_value = "inline-" + label + "-fixture"
        sensitive_values.append(sensitive_value)
        private_file = (
            tmp_path
            / ".superpowers"
            / "sdd"
            / f"{label}-backup-{sensitive_value}.bin"
        )
        private_file.parent.mkdir(parents=True, exist_ok=True)
        private_file.write_bytes(
            b"\x00binary\npassword="
            + quote
            + sensitive_value.encode("utf-8")
            + quote
            + b" # "
            + b"note\n"
        )
        expected_paths.add(f".superpowers/sdd/{label}-backup-[REDACTED].bin")

    findings = audit_tree(tmp_path)

    serialized = json.dumps(findings)
    if any(value in serialized for value in sensitive_values):
        raise AssertionError("serialized findings exposed an inline-comment fixture value")
    for expected_path in expected_paths:
        assert {
            finding["category"]
            for finding in findings
            if finding["path"] == expected_path
        } >= {"internal-report", "secret-assignment", "unexpected-binary"}


def test_private_key_header_and_user_path_are_located_without_values(tmp_path: Path) -> None:
    content = "-----BEGIN " + "PRIVATE KEY-----\n/Users/fixture-owner/Photos/input.jpg\n"
    (tmp_path / "notes.txt").write_text(content, encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert {"private-key", "user-path"} <= _categories(findings)
    assert "fixture-owner" not in json.dumps(findings)


@pytest.mark.parametrize("separator", ["\\", "\\\\"])
def test_windows_user_path_with_single_or_escaped_separators_is_blocking(
    tmp_path: Path, separator: str
) -> None:
    owner = "windows-fixture-owner"
    content = separator.join(("C:", "Users", owner, "Photos", "input.jpg"))
    (tmp_path / "notes.txt").write_text(content, encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert ("user-path", "blocking", 1) in {
        (finding["category"], finding["severity"], finding.get("line"))
        for finding in findings
    }
    if owner in json.dumps(findings):
        raise AssertionError("serialized findings exposed a Windows owner fixture")


def test_windows_content_owner_redacts_every_cofinding_path(tmp_path: Path) -> None:
    owner = "windows-cofinding-owner"
    private_file = tmp_path / ".superpowers" / "sdd" / f"backup-{owner}.bin"
    private_file.parent.mkdir(parents=True)
    private_file.write_bytes(
        b"\x00binary\nC:\\Users\\"
        + owner.encode("utf-8")
        + b"\\Photos\\input.jpg\n"
    )

    findings = audit_tree(tmp_path)

    if owner in json.dumps(findings):
        raise AssertionError("serialized co-findings exposed a Windows owner fixture")
    assert {
        "internal-report",
        "unexpected-binary",
        "user-path",
    } <= _categories(findings)
    assert {finding["path"] for finding in findings} == {
        ".superpowers/sdd/backup-[REDACTED].bin"
    }


def test_content_signatures_detect_mislabeled_photo_and_database(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_bytes(b"\xff\xd8\xff\xe0" + b"photo" * 4)
    (tmp_path / "cache.dat").write_bytes(b"SQLite format 3\x00" + b"data" * 4)

    findings = audit_tree(tmp_path)

    assert {("unexpected-binary", "sample.txt"), ("unexpected-binary", "cache.dat")} <= {
        (finding["category"], finding["path"]) for finding in findings
    }


def test_utf8_character_split_at_probe_boundary_is_not_binary(tmp_path: Path) -> None:
    text = "a" * 8191 + "界"
    (tmp_path / "notes.txt").write_text(text, encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert not any(finding["category"] == "unexpected-binary" for finding in findings)


def test_internal_report_path_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "calibration.csv").write_text("metric,value\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert ("internal-report", "reports/calibration.csv") in {
        (finding["category"], finding["path"]) for finding in findings
    }


def test_markdown_internal_work_products_are_flagged(tmp_path: Path) -> None:
    paths = (
        "docs/private-report.md",
        "docs/handover-2026-09-21.md",
        "docs/superpowers/specs/design.md",
        "docs/private-workflows/release.md",
    )
    for relative_path in paths:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("review notes\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    flagged = {
        finding["path"]
        for finding in findings
        if finding["category"] == "internal-report"
    }
    assert set(paths) <= flagged


def test_dotted_superpowers_area_is_flagged(tmp_path: Path) -> None:
    design = tmp_path / ".superpowers" / "sdd" / "design.md"
    design.parent.mkdir(parents=True)
    design.write_text("private design notes\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert ("internal-report", ".superpowers/sdd/design.md") in {
        (finding["category"], finding["path"]) for finding in findings
    }


def test_internal_report_exemption_is_exact_path_and_rule(tmp_path: Path) -> None:
    reviewed = tmp_path / "docs" / "public-handover-template.md"
    reviewed.parent.mkdir(parents=True)
    reviewed.write_text("public template\n", encoding="utf-8")
    copy = tmp_path / "examples" / "public-handover-template.md"
    copy.parent.mkdir(parents=True)
    copy.write_text("unreviewed copy\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    internal_paths = {
        finding["path"]
        for finding in findings
        if finding["category"] == "internal-report"
    }
    assert "docs/public-handover-template.md" not in internal_paths
    assert "examples/public-handover-template.md" in internal_paths


def test_reviewed_runtime_assignment_exemption_requires_exact_file_hash(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src/xuanpian/cache/thumbnail_cache.py"
    target = tmp_path / "src/xuanpian/cache/thumbnail_cache.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())

    assert "secret-assignment" not in _categories(audit_tree(tmp_path))

    target.write_bytes(source.read_bytes() + b"\ntoken = changed_runtime_state\n")
    assert "secret-assignment" in _categories(audit_tree(tmp_path))


def test_reviewed_assignment_exemption_does_not_cover_other_paths(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src/xuanpian/cache/thumbnail_cache.py"
    target = tmp_path / "other/thumbnail_cache.py"
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())

    assert "secret-assignment" in _categories(audit_tree(tmp_path))


def test_reviewed_exemption_does_not_hide_other_categories(tmp_path: Path, monkeypatch) -> None:
    import scripts.audit_public_source as scanner

    sample = b'token = runtime_state\n-----BEGIN PRIVATE KEY-----\n'
    target = tmp_path / "reviewed.py"
    target.write_bytes(sample)
    monkeypatch.setitem(
        scanner._REVIEWED_EXEMPTIONS,
        ("reviewed.py", "secret-assignment"),
        (hashlib.sha256(sample).hexdigest(), "synthetic test-only rationale"),
    )

    categories = _categories(audit_tree(tmp_path))
    assert "secret-assignment" not in categories
    assert "private-key" in categories


def test_reviewed_macos_bundle_fixture_owner_is_synthetic_and_hash_bound(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "tests/test_macos_bundle.py"
    data = source.read_bytes()
    owners = re.findall(rb"/(?:Users|home)/([^/\s'\"\\]+)/", data)
    assert len(owners) == 2
    assert set(owners) == {b"synthetic-test-user"}
    target = tmp_path / "tests/test_macos_bundle.py"
    target.parent.mkdir()
    target.write_bytes(data)

    assert "user-path" not in _categories(audit_tree(tmp_path))
    target.write_bytes(data + b'\n# /Users/synthetic-test-user/extra\n')
    assert "user-path" in _categories(audit_tree(tmp_path))


def test_excluded_candidate_directories_are_blocking_not_silent(tmp_path: Path) -> None:
    photo = tmp_path / "build" / "photos" / "sample.jpg"
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b"\xff\xd8\xff\xe0fixture")
    database = tmp_path / "dist" / "cache.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"SQLite format 3\x00fixture")
    report = tmp_path / "build" / "reports" / "private-report.md"
    report.parent.mkdir(parents=True)
    report.write_text("internal\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert {("excluded-location", "build"), ("excluded-location", "dist")} <= {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert all(finding["severity"] == "blocking" for finding in findings)


def test_symlink_outside_root_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("token=" + "ghp_" + "B" * 36, encoding="utf-8")
    (tmp_path / "outside.txt").symlink_to(outside)

    findings = audit_tree(tmp_path)

    assert ("symlink-outside-root", "outside.txt") in {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert "credential-format" not in _categories(findings)


def test_excluded_name_symlink_outside_root_is_not_silently_pruned(tmp_path: Path) -> None:
    outside = tmp_path.parent / (tmp_path.name + "-excluded-outside")
    outside.mkdir()
    (outside / "private-report.md").write_text("internal\n", encoding="utf-8")
    (tmp_path / "build").symlink_to(outside, target_is_directory=True)

    findings = audit_tree(tmp_path)

    assert ("symlink-outside-root", "build") in {
        (finding["category"], finding["path"]) for finding in findings
    }


def test_reviewed_model_binary_is_review_not_blocking(tmp_path: Path) -> None:
    model = tmp_path / "assets" / "models" / "detector.tflite"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"\x00\x01\x02\x03model")

    findings = audit_tree(tmp_path)

    assert {("provenance-binary-review", "review")} <= {
        (finding["category"], finding["severity"]) for finding in findings
    }
    assert not any(finding["severity"] == "blocking" for finding in findings)


def test_reviewed_binary_is_still_scanned_for_private_content(tmp_path: Path) -> None:
    token = "ghp_" + "M" * 36
    model = tmp_path / "assets" / "models" / "detector.tflite"
    model.parent.mkdir(parents=True)
    model.write_bytes(
        b"\x00\x01model\n"
        + b"token="
        + token.encode()
        + b"\n-----BEGIN PRIVATE KEY-----\n"
        + b"/Users/binary-fixture/Photos/input.jpg\n"
    )

    findings = audit_tree(tmp_path)

    assert {
        "credential-format",
        "private-key",
        "provenance-binary-review",
        "secret-assignment",
        "user-path",
    } <= _categories(findings)
    assert any(finding["severity"] == "blocking" for finding in findings)
    serialized = json.dumps(findings)
    assert token not in serialized
    assert "binary-fixture" not in serialized


def test_reviewed_task_model_is_review_not_blocking(tmp_path: Path) -> None:
    model = tmp_path / "src" / "resources" / "models" / "landmarker.task"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"\x00\x01\x02\x03model")

    findings = audit_tree(tmp_path)

    assert [(finding["category"], finding["severity"]) for finding in findings] == [
        ("provenance-binary-review", "review")
    ]


def test_exemption_is_limited_to_exact_path_and_rule(tmp_path: Path) -> None:
    fixture = "/Users/fixture-owner/Photos/input.jpg\n"
    test_file = tmp_path / "tests" / "test_public_source_audit.py"
    test_file.parent.mkdir()
    test_file.write_text(
        fixture + "token=fixture-value\n" + "-----BEGIN " + "PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    other = tmp_path / "notes.txt"
    other.write_text(fixture + "token=fixture-value\n", encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert ("user-path", "notes.txt") in {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert ("user-path", "tests/test_public_source_audit.py") not in {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert ("private-key", "tests/test_public_source_audit.py") not in {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert ("secret-assignment", "tests/test_public_source_audit.py") not in {
        (finding["category"], finding["path"]) for finding in findings
    }
    assert ("secret-assignment", "notes.txt") in {
        (finding["category"], finding["path"]) for finding in findings
    }


def test_cli_exits_nonzero_without_echoing_secret(tmp_path: Path) -> None:
    token = "ghp_" + "D" * 36
    (tmp_path / "settings.txt").write_text("token=" + token, encoding="utf-8")
    script = Path(__file__).parents[1] / "scripts" / "audit_public_source.py"

    result = subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert token not in result.stdout + result.stderr
