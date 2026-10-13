# Install XuanPian on macOS

XuanPian 0.1.0 is a beta for Apple Silicon (`arm64`) Macs. Intel Macs and Windows are not supported by this package. Python, Qt, Perl/ExifTool, and the analysis models are included in the app; normal installation does not require Homebrew or a first-run download.

## Before opening the download

Download the `.dmg` and published SHA-256 checksum from the same GitHub Release. In Terminal, verify the file before opening it:

```bash
shasum -a 256 XuanPian-0.1.0-arm64.dmg
```

The result must exactly match the checksum shown on the release page. A differently named beta file can be verified by replacing the example filename with the downloaded filename.

## Install

1. Open the verified `.dmg`.
2. Drag `选片APP.app` to the `Applications` shortcut.
3. Eject the disk image.
4. Open the copied app from Applications.

There is no administrator installer script. The 0.1.0 beta is locally ad-hoc signed and is **not Apple-notarized**, so macOS may warn or prevent it from opening. Do not disable Gatekeeper or other system-wide security controls. If you are not comfortable running a non-notarized beta, build from source or wait for a future notarized release.

The packaged binary declares macOS 26.0 as its minimum target; the release notes state the versions actually tested. Before importing irreplaceable work, keep a backup and try a small copied folder first. Analysis does not rewrite originals, but explicit move and export actions can change file locations or create output files.

See [privacy and data handling](../PRIVACY.md), [analysis limitations](../README.md#analysis-scope-and-limitations), and [building from source](building-macos.md).
