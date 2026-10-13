# Building the standalone macOS metadata runtime

> These instructions build the locally ad-hoc-signed, non-notarized 0.1.0 beta.
> Review [third-party notices](../THIRD_PARTY_NOTICES.md) and the exact bundled
> component inventory before redistributing a build. A successful build verifies
> technical integrity; it is not a substitute for license review, Developer ID
> signing, notarization, or clean-machine acceptance testing.

The public macOS build is Apple Silicon only. It uses the exact source versions,
digests, compiler, and SDK recorded in `packaging/runtime-sources.json`. The build
downloads source archives only into a temporary directory, verifies SHA-256 before
extraction, builds Perl from source, runs the Perl and ExifTool upstream test suites,
and refuses to replace an existing output directory.

The selected ExifTool 13.59 release supersedes the previously exercised 13.55
production release because upstream labels 13.59 a security update and publishes a
matching SHA-256. This dependency update does not change XuanPian's AF parser,
thresholds, requested tags, or read-only invocation.

## Prerequisites

- An arm64 Mac.
- Apple Command Line Tools providing Apple clang
  `21.0.0 (clang-2100.1.1.101)` and macOS SDK `26.5`.
- The repository's Python 3.11 environment.
- Network access during this build only. The finished application must not download
  a runtime or model.

The source manifest links the upstream release pages, checksum evidence, and license
identifiers. Perl's `INSTALL` and `README.macosx` are authoritative for the build.
Those source references and the runtime's copied license files are preliminary
evidence, not a complete source/notice compliance review of the final DMG.
The build uses `-Duserelocatableinc`, a static libperl, and system library search
paths as those documents prescribe. ExifTool is staged as its verified script plus
adjacent `lib` module tree, as described by its upstream `README`.

## Build

### Notice snapshot

Both the wheel and macOS app consume `packaging/notice-manifest.json`. Preserve
upstream wording and record wheel/member provenance when updating notice texts;
review any manifest hash update rather than regenerating hashes to hide a mismatch.
The wheel's license metadata contains the snapshot at `.dist-info/licenses/` and
the app contains it at `Contents/Resources/legal/`. The app/DMG inspector compares
these bytes against the source snapshot. Missing, altered, or symlinked notice
files fail inspection. A successful integrity check does not clear the unresolved
model, artwork, native-code, source-availability, or relinking obligations.

The clean wheel builder reads notices from the Git index, so stage all reviewed
notice changes along with release inputs before invoking it. Older app/DMG
candidates do not gain the new notice set retroactively.

Always choose a new output path:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/build_metadata_runtime.py \
  --manifest packaging/runtime-sources.json \
  --output build/metadata-runtime
```

Redirect complete output to a local log outside Git when preserving build evidence.
Do not add source archives, build logs, or the generated runtime to the repository.

The script performs these upstream build commands with argument lists and checked
return codes:

```text
arch -arm64 /bin/sh Configure -des -Dprefix=<staging>/runtime/perl \
  -Duserelocatableinc -Uuseshrplib -Dcc=<xcrun clang> \
  -Uloclibpth -Dlibpth=/usr/lib -Dman1dir=none -Dman3dir=none
arch -arm64 /usr/bin/make -j4
arch -arm64 /usr/bin/make test
arch -arm64 /usr/bin/make install
<staging>/runtime/perl/bin/perl Makefile.PL
/usr/bin/make test
```

Paths containing `<staging>` are intentionally ephemeral and are not copied into the
runtime command contract.

The app builder keeps the runtime's bundled Perl interpreter and libraries, but
omits Perl's ancillary developer command-line tools from the *copied* app staging
tree. Those tools have nonrelocatable build-directory shebangs and are not part of
the XuanPian/ExifTool command contract. The source runtime remains unchanged;
the final-bundle manifest is regenerated after this trimming and signing.

## Generated manifest contract

`manifest.json` is the source-build manifest. It is separate from the final bundled
manifest regenerated after PyInstaller relocation and any authorized signing, because
those operations can rewrite native binaries.

The generated object has five fields:

- `schema_version`: integer `1`.
- `versions`: exact `perl` and `exiftool` strings.
- `command_prefix`: a constrained object with exactly `perl`, `module_dirs`, `script`,
  and `fixed_args`. Every path is relative to the runtime root. A resolver constructs
  the command as the resolved `perl`, one `-I` plus resolved path for every
  `module_dirs` entry, the resolved `script`, then `fixed_args`. For schema 1,
  `fixed_args` is exactly `["-config", ""]`; consumers must reject extra keys,
  absolute paths, traversal, unsupported switches, or files outside the trusted root.
- `files`: relative payload path to SHA-256. It excludes `manifest.json`, which cannot
  contain its own digest.
- `source_manifest_sha256`: digest of the exact `runtime-sources.json` input bytes.

The source-build manifest proves the staged runtime and source provenance. Release
inspection must validate the regenerated final-bundle manifest against the files that
will actually ship; it must not substitute this pre-bundle file list.

## Relocation checks

Copy the generated runtime to a different directory whose name contains spaces and
Chinese characters. Remove `PERL5LIB`, `PERL5OPT`, `PERLLIB`,
`PERL_LOCAL_LIB_ROOT`, `PERL_MB_OPT`, and `PERL_MM_OPT`, then run the manifest prefix
with `-ver` and with `-j -n` against a synthetic JPEG. Inspect the bundled Perl using
`file`, `otool -L`, and a printed `@INC`. Every non-system dependency and module path
must remain inside the relocated runtime. Exact commands and results from the verified
build belong in the Task 1 implementation report, not in a guessed success transcript.

## Assemble a macOS app candidate

Use CPython 3.11.15 on an arm64 Mac with Command Line Tools SDK 26.5. Install the
target wheel closure in a fresh virtual environment; the lock contains SHA-256 for
each external package and includes build/test tooling. The local XuanPian source is
not a PyPI dependency. Download wheels into a local cache if building offline, then
keep hash verification enabled:

```bash
python3.11 -m venv build/macos-arm64-venv
build/macos-arm64-venv/bin/python -m pip install --require-hashes \
  -r packaging/requirements-macos-arm64.lock
build/macos-arm64-venv/bin/python -m pip check
build/macos-arm64-venv/bin/python scripts/build_macos.py \
  --runtime build/metadata-runtime \
  --output build/macos-candidate-0.1.0-arm64
```

Always choose a new output directory. The builder rejects existing paths, including
symlinks, and does not replace the source runtime. It validates that runtime with the
application resolver, then stages a regular, symlink-free copy under
`Contents/Resources/metadata-runtime`. The frozen AF reader accepts that exact
resource directory only when it belongs to the running `.app` and still verifies its
constrained manifest and payload hashes. PyInstaller's ordinary arm64 ad-hoc signing
is followed by local signing of staged native Perl components, regeneration of the
*final* runtime manifest, and an outer app seal. This is not Developer ID signing or
notarization.

`Contents/Resources/build-provenance.json` records the source commit, whether it was
dirty, project version, Python version, SDK, lock digest, source-runtime provenance,
and final runtime-manifest digest without absolute local paths. The source version
in `pyproject.toml` is the only version authority. This candidate's bundled Perl
declares macOS 26.0 as its binary floor; `LSMinimumSystemVersion` reflects that floor,
not a claim of testing on macOS 26.0. Actual tested macOS versions, clean-machine
portability, app launch, DMG installation, and signing/notarization status must be
recorded separately before any public release claim.

## Inspect and package a candidate

Inspect the exact `.app` that will ship. The gate returns JSON violations and a
nonzero exit status on failure. It checks all Mach-O objects and required load
paths (including inherited loader runpaths), arm64 slices and deployment targets,
executable-script interpreters, the isolated bundled Perl `@INC`, bundle links,
required resources and pinned model digests, the strict final runtime manifest,
version/provenance, and the local ad-hoc code seal. An optional, unused debug
runpath alone is not a failure. The ExifTool script's `/usr/bin/env perl` shebang
is allowed only because the validated runtime manifest always passes that exact
script to the bundled Perl executable explicitly.
Required `@rpath` loads are checked in search order: an earlier private absolute
runpath is a failure even if a later bundled copy exists (and even if that private
path is absent on the build host). An absolute path into the current app is also
nonrelocatable. System-path exceptions use normalized and canonical paths, so
`..` traversal or a symlink cannot disguise a private path as `/usr/lib/` or
`/System/Library/`. Unused runpaths remain non-blocking.

```bash
build/macos-arm64-venv/bin/python scripts/verify_macos_bundle.py \
  build/macos-candidate-0.1.0-arm64/选片APP.app
build/macos-arm64-venv/bin/python scripts/build_dmg.py \
  --app build/macos-candidate-0.1.0-arm64/选片APP.app \
  --output build/选片APP-0.1.0-arm64-local-ad-hoc.dmg
shasum -a 256 build/选片APP-0.1.0-arm64-local-ad-hoc.dmg
hdiutil verify build/选片APP-0.1.0-arm64-local-ad-hoc.dmg
```

Choose a new output path each time. The DMG builder refuses an existing path or
sidecar (including dangling symlinks), stages with `ditto` preserving links,
permissions, metadata, and extended attributes, creates an `/Applications`
shortcut, and writes a `.dmg.sha256` sidecar. It does not install or overwrite an
app in `/Applications`. Publication takes an advisory lock on the output parent
directory and creates both final names with atomic no-replace links; a sidecar
collision rolls back only the image inode this invocation published. An existing
or concurrently created output or sidecar is never intentionally overwritten.

For local static copy verification, mount the DMG read-only at a fresh temporary
mountpoint, `ditto` the app into a fresh verification directory, detach the image,
and rerun `verify_macos_bundle.py` against the copied `.app`. A successful static
copy check is not a GUI launch or clean-machine acceptance test. Do not launch
the copied app without separate authorization. This artifact is locally ad-hoc
signed, not Developer ID signed or notarized, and must not be described as a
publicly releasable signed distribution.

The declared binary floor is macOS 26.0; only the macOS 26.3.1(a) build host
has actually been exercised. A clean Mac, drag-and-drop GUI launch, offline
workflow, exact third-party notice inclusion, and model/native rights remain
unverified release gates. The wheel staging script currently copies only
`pyproject.toml` and `src/`; root `LICENSE` and third-party notices must not be
assumed to appear in a wheel or the current r5 app/DMG.
