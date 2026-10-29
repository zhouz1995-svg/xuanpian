# Intel Mac candidate design

## Scope and approach

Produce a standalone x86_64 macOS candidate targeting macOS 12.0, without changing the UI, technical-v8, database schema, photo workflows or model versions. Extend the existing native Mac tooling with an explicit architecture parameter, defaulting to arm64. Build Intel on the actual `macos-15-intel` GitHub runner, never by renaming or executing an arm64 bundle under emulation. Universal2 delivery would duplicate the Perl/runtime closure and complicate slice-specific deployment gates; separate architecture candidates are the selected approach.

The human has authorized designing and executing subsequent work without repetitive permission questions. Windows 10/11 hands-on testing remains deferred. Do not merge, modify repository visibility, create a formal Release, buy/install signing credentials, or claim old-OS acceptance.

## Build profiles

Accepted architectures are exactly `arm64` and `x86_64`. Existing calls keep their arm64 default. The selected architecture must match the native Python host and every shipped Mach-O object must contain that slice. Unknown architecture or host/target mismatch fails before building. Preserve existing no-overwrite, symlink, hash, clean-source, relocation and ad-hoc signature checks.

Keep `packaging/runtime-sources.json` unchanged for arm64. Add `packaging/runtime-sources-macos-x86_64.json`, using the same Perl 5.44.0 and ExifTool 13.59 source URLs and SHA-256, deployment target `12.0`, Xcode 16.4 at `/Applications/Xcode_16.4.app/Contents/Developer`, SDK `15.5`, and `Apple clang version 17.0.0 (clang-1700.0.13.5)`. CI verifies these values rather than silently selecting a newer compiler/SDK. The upstream runner image lists Xcode 16.4 and SDK 15.5; any actual mismatch is a diagnostic failure, not permission to float the pin.

The runtime builder launches Configure/make/test/install with `/usr/bin/arch -<target>`, preserves the sanitized environment and upstream Perl/ExifTool test suites, and retains the existing schema-1 runtime layout and exact key contract. That schema does not encode CPU architecture; actual Mach-O checks and source-profile provenance bind the payload to its target.

Add `packaging/requirements-macos-x86_64.lock` with the same package versions as the current arm64 lock. Resolve from pyproject extras `test`, `formats`, `build`, constrained by the arm64 pins; retain hashes. Download only binary wheels selected for `macosx_12_0_x86_64` before offline installation. The app spec takes the actual native host architecture, while builder preflight enforces the explicitly requested profile. No inherited architecture environment override.

## Inspection and provenance

Architecture-aware interfaces: `build_app(..., architecture='arm64')`, `inspect_bundle(app, *, architecture='arm64')`, `build_dmg(app, output, *, architecture='arm64')` and matching CLI `--architecture`. The native metadata inspector selects the requested slice with `otool -arch <target>`; `lipo -archs` still proves that slice exists. This avoids applying a different slice's minimum-OS load command to an Intel candidate.

Provenance records the selected architecture and corresponding dependency lock path/hash. Check actual runtime source-manifest identity against the chosen approved profile, final runtime hashes, project version, clean source revision, relative/nonprivate provenance, model hashes, notice bytes and ad-hoc signatures. Keep all dependency graph traversal, active-chain cycle handling and external-loader rejection intact.

Preserve license/notice files from the actual selected Intel wheels, not merely an arm64 filename. Record wheel filename/SHA-256 and member identity in an Intel notice receipt, reuse existing identical snapshot bytes where possible and add unmatched texts to the hashed notice set. This is notice preservation, not a blanket license/security clearance; the existing blocked-release flags and third-party review boundaries remain.

## Native verification and artifacts

Use Python 3.11.15 and the pinned Xcode profile on `macos-15-intel`. Tests precede the runtime build; full upstream runtime suites may be long-running and must not be skipped to make the pipeline green. Build a fresh `.app`, inspect as x86_64, perform offscreen startup and actual frozen child-process work loading Pillow 13.0.0, creating/closing a project database, invoking technical-v8 with portrait assessment, and reading dimensions via bundled ExifTool. Verify that imported task modules are inside the app. On POSIX, the frozen probe must temporarily set both multiprocessing executable and `sys.executable` to the bundled executable, restoring them afterward; this differs from Windows venv-launcher handling. Bound failures and terminate only this probe's own workers before executor shutdown.

Generate a JPEG and project only inside fresh synthetic temporary directories, including Chinese/space-containing paths. Check the photo SHA-256 before/after. Build a new `XuanPian-0.1.0-macos12-x86_64-local-ad-hoc.dmg` and checksum, verify with hdiutil, mount read-only at an owned temporary mount point, copy the app with ditto, recheck signatures/resources/native slices and repeat frozen/metadata checks on the relocated copy; detach only that mount in finally.

Upload the DMG, checksum, verification JSON and build-provenance JSON only after all gates pass. Retain public CI artifacts for 7 days; no user photos, local production database or secrets enter artifacts. CI on newer Intel macOS proves a native candidate, not clean macOS 12 compatibility, notarization or formal distribution readiness.

## Completion

Create a separate PR stacked on `feat/windows-installer`; keep the previous PR unchanged. Download the successful candidate into a new local output directory and compare actual DMG hash with both sidecar and CI verification JSON. Document exact source/CI identity and limits in `docs/verification/macos-intel-installer.md`, update bilingual README/status links and the public source allowlist, and rerun source export/privacy checks. Do not label native packaging complete if any runtime, app, DMG or mounted-copy gate is still failing.
