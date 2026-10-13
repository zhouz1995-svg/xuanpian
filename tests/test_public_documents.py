"""Contract checks for the source/DMG public documentation candidate."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "README.md",
    "docs/installation.md",
    "docs/building-macos.md",
    "PRIVACY.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "LICENSE",
    ".github/ISSUE_TEMPLATE/bug_report.md",
)


def test_required_documents_exist() -> None:
    assert all((ROOT / name).is_file() for name in DOCUMENTS)


def test_readme_documents_supported_target() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Apple Silicon" in text
    assert ".dmg" in text
    assert "/Users/" not in text


def test_installation_is_arm64_dmg_drag_and_drop() -> None:
    text = (ROOT / "docs/installation.md").read_text(encoding="utf-8")
    assert "arm64" in text
    assert ".dmg" in text
    assert "drag" in text.lower() and "Applications" in text
    assert "/Users/" not in text


def test_known_af_limits_are_documented() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "NIKON CORPORATION" in text
    assert "NIKON Z50_2" in text
    assert "0402" in text
    assert "fallback" in text.lower()
    assert "no universal eye-sharpness guarantee" in text.lower()


def test_notices_keep_release_gate_explicit() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "BLOCKED" in text
    assert "model" in text.lower()
    assert "native" in text.lower()


def test_manifest_covers_every_lock_pin_and_model() -> None:
    manifest = json.loads((ROOT / "packaging/third-party-components.json").read_text(encoding="utf-8"))
    components = manifest["components"]
    names = {component["name"].lower().replace("_", "-") for component in components}
    lock = (ROOT / "packaging/requirements-macos-arm64.lock").read_text(encoding="utf-8")
    pins = set(re.findall(r"^([A-Za-z0-9_.-]+)==", lock, re.MULTILINE))
    assert {pin.lower().replace("_", "-") for pin in pins} <= names
    assert {"face-detector", "pose-landmarker", "object-detector"} <= names
    for component in components:
        assert {
            "name", "version", "source_url", "license_expression",
            "license_file", "redistribution_notes", "verification_source",
        } <= component.keys()
        if component["license_file"] is not None:
            assert (ROOT / component["license_file"]).is_file()


def test_unresolved_components_cannot_be_marked_clear() -> None:
    manifest = json.loads((ROOT / "packaging/third-party-components.json").read_text(encoding="utf-8"))
    assert manifest["release_status"] == "blocked"
    assert any(item["status"] == "unresolved" for item in manifest["components"])


def test_project_icon_variants_have_reviewed_provenance() -> None:
    manifest = json.loads((ROOT / "packaging/third-party-components.json").read_text(encoding="utf-8"))
    icons = [item for item in manifest["components"] if item["name"].startswith("icon:")]

    assert len(icons) == 21
    assert {item["status"] for item in icons} == {"reviewed"}
    assert {item["license_expression"] for item in icons} == {"GPL-3.0-only"}
    assert all("2026-10-12" in item["verification_source"] for item in icons)


def test_bundled_models_only_clear_when_exact_official_evidence_is_recorded() -> None:
    manifest = json.loads((ROOT / "packaging/third-party-components.json").read_text(encoding="utf-8"))
    models = {
        item["name"]: item
        for item in manifest["components"]
        if item["distribution_scope"] == "bundled_model"
    }

    assert set(models) == {"face-detector", "pose-landmarker", "object-detector"}
    assert {models[name]["status"] for name in ("face-detector", "pose-landmarker")} == {"reviewed"}
    assert {models[name]["license_expression"] for name in ("face-detector", "pose-landmarker")} == {
        "Apache-2.0"
    }
    assert {models[name]["license_file"] for name in ("face-detector", "pose-landmarker")} == {
        "packaging/licenses/wheels/8707eef0533987efc5b155d64761eeb6e20793f50b9bd1a68dad1cf4719d0ed8.txt"
    }
    assert "1682480001338381" in models["face-detector"]["verification_source"]
    assert "1682624736756847" in models["pose-landmarker"]["verification_source"]
    assert models["object-detector"]["status"] == "unresolved"
    assert models["object-detector"]["license_expression"] is None


def test_spdx_metadata_requires_a_compatible_builder() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["project"]["license"] == "GPL-3.0-only"
    assert "setuptools>=77" in config["build-system"]["requires"]


def test_public_contribution_docs_match_the_open_repository() -> None:
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert "This is a source candidate" not in contributing
    assert "before opening a remote repository" not in contributing
    assert "not approved for public distribution" not in security
    assert "https://github.com/zhouz1995-svg/xuanpian/issues" in contributing
    assert "https://github.com/zhouz1995-svg/xuanpian/security/advisories/new" in security


def test_ci_runs_the_full_suite_on_arm64_with_minimal_permissions() -> None:
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert "pull_request_target" not in workflow
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "contents: read" in workflow
    assert "runs-on: macos-15" in workflow
    assert "3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "pip install --require-hashes -r packaging/requirements-macos-arm64.lock" in workflow
    assert "python -m pytest -q" in workflow


def test_dependabot_keeps_github_actions_current() -> None:
    config = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

    assert 'package-ecosystem: "github-actions"' in config
    assert 'directory: "/"' in config
    assert 'interval: "weekly"' in config
