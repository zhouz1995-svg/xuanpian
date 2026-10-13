# XuanPian (选片)

XuanPian is a privacy-friendly desktop photo-review assistant. It helps photographers import a folder, compare likely bursts, review focus and exposure cues, mark keepers, and export a selection without uploading photos to a cloud service.

Version 0.1.0 is a beta for Apple Silicon Macs. Analysis results are review aids, not an automatic decision about whether a photo is usable.

## Highlights

- Local JPEG and supported RAW import and preview.
- Fast keyboard-driven culling, ratings, marks, filters, and burst grouping.
- Subject-aware focus, exposure, and portrait-review cues.
- Persistent local project state, so a review can be closed and reopened.
- Explicit move and export workflows; analysis itself does not rewrite originals.
- Bundled models and metadata runtime, with no account or first-run model download.

## Download and install

Download the current `.dmg` and its SHA-256 checksum from the GitHub Releases page. The packaged beta supports Apple Silicon (`arm64`) only; Intel Macs and Windows are not supported.

This beta is locally ad-hoc signed and is **not Apple-notarized**. macOS may display a security warning. Do not disable Gatekeeper or other system-wide security protections. See the [installation guide](docs/installation.md) for the checksum and drag-to-Applications steps.

## Analysis scope and limitations

Focus analysis prioritizes detected subjects. For supported JPEG metadata, it can also evaluate an AF-aware region. The exact AF metadata mapping currently supported is `Make=NIKON CORPORATION`, `Model=NIKON Z50_2`, `AFInfo2Version=0402`. Other cameras, RAW files, unsupported or missing metadata, invalid coordinates, and metadata-runtime failures use a conservative image-based fallback; they are not evidence of an AF point. There is no universal eye-sharpness guarantee, especially with shallow depth of field, motion, occlusion, or very small subjects.

Treat “review suggested” as a prompt to inspect the subject at useful magnification, not as a rejection. Keep backups of irreplaceable photos and check the destination before any explicit move or export action. More detail is available in [privacy and data handling](PRIVACY.md).

## Build from source

The project requires Python 3.11 and is licensed under [GPL-3.0-only](LICENSE). On an arm64 Mac, install the hash-pinned dependencies and run the test suite:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Run the development app with:

```bash
.venv/bin/python -m src.main
```

For the standalone application and DMG workflow, see [building for macOS](docs/building-macos.md). Contributions should follow [CONTRIBUTING.md](CONTRIBUTING.md); bug reports must not include private photos or metadata.

## Release notes

XuanPian 0.1.0 is an early public beta. The current build is intended for evaluation on Apple Silicon and has not completed Apple Developer ID signing or notarization. Third-party libraries, models, and assets retain their own licenses; the bundled notice set and the project’s current provenance review are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
