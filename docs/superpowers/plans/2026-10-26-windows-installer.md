# Windows installer implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation and review checkpoints.

**Goal:** Produce and exercise a self-contained Windows x64 candidate installer without changing photo workflows.

**Architecture:** Add a strict Windows metadata-runtime variant alongside the unchanged Mac contract, stage hash-verified upstream inputs, package using PyInstaller onedir and Inno Setup, and verify on a native Windows CI runner.

**Tech Stack:** Python 3.11, PyInstaller 6.21.0, ExifTool 13.59 x64, Inno Setup 6, existing ONNX/PyQt/Pillow pins.

**Spec:** `docs/superpowers/specs/2026-10-26-windows-installer-design.md`.

## Global constraints

- Windows 10 22H2/11 x64 target; offline/self-contained; user photos never uploaded.
- Preserve Mac schema 1, UI, technical-v8, source files and user project data.
- New output paths only, strict hashes and no frozen host-runtime fallback.
- No public Release or automatic merge; native Server CI is not desktop clean-machine acceptance.

## Task 1: Windows metadata contract

Files: `src/xuanpian/engine/metadata_runtime.py`, `src/xuanpian/engine/af_metadata.py`, `tests/test_windows_runtime.py`, `tests/test_af_metadata.py`.

- [x] Add failing tests for schema 2 exact executable/arguments, Windows-safe manifest paths, cache corruption checks, required environment, trusted `_internal` root, UTF-8 and CREATE_NO_WINDOW.
- [x] Run focused tests and confirm the new boundary fails before implementation.
- [x] Implement the Windows variant while retaining schema 1 and all existing Mac rejection cases; run full local regression.

## Task 2: Verified input staging and native package

Files: `packaging/runtime-windows-sources.json`, `scripts/prepare_windows_runtime.py`, `tests/test_windows_packaging.py`, `packaging/xuanpian-windows.spec`, `packaging/requirements-windows-x64.lock`, `packaging/windows-installer.iss`, `scripts/build_windows.py`.

- [x] Test wrong digest, unsafe ZIP entries/case collisions, no-overwrite and exact staged manifest; implement archive staging and verify the real approved archive locally without executing Windows binaries.
- [x] Generate a Windows x64 hash lock preserving current direct dependency versions; add Windows-only onedir assets/spec and per-user Inno installer.
- [x] Test builder preconditions and resource/provenance gates. Record compiler identity; never replace existing output.

## Task 3: Native verification and handoff

Files: `scripts/verify_windows_bundle.py`, `scripts/check_windows_candidate.py`, `.github/workflows/tests.yml`, `docs/building-windows.md`, `docs/verification/windows-installer.md`.

- [x] Add resource/hash/model/PE x64 checks and a native smoke verifier that uses generated Chinese-path images, actual frozen analysis, and a fresh install/uninstall cycle with retained project sentinel.
- [x] Run local tests and independent read-only review; publish a separate PR based on the preceding safety branch.
- [ ] Run Windows build/installer verification in CI, fetch candidate artifacts and report exact source/hash/test evidence. Fix failures using root-cause tests; do not label unavailable or failing native verification complete.
