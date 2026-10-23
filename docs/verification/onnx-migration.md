# ONNX migration verification — 2026-10-23

## Implemented scope

The default technical analysis service now uses CPU ONNX Runtime with two
packaged, SHA-256-verified ONNX models. YuNet supplies faces and five landmarks;
YOLOX-Nano supplies person/animal/object candidates. MediaPipe, OpenCV and the
three old model blobs are absent from the runtime dependency set/current tree.

Pillow and NumPy replace decoding, resize, grayscale and derivative operations.
High-bit-depth grayscale PNG/TIFF values are reduced across their full range
before analysis. Reliable AF metadata retains priority. A face and its matched
body share an envelope for AF validation; unrelated people cannot override a
face/AF conflict. Without AF, scoring uses inner subject regions and a
conservative multi-region scene fallback.

The algorithm version is technical-v8. Existing v3–v7 results remain readable
and are treated as historical; stored marks remain intact. No UI or database
schema migration was introduced.

## Model checks

- Both actual packaged models execute through CPUExecutionProvider offline.
- The official YOLOX 0.1.1rc0 asset requires raw BGR, not the old tag's normalized
  RGB demo preprocessing. The release notes document this breaking change.
- ONNX Runtime 1.19.2 CPU sigmoid fusion can emit -2**-24 near zero. Probability
  parsing tolerates rounding within 1e-6 and clamps to [0,1].
- Nonfinite output or probabilities beyond that tolerance are detector errors,
  not successful zero-detection results. An integration regression verifies that
  corrupt sessions yield failed sources and REVIEW even on a textured image.
- Model restoration verifies the pre-recorded digest and size instead of
  accepting a fresh download by changing the manifest.

## Real-photo comparison

A deterministic sample of 24 JPEGs was selected across the existing sample
folder, using the same photos for the dedaec6 baseline and new engine. Reports
containing source names/hashes remain in the ignored local reports directory.

| Observation | Baseline | ONNX |
| --- | ---: | ---: |
| Clear | 15 | 17 |
| Review | 9 | 7 |
| Out of focus | 0 | 0 |
| Faces detected | 4 | 13 |

Two photos changed from REVIEW to CLEAR; no baseline CLEAR photo was downgraded
in the final comparison. The new run took approximately 12 seconds on the local
Apple Silicon machine, including decoding, metadata and both detectors. Detector
source failures: 0. Source mutations: 0, checked by SHA-256 before/after and
against the earlier sample report.

These samples have no independent human truth labels. Counts measure behavior,
not accuracy, and do not establish recall for truly blurred, low-light or
unrepresented scenes. The two changed photos were also visually inspected at
reduced resolution; that does not replace a full-resolution human benchmark.

## Automated verification and review

The full test suite passed: 1428 tests (three pre-existing Pillow deprecation
warnings). The clean indexed wheel, packaged-model checks, icon/style checks
and offscreen main-window boot also passed locally. A read-only independent
review identified the uint16 decode and invalid-output fallback issues; both
were reproduced in failing tests, corrected, and re-reviewed without remaining
actionable findings.

## Remaining release work

This verifies the source migration on the current Apple Silicon host. It does
not certify macOS 12 Intel/arm64 or Windows 10/11 installers. The existing
metadata-runtime Perl binary still requires macOS 26 and needs replacement for
the older macOS target. HEIC decoder packaging, final Qt/native notices/source
obligations, and installer release verification remain separate work.

The official YOLOX release asset is paired with the repository Apache-2.0
license; the absence of a separate weight-specific model card remains explicit
in the component inventory. Historical r5 binaries/notices are retained as
historical evidence, not relabeled as the new native closure.
