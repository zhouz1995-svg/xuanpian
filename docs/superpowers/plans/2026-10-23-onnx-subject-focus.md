# ONNX Subject Focus Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MediaPipe/OpenCV analysis with offline ONNX subject detection and subject-focused NumPy metrics.

**Architecture:** Keep AnalysisContext, SubjectCandidate and TechnicalAnalysisResult as the boundaries. Add separate preprocessing/postprocessing helpers and a lazy ONNX backend. Preserve autofocus priority and old stored results.

**Tech Stack:** Python 3.11, ONNX Runtime CPU, YuNet, YOLOX-Nano, Pillow, NumPy, rawpy, PyQt6.

**Spec:** docs/superpowers/specs/2026-10-23-onnx-subject-focus-design.md

## Global Constraints

- Target macOS 12+ on x86_64/arm64 and Windows 10 22H2 / Windows 11 x64. Native installers require separate verification.
- Models ship offline and are checked against fixed SHA-256 values.
- No interface or database schema change; retain AF priority.
- No HEIC codec rebuild or Windows installer in this plan.
- No merging, publishing, or replacing the existing release during implementation.

### Task 1: Offline models and ONNX detector

**Files:** engine/onnx_detection.py, engine/vision_backend.py, engine/model_assets.py; resources/models/manifest.json and two ONNX files; scripts/vendor_analysis_models.py; tests/test_onnx_detection.py and tests/test_vision_backend.py.

**Interfaces:** `prepare_yolox(bgr, size=416) -> (tensor, scale)`; `decode_yolox(outputs, shape, scale) -> tuple[SubjectCandidate, ...]`; corresponding YuNet helpers; `OnnxSubjectBackend.detect_faces/detect_objects(bgr)` and `close()`.

- [x] Add failing tests for letterbox channel order, normalized boxes/landmarks, NMS, malformed output and offline lazy session creation. Hand-check a person row `(cx=208, cy=104, w=104, h=104)` on a 416x208 image maps to `Rect(.375,.25,.25,.5)`.
- [x] Run `.venv/bin/python -m pytest -q tests/test_onnx_detection.py tests/test_vision_backend.py` and observe missing implementation failures.
- [x] Implement fixed preprocessing and output decoding with finite checks, clipped rectangles and class-aware NMS. Verify actual upstream model tensor signatures before implementing decoding.
- [x] Vendor official release models, record source URL/version/digest/license and run real CPU inference on a generated image without network.
- [x] Rerun detector/model tests and commit.

### Task 2: Compatible service integration and subject fusion

**Files:** services/analysis_service.py, engine/subject_locator.py, domain/technical_analysis.py; tests/test_subject_locator.py, tests/test_analysis_service.py, tests/test_technical_analysis_contracts.py.

**Interfaces:** Existing `SubjectLocator.locate(context) -> SubjectLocationResult` and analysis request/result interfaces stay unchanged; source names are `face/object`; current stored version becomes technical-v8.

- [x] Add failing tests that a face contained by a person is one competing subject, separate comparable faces stay ambiguous, and face/object failures remain distinct. Add a v7-read/v8-write compatibility test.
- [x] Run the focused tests and confirm failures are caused by the old fusion/version/backend behavior.
- [x] Wire OnnxSubjectBackend into the service, remove pose normalization, fuse contained face/person candidates for ranking and retain diagnostics.
- [x] Run service, subject and serialization tests and commit.

### Task 3: NumPy metrics and Pillow decoding

**Files:** engine/image_ops.py, engine/image_reader.py, engine/analysis_context.py, engine/af_focus.py, capabilities.py; tests/test_image_ops.py, tests/test_raw_support.py, tests/test_analysis_context.py, tests/test_af_focus.py, tests/test_capabilities.py.

**Interfaces:** `bgr_to_gray`, `resize_bgr`, `laplacian`, `sobel` accept NumPy arrays; existing image reader and context interfaces stay unchanged.

- [x] Add failing tests for fixed BT.601 colors, EXIF orientation, Unicode paths, hand-derived Laplacian/Sobel kernels, constant images and reflect borders. Retain the existing AF regression tests.
- [x] Run the focused tests and confirm missing helper failures.
- [x] Implement bounded-memory signed derivative kernels, Pillow image decoding/resizing and rawpy RGB conversion. Replace AF OpenCV reductions without changing its gates.
- [x] Replace old test fixture-only OpenCV use with Pillow/NumPy and assert metric tolerances against fixed hand-derived values.
- [x] Run image, RAW, AF, exposure and context regressions and commit.

### Task 4: Subject-focused sharpness

**Files:** engine/focus_analyzer.py; tests/test_focus_analyzer.py, tests/test_subject_locator.py.

**Interfaces:** `FocusAnalyzer.analyze(context, subjects) -> FocusResult` remains stable; evidence adds chosen ROI and scene fallback diagnostics.

- [x] Add failing image fixtures with sharp eyes/soft background, soft face/sharp surroundings, padded object boxes, low texture, ambiguous subjects and detector failure.
- [x] Run focus tests to confirm old whole-box decisions fail the new fixtures.
- [x] Use face landmark/central patches, upper-body patches, 70% inner object boxes, and conservative multi-region scene aggregation. Preserve AF priority and current metric gates until sample evidence supports adjustment.
- [x] Verify synthetic regressions and compare a deterministic real-photo sample against the baseline without modifying originals. Record unresolved manual labels honestly.
- [x] Commit tests and implementation.

### Task 5: Dependency, package and documentation closure

**Files:** pyproject.toml, packaging/xuanpian.spec, packaging/requirements-macos-arm64.lock, packaging/public-files.json, packaging/notice-manifest.json, packaging/licenses, docs/model-provenance.md, README.md, THIRD_PARTY_NOTICES.md, acceptance/calibration scripts, affected package tests and CI.

- [x] Update packaging tests to validate ONNX model inclusion, loadable analysis service and no MediaPipe/OpenCV binary collection; run them to observe old packaging failures.
- [x] Replace runtime dependencies and collect ONNX Runtime libraries. Pin a tested dependency set; keep old inventory labeled historical until a new bundle is audited.
- [x] Preserve licensing evidence, add exact model/runtime license snapshots, update public export allowlist, calibration backend flags and test-only dependencies.
- [x] Run the full test suite, clean wheel build, public-source audit, real-model smoke test and `git diff --check`. Recheck any failures after correction.
- [x] Record platform installer limitations and real-sample results, then commit the reviewable branch.

## Review checklist

All five deliverables cover the approved internal migration. Old native installer floor and HEIC obligations remain visible release limitations. Model publication grants must not be inferred from runtime licenses. Real-photo conclusions require human labels before claiming accuracy improvements.
