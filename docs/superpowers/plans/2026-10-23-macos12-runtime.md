# macOS 12 runtime implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the measured macOS 26 Perl and macOS 14 NumPy deployment floors without changing application behavior.

**Architecture:** Pin the metadata runtime deployment target in its source manifest and pass it through a sanitized compiler environment. Build with macOS-12-compatible Python wheels, then let the existing native bundle inspector reject components above the declared application floor.

**Tech Stack:** Perl 5.44.0, ExifTool 13.59, Python 3.11, NumPy 2.2.6, PyInstaller.

**Spec:** `docs/superpowers/specs/2026-10-23-onnx-subject-focus-design.md` (confirmed platform requirements).

## Global constraints

- macOS minimum target: 12.0; this deliverable covers arm64 only, not an Intel or universal installer.
- Preserve UI, technical-v8 analysis, file safety, offline models, and supported formats.
- Do not overwrite existing runtimes, applications, or releases.
- A native deployment-floor check does not substitute for testing on an actual macOS 12 machine.

## Task 1: Reproducible metadata deployment target

Files: `packaging/runtime-sources.json`, `scripts/build_metadata_runtime.py`, `tests/test_metadata_runtime_build.py`.

- [ ] Add tests for rejecting missing/malformed deployment targets and for overriding inherited host targets/compiler flags.
- [ ] Run `.venv/bin/python -m pytest tests/test_metadata_runtime_build.py -q` and confirm the new tests fail.
- [ ] Require a numeric `build.deployment_target`, export it as `MACOSX_DEPLOYMENT_TARGET`, and remove inherited compiler/linker overrides. Pin `12.0` in the source manifest.
- [ ] Run the tests again and build a fresh runtime with `scripts/build_metadata_runtime.py --manifest packaging/runtime-sources.json --output <fresh-runtime>`; retain upstream Perl and ExifTool test results.

## Task 2: Compatible application candidate

Files: `packaging/xuanpian.spec`, `docs/verification/macos12-runtime.md`.

- [ ] Install the existing hash-locked requirements with `uv pip install --python-platform aarch64-apple-darwin --python ...` using a macOS 12 target selection; inspect NumPy's installed Mach-O files before proceeding.
- [ ] Once all native components have floors at most 12.0, set `LSMinimumSystemVersion` to `12.0`.
- [ ] Run the complete test suite, build a fresh app with the new runtime, run `verify_macos_bundle.py` and `smoke_test_bundle.py`, and test metadata extraction on an existing read-only photo sample.
- [ ] Document actual native floors, host-only verification, artifact paths, and outstanding macOS 12/Intel/Windows acceptance. Review changes and commit on the feature branch; do not push directly to main.
