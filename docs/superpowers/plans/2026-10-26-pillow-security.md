# Pillow security update implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline execution with verification checkpoints.

**Goal:** Replace vulnerable Pillow 11.3.0 with the upstream security release 13.0.0 without changing photo selection behavior.

**Architecture:** Keep the existing image/preview interfaces, offline model pipeline and UI. Update the direct dependency and hash lock, fix only reproduced API incompatibilities, preserve the new wheel's notice evidence, and rebuild a fresh arm64 candidate.

**Tech Stack:** Python 3.11, Pillow 13.0.0, NumPy 2.2.6, PyQt6 6.7.1, PyInstaller.

**Spec:** Existing platform and behavior requirements in `docs/superpowers/specs/2026-10-23-onnx-subject-focus-design.md`; security release evidence at https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html.

## Global constraints

- macOS 12+ arm64/Intel and Windows 10 22H2/11 x64 remain targets; Python >=3.11 remains unchanged.
- Preserve original files, technical-v8 decisions, RAW, previews, image orientation and bit-depth handling.
- No public binary release or overwrite of existing artifacts; native floors and host tests do not replace old-OS acceptance.
- Resolve the existing CI fixture failure before asserting the branch passes CI.

## Task 1: Restore trustworthy baseline and evaluate upgrade

Files: `tests/test_metadata_runtime_build.py`, `pyproject.toml`, `packaging/requirements-macos-arm64.lock`.

- [x] Read the failing CI output and resolve the toolchain/fixture mismatch; remove the fabricated SDK override only from the real-make test, preserving production SDK validation and target assertions. The corrected test passed on the full-Xcode CI runner.
- [x] Create a separate Python 3.11 environment and run the existing suite with Pillow 13.0.0 before modifying application code.
- [x] Check reproduced API breaks against behavioral regressions. None were found: all 1436 original tests passed without application changes.
- [x] Pin `Pillow==13.0.0`, regenerate the existing hash lock without upgrading unrelated packages, and install only target-compatible binary wheels.

## Task 2: Verify deliverable and security scope

Files: existing `packaging/licenses/wheels/` snapshot, `packaging/notice-manifest.json`, `packaging/third-party-components.json`, `THIRD_PARTY_NOTICES.md`, `scripts/verify_macos_bundle.py`, `scripts/audit_public_source.py`, `tests/test_macos_bundle.py`, `docs/verification/pillow-security.md`.

- [x] Preserve exact Pillow 13 arm64 wheel license/bundled-library texts with wheel/member hashes; reuse the byte-identical existing snapshot and update current evidence without rewriting historical records.
- [x] Reproduce the real Pillow 13 FreeType/HarfBuzz cycle as failing native-gate tests; stop only current-load-chain cycles while retaining external dependency checks. Review and refresh the hash-bound synthetic fixture exemption because its file gained these tests; keep unknown/changed owner rejection.
- [x] Run full pytest, pip consistency checks, real-photo before/after comparisons, and a fresh bundle build; verify native floors, notices, model loading, startup, frozen analysis and DMG copy integrity.
- [x] Obtain independent read-only review; record the exact scope and remaining dependency/OS acceptance gates. Do not call the entire dependency graph secure solely because Pillow is updated.
**Final PR gate:** Commit and update PR #2; require macOS arm64, Intel Mac and Windows CI to pass on the latest head without bypassing failures or merging automatically. Live results are recorded on the PR rather than copied into a checkbox that becomes stale after documentation commits.
