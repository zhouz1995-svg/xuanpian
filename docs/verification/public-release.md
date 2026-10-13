# Public release verification — local preparation only

This checklist describes a local source and binary candidate. It is not permission
to publish source, a DMG, or a release. The source uses GPL-3.0-only; third-party
rights and distributable notices remain separate gates. See the
[component inventory](../../THIRD_PARTY_NOTICES.md) and
[macOS build procedure](../building-macos.md).

## Evidence ledger

### Notice-packaging follow-up — 2026-10-11

Source `03e792d660010a294d20798e127ffc4392f9ec5f` produced a new local
wheel, app and DMG with 87 notice-snapshot files (including the manifest).
The DMG SHA-256 is
`9f8d653f877dc2fb57cb0eae227135b77fd431155baea0a4cc4c3a982086ba15`.
The development checkout passed 1,382 tests in 29.14 seconds; the fresh source
candidate `f916c73137757edee553fd21bff006ecf6631525` passed 1,382 tests in
30.57 seconds using the existing hash-locked build environment (not a new
dependency installation). Its 313 exported files matched the source commit and
manifest. The source audit found zero blocking findings and the same 24 provenance
review items. The new app and read-only mounted DMG copy passed static inspection
with zero violations; DMG checksum validation and copied-app signature validation
passed. No production GUI or private-photo test was performed. The verified
Stage A Perl/ExifTool runtime was reused, not rebuilt in this follow-up.

This checklist update is documentation-only and is not part of that binary's
source revision. Any subsequent export must retain its own identity/receipt.
Known notice inclusion is now verified; completeness of required grants, notices,
source offers and relinking materials is still blocked. The older evidence below
remains historical and must not be mistaken for the new binary.

### Icon provenance follow-up — 2026-10-12

The project owner confirmed that the application icon was created specifically
for this project by the coding assistant without supplied third-party artwork or
stock assets. The 21 checked-in variants share one project master and are now
recorded as reviewed GPL-3.0-only project assets to the extent copyright applies.
The source scanner still reports these binary files for human provenance review
by design; those review findings are no longer “source unknown.” The three model
blobs remain unresolved. This documentation and notice update was not rebuilt
into the 2026-10-11 DMG; that earlier binary remains the latest tested package.

### Model provenance follow-up — 2026-10-13

The three bundled model files were matched to their fixed version-1 Google
Storage objects using byte count and MD5 object metadata, with local SHA-256
retained as the project integrity key. Google's official task pages link the
BlazeFace short-range and Pose Landmarker Lite downloads to model cards that
identify those models as Apache License 2.0. Those two component rows are now
reviewed and reference the preserved MediaPipe Apache-2.0 text. The exact
EfficientDet-Lite0 float32 object remains unresolved: its official download and
identity are verified, but the Apache-2.0 evidence located for a related
TensorFlow/Kaggle variation is not byte-identical to this object. See
`docs/model-provenance.md` for hashes, GCS generations, links, and the evidence
boundary. This notice/documentation change has not been rebuilt into the latest
tested DMG.

### Earlier standalone baseline

The independently built binary below came from source export revision
`95e51ff1cf7a0c829a861e1c5772fc959270fa1f` and fresh local candidate
commit `43402e194ef1b58a04e298e07a9d9fc9f6dca0eb` on `main`. Its
`export-manifest.json` recorded and verified the SHA-256 of all 230 exported
files. The DMG SHA-256 is
`0bad1b9508592325aa90b5fd6b4b6681e6128571a47247a8155aa1260f265e25`.
The separately tested Stage B documentation-only source export is revision
`990a2a0d6b0f2cb30045e609720197adfa08af30`, candidate commit
`f854a264fdc4751120e9ff8ec0888f8829d39d2f`. Its independent clone
passed 1,366 offscreen tests in 37.46 seconds. All exported source/build inputs
match Stage A byte-for-byte; only this checklist changed (and the export
manifest consequently names a new revision and document digest). Stage B's
wheel had the same 79 member names and byte contents as Stage A's; ZIP
timestamps differed. The DMG above was **not** rebuilt from Stage B.

For any later documentation-only candidate, read its *own* commit with
`git rev-parse HEAD` and its source export revision from
`export-manifest.json` (the `source_commit` field). Its external verification
receipt must pair those values with fresh test results. Do not substitute the
Stage A or Stage B commit for a later candidate's identity, and do not treat a
documentation-only commit as evidence that the binary was rebuilt from it.

Run from that later candidate's root:

```bash
git rev-parse HEAD
python3.11 -c 'import json, pathlib; print(json.loads(pathlib.Path("export-manifest.json").read_text())["source_commit"])'
```

| Gate | Current state |
| --- | --- |
| Public commit identity | The repository owner's existing author name and email were explicitly approved for this local candidate. Do not publish them by implication; remote creation requires separate approval. |
| Source history and privacy | Stages A and B each had one fresh root commit, no old development commit, no remote or alternates, and clean `git fsck`. Each scan found zero blocking items and 24 model/icon binaries requiring provenance review. Repeat the audit on each later source export. |
| Independent install and tests | Stages A and B each used a fresh Python 3.11.15 venv and the 41-wheel hashed lock; `pip check` was clean. A: 1,366 offscreen tests passed in 44.83 seconds. B: 1,366 passed in 37.46 seconds. Non-GUI model/icon/stylesheet checks passed for both. No production GUI launch or private-photo test. |
| Wheel, runtime and app | Clean wheel built and archive tested. Source-built Perl and ExifTool upstream suites passed. App bundle inspection passed with zero violations and clean source provenance at the tested candidate commit. |
| DMG | Stage A local ad-hoc DMG: digest above; `hdiutil verify` valid. A read-only mounted copy passed static bundle inspection with zero violations. This is not drag-and-drop GUI acceptance. |
| macOS support | Apple Silicon only. Declared binary floor: macOS 26.0; only a macOS 26.3.1(a) host has been exercised. This is not a clean-Mac or 26.0 test. |
| AF behavior and private photos | Supported metadata is Nikon Z50 II JPEG `AFInfo2Version=0402`; other inputs conservatively fall back to image evidence. Earlier aggregate acceptance remains separate evidence, not a fresh sample run on this DMG. No private photos belong in Git. |
| Dependency and asset rights | **BLOCKED**: the exact EfficientDet-Lite0 float32 grant, native FFmpeg/HEIF/x265 and Qt closure, fonts/data, and compatible notices/source duties require review. BlazeFace and Pose Landmarker model provenance plus icon provenance are recorded separately; trademark similarity was not searched. |
| Distributable notices | Known snapshot inclusion **verified** in the 2026-10-11 follow-up above. Full required notice/source/relinking coverage remains **BLOCKED** pending exact dependency and asset rights review. Earlier Stage A/B artifacts do not contain the new snapshot. |
| Signature | Local ad-hoc signing only. No Developer ID signature or notarization. |
| Public release | **NOT READY**. No remote, push, repository visibility change, asset upload, or release creation is authorized by this checklist. |

Before publication, review the [README](../../README.md),
[installation instructions](../installation.md), model notices, binary contents,
source and history audits, exact release checksum, clean-Mac installation and
offline GUI/photo workflow. Obtain explicit permission for the intended repository
name and description, remote creation, public visibility, pushing, release creation,
asset upload, and any signing or notarization. Never use a private sample or
production project database as a substitute for a controlled acceptance fixture.
