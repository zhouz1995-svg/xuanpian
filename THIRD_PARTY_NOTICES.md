# Third-party components — release BLOCKED

This is an evidence inventory, **not** a statement that the application or DMG may be redistributed. The project source is GPL-3.0-only, but dependencies, data files, fonts, models, icons, and bundled native libraries retain their own terms. The machine-readable [component inventory](packaging/third-party-components.json) separates the 41 hashed build-environment lock pins from observed runtime artifacts. Its `unresolved` and `partial` rows require review; a `null` license or license-file field means evidence is missing, not that permission is unnecessary.

## Evidence already located

### Intel macOS candidate inputs (2026-10-27)

`packaging/macos-x86_64-wheel-notices.json` records the exact 28 selected
macOS-12-compatible Intel build/test wheel filenames and archive SHA-256 values,
with every discovered license/notice member and its raw source snapshot digest.
The stdlib-only wheel-input gate checks archive, member and snapshot bytes before
offline installation. Identical existing snapshots are reused; unmatched texts
under `packaging/licenses/macos-x86_64/` retain original bytes, including CRLF.
The receipt is a public build input, not an additional bundled legal document.
The actual text snapshots remain in the existing hashed legal payload.

This covers notice preservation, not legal clearance or proof of complete
corresponding-source/relinking materials. The selected PyQt6 and FlatBuffers
wheels have no standalone license-text member; existing evidence and unresolved
review boundaries remain applicable. Build/test-only notices do not imply those
packages ship in the frozen app. Native HEIF/Qt/runtime and final-bundle rights
remain unreviewed; release status stays BLOCKED.

### Windows candidate inputs (2026-10-26)

The Windows x64 ExifTool 13.59 input is fixed by archive SHA-256 in
`packaging/runtime-windows-sources.json`, independently matched against the
upstream distribution checksums. The original `exiftool_files` payload, including
its complete `Licenses_Strawberry_Perl.zip`, LICENSE and Windows readme, is kept
in the hashed runtime. The Windows readme is also preserved as a source notice;
it identifies the ExifTool, Strawberry Perl and Oliver Betz launcher components
and states the launcher's CC0 terms. This is not a classification or clearance
of every bundled DLL or its corresponding-source obligations.

`packaging/requirements-windows-x64.lock` separately identifies the Windows build
closure. Inno Setup is used only as a compiler; its identity is recorded in each
candidate. Its separate upstream license and commercial-support requests remain
applicable. Public CI candidates are not treated as private storage or as a
completed license/security review of a Windows Release.

### Pillow security update (2026-10-26)

The current dependency is Pillow 13.0.0, replacing 11.3.0 in new builds to
consume upstream security fixes. The macOS-11 arm64 wheel's complete LICENSE,
including bundled-library notices, is byte-identical to the preserved
`dda12a98...` text under `packaging/licenses/wheels/`; the notice manifest adds
the exact 13.0.0 wheel and member hashes without rewriting historical origins.
Upstream release notes: https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html.
This update is not a security or license clearance of other dependencies or
older released binaries.

### ONNX migration (2026-10-23)

The current source engine replaces MediaPipe and OpenCV with ONNX Runtime CPU,
YuNet and YOLOX-Nano. The three former MediaPipe model blobs are removed from
the current tree; Git history retains them. Current model identity and upstream
license evidence are recorded in [model-provenance.md](docs/model-provenance.md).
YuNet's model directory explicitly licenses all files under MIT. The official
YOLOX-Nano release asset is paired with the project's Apache-2.0 license; no
separate asset-specific model card was found, so that distinction remains
explicit in the inventory. ONNX Runtime v1.19.2 MIT and third-party notice texts
are preserved under `packaging/licenses/onnxruntime/`.

The r5 native observations and wheel notices below are historical evidence.
They do not describe the current 28-pin ONNX build environment or clear a future
installer. The existing HEIF/Qt/source obligations and metadata-runtime platform
floor still require a new bundle audit.

### Packaged notice snapshot (2026-10-11)

The macOS-12-targeted arm64 candidate uses the NumPy 2.2.6
`macosx_11_0_arm64` wheel rather than the host-selected macOS-14 wheel.
Its exact 94,029-byte `LICENSE.txt`, including bundled-library notices,
is preserved at `packaging/licenses/numpy/2.2.6-macos11-arm64-LICENSE.txt`;
the notice manifest records the wheel and member hashes. This additional
notice evidence is not clearance of the final native distribution.

`packaging/notice-manifest.json` records checksums for the project license, this
notice, the component inventory, and preserved upstream texts. Its wheel-origin
records identify the exact lock-matched wheel SHA-256 and archive member, including
build/test-only packages and vendored notices; inclusion here does not assert that
all those packages are shipped. Duplicate texts are stored once. Formatting is
normalized to LF with trailing document whitespace replaced by one final LF;
upstream wording is unchanged, and original member hashes remain recorded.

The wheel builder validates this snapshot from Git-index blobs and checks the
resulting `.dist-info/licenses/` bytes. The macOS spec includes the same snapshot
under `Contents/Resources/legal/`; the bundle inspector compares it with the
reviewed source snapshot. These are notice-integrity checks, **not legal clearance**.
Original upstream text may itself contain stale native-library versions, especially
the HEIF wheel notice. That evidence is preserved, not treated as a resolved grant.
FlatBuffers has no matching license-text member in the pinned wheel; its text
remains a follow-up. CPython's installed 3.11.15 license and the verified local
Perl/ExifTool runtime notices are also preserved; complete runtime source and
native-closure duties remain open.

- PyQt6 6.11.0: the pinned wheel's `LICENSE` is GPLv3; [Riverbank's PyQt licensing page](https://www.riverbankcomputing.com/software/pyqt/) describes GPLv3/commercial choices and says the GPL wheels contain a corresponding LGPL Qt build. The verbatim GPLv3 text also appears in this repository's [LICENSE](LICENSE). This does not complete all binary-distribution obligations.
- PyQt6_sip 13.11.1: pinned-wheel BSD-2-Clause text is preserved at [packaging/licenses/PyQt6_sip-BSD-2-Clause.txt](packaging/licenses/PyQt6_sip-BSD-2-Clause.txt).
- PyQt6-Qt6 6.11.1: pinned-wheel LGPLv3 text is preserved at [packaging/licenses/Qt-LGPL-3.0.txt](packaging/licenses/Qt-LGPL-3.0.txt). [Qt's obligations page](https://www.qt.io/development/open-source-lgpl-obligations) explains that module choice and redistribution details matter. The exact frozen Qt modules, their third-party notices, corresponding source, and user-relinking arrangements still need review.
- Perl 5.44.0 and ExifTool 13.59 have hashed upstream source references in [runtime-sources.json](packaging/runtime-sources.json). The local r5 candidate includes Perl Artistic/Copying and ExifTool README files in its metadata runtime. Those build-local files are not yet a source-repository notice set or proof of final DMG compliance.
- The [OpenCV wheel project](https://github.com/opencv/opencv-python) points to third-party notices for bundled FFmpeg and other native libraries. The actual pinned 5.0.0.93 macOS wheel/app carries a larger native set, including `libx264` and `libx265`; do not treat generic README text calling FFmpeg LGPL as a determination for this binary. The pinned `pillow_heif` 1.5.0 bundled-license file describes GPLv2 wheel conditions but names older library source versions than the current candidate's `libheif.1.23.1` and `libx265.216`. The exact grants and obligations of these binaries remain unresolved. A current [x265 source header](https://github.com/videolan/x265/blob/master/source/x265.cpp) uses GPL-2.0-or-later wording; that alone does not identify the source or terms of either bundled x265 build.

## Unresolved gates before any public source or binary publication

1. **Exact model weights.** The [model provenance record](docs/model-provenance.md) ties all three checked-in files to fixed Google Storage objects. The official task pages and linked model cards provide artifact-specific Apache-2.0 evidence for BlazeFace short-range and the Pose Landmarker Lite bundle, so those two rows are reviewed and reuse the preserved MediaPipe Apache-2.0 text. EfficientDet-Lite0 float32 remains unresolved: the official task page and object identity are verified, but the Apache-2.0 label located for a related TensorFlow/Kaggle variation does not identify the exact 13,836,895-byte MediaPipe object. Obtain an exact upstream grant or remove/replace that asset through an approved product change.
2. **Native closure.** Attribute each observed `.dylib`, `.so`, and Qt framework binary in the r5 candidate to its exact wheel/source/version and determine license compatibility, corresponding-source, dynamic-linking/relinking, patent, and notice duties. The inventory lists 277 `.dylib`/`.so` paths and 7 Qt framework executables as unresolved observations, not legal classifications. Audit any remaining embedded data, fonts, Python archive content, and native files not captured by those suffixes. In particular, resolve the OpenCV/FFmpeg and HEIF/x265 builds rather than copying stale wheel notices as if they described current bytes.
3. **Authored-code and icon follow-up.** The project owner confirmed on 2026-10-12 that the application icon was designed specifically for this project by the coding assistant and did not incorporate supplied third-party artwork or stock assets. The [icon provenance record](docs/icon-provenance.md) links the common master, derived variants, design record, and Git history. The 21 icon rows are now reviewed project assets under GPL-3.0-only to the extent copyright applies. This confirmation is not a trademark search or a guarantee of exclusive rights in common motifs. A scoped search found no third-party attribution headers in application `src/` and `scripts/`; absence of headers remains insufficient evidence for unrelated authored-code clearance.
4. **Actual distributable notices.** Verify the rebuilt app/DMG contains every required license, copyright, attribution, offer/source link, and any usable replacement/relinking materials. Earlier candidates predate the new snapshot and are not retroactively updated. The current builders include and validate the known notice snapshot; this does not prove that it is the complete required notice/source/relinking set. A public-source candidate still needs its own audited allowlist and clean build check.

The project license decision does not relicense third-party works. Until these gates are resolved and independently reviewed, do not publish a DMG or describe this inventory as legal clearance. Build-only and test-only packages in the lock are not silently described as shipped; actual frozen runtime content must be inspected separately.
