# Intel Mac Candidate Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development task-by-task with independent review.

**Goal:** Build and exercise a self-contained Intel Mac x86_64 candidate DMG targeting macOS 12.0.

**Architecture:** Extend the existing native Mac builders and inspectors with an explicit architecture while preserving arm64 defaults. Resolve Intel wheels separately, retain actual notice inputs, and validate the resulting app and relocated DMG copy on an Intel runner.

**Tech Stack:** Python 3.11.15, PyInstaller 6.21.0, Perl 5.44.0, ExifTool 13.59, Qt 6.7.3, Pillow 13.0.0, ONNX Runtime 1.19.2, Xcode 16.4.

**Spec:** `docs/superpowers/specs/2026-10-27-macos-intel-installer-design.md`.

## Global Constraints

- Target x86_64 macOS 12.0; native `macos-15-intel`, no Rosetta or universal2 deliverable.
- Keep arm64 APIs/defaults, runtime schema 1, UI, technical-v8, database schema, photo workflow and dependency versions unchanged.
- Fixed source hashes; SDK 15.5 and Apple clang 17.0.0 (clang-1700.0.13.5) via Xcode 16.4; no floating compiler substitution.
- New output paths only; no overwrite, host-runtime fallback, weakened native/hash/signature checks or skipped upstream runtime tests.
- Only generated synthetic photos/projects in CI. Public artifacts retained 7 days, local ad-hoc signature, no formal Release/merge or old-OS acceptance claim.
- Windows 10/11 hands-on testing remains deferred. No change to the existing Windows application path.

## Task 1: Architecture-aware Mac build and inspection

**Files:** Modify `scripts/build_metadata_runtime.py`, `scripts/build_macos.py`, `scripts/verify_macos_bundle.py`, `scripts/build_dmg.py`, `packaging/xuanpian.spec`; create `packaging/runtime-sources-macos-x86_64.json`; extend `tests/test_metadata_runtime_build.py`, `tests/test_macos_build.py`, `tests/test_macos_bundle.py`, `tests/test_build_dmg.py`.

**Interfaces:** Keep all current arm64 defaults. Add `architecture: str = 'arm64'` to `build_app`, `_preflight`, `_provenance`, `inspect_bundle`, native dependency/identity inspection and `build_dmg` (keyword-only for public APIs). Add `macos_lock(architecture: str) -> Path` and `macos_runtime_sources(architecture: str) -> Path` in build_macos, rejecting unknown values and preserving existing `LOCK` behavior for arm64 tests. `_validate_host(build, architecture='arm64')` consumes source target; `_native_metadata(path, architecture='arm64')` queries that slice explicitly. CLI wrappers pass `--architecture`. The x86_64 lock file is supplied by Task 2; Task 1 unit fixtures can supply a temporary Intel lock.

- [x] **Step 1:** Add red behavior tests for accepting Intel source profile, rejecting foreign/unknown host architecture, preserving no-overwrite, and selecting the matching provenance lock. Example native-slice gate regression:

```python
def test_intel_gate_rejects_arm_only_metadata(tmp_path, monkeypatch):
    app, binary = _native(tmp_path, "")
    monkeypatch.setattr(gate, "_native_metadata", lambda path, architecture="arm64":
        gate.NativeInfo((), (), "arm64", "12.0"))
    assert any(v["kind"] == "architecture" for v in
        gate._inspect_native_dependencies(app, binary, [binary], architecture="x86_64"))
```

- [x] **Step 2:** Run `python -m pytest -q tests/test_metadata_runtime_build.py tests/test_macos_build.py tests/test_macos_bundle.py tests/test_build_dmg.py`; capture expected red failures in the task report.
- [x] **Step 3:** Implement native architecture selection and the Intel source profile from the spec. Preserve arm64 source manifest bytes. Propagate expected architecture through graph and provenance checks. Select Mach-O load commands with `otool -arch`; adapt existing tool fakes for the explicit slice and retain cycle/external-loader tests. Require the provenance source-manifest and lock identities for the selected profile. Do not modify engine runtime schema or application modules.
- [x] **Step 4:** Run focused tests, then the complete suite with `QT_QPA_PLATFORM=offscreen`. Add unsupported architecture, Intel-only accepted, arm-only rejected, and slice-specific minOS tests. Existing arm64 invocation must retain its behavior.
- [x] **Step 5:** Self-review and commit the task; report SHA, exact RED/GREEN commands/output and any limitations. No push or subagents from the implementer.

## Task 2: Intel dependency closure, native candidate verifier and CI

**Files:** Create `packaging/requirements-macos-x86_64.lock`, `scripts/check_macos_candidate.py`, `scripts/verify_macos_wheel_inputs.py`, `.github/workflows/macos-intel-package.yml`, `packaging/macos-x86_64-wheel-notices.json` and any unmatched notice texts under `packaging/licenses/macos-x86_64/`. Modify `packaging/notice-manifest.json`, `packaging/public-files.json`, `THIRD_PARTY_NOTICES.md`, `docs/building-macos.md`, and `.gitattributes` only if needed to retain raw vendor notice bytes; add `tests/test_macos_candidate.py`, `tests/test_macos_wheel_inputs.py` and notice contract tests in `tests/test_license_notices.py` as needed. Preserve arm64 lock bytes.

**Interfaces:** Consume Task 1 architecture-aware CLIs and `macos_lock`/`macos_runtime_sources`. New verifier interface `check(candidate: Path, *, architecture: str = 'x86_64') -> dict` expects `candidate/选片APP.app` and exactly one Intel DMG, writes `candidate/verification.json` only after success. Its frozen probe takes explicit executable/framework paths and returns Pillow version, module containment, project-store status and technical-v8/portrait assessment. On POSIX, set and restore `sys.executable` plus multiprocessing executable; use only owned worker handles for failure cleanup. Do not refactor the existing Windows verifier.

The stdlib-only `verify_macos_wheel_inputs.py` exposes `verify_wheel_inputs(wheel_directory: Path, receipt: Path) -> None` and CLI `--wheels <directory> --receipt <json>` so it can run before dependency installation. Receipt schema: `schema_version=1`, `target={platform:macos,architecture:x86_64,minimum_macos:12.0,python:3.11}`, `rights_status=unreviewed`, `wheel_files={filename:{sha256:<digest>,notices:[{member:<zip member>,member_sha256:<digest>,snapshot:<source-relative text>,snapshot_sha256:<digest>}]}}`. Check the exact wheel set, each archive digest and each listed member/snapshot's actual bytes; reject unsafe paths and symlink inputs. The receipt is a public source build input, not a new legal-payload file type: preserve notice_payload's existing exact required documents plus all `packaging/licenses/**/*.txt` contract. Only the actual notice texts enter that hashed legal payload.

- [ ] **Step 1:** Generate the Intel lock with:

```sh
uv pip compile pyproject.toml --extra test --extra formats --extra build --python-platform x86_64-apple-darwin --python-version 3.11 --constraint packaging/requirements-macos-arm64.lock --generate-hashes --no-annotate --output-file packaging/requirements-macos-x86_64.lock
python -m pip download --platform macosx_12_0_x86_64 --only-binary=:all: --require-hashes -r packaging/requirements-macos-x86_64.lock -d /tmp/xuanpian-intel-wheels-unique
```

Use a real fresh directory from tempfile/mktemp instead of a shared fixed path. Check all resolved package versions match arm64 pins. Retain exact wheel filename/hash and license-member bytes in a receipt; deduplicate against matching existing hashed notice snapshots, add unmatched texts with their own hashes and public allowlist entries. Keep blocked-release flags. Candidate CI verifies selected wheel bytes/receipt before offline installation; never silently choose a macOS-14 wheel.

- [ ] **Step 2:** Write failing verifier tests for non-native host, existing verification output, mismatched DMG checksum, and module-containment rejection. Exercise actual owned-process termination on timeout as in the existing Windows regression, without fake success signals or source-image mutation. Run focused tests and save RED evidence.
- [ ] **Step 3:** Implement the verifier per spec: inspect original app as x86_64; offscreen boot; frozen Pillow 13.0.0, real project store, technical-v8 and portrait assessment; bundled ExifTool width/height; photo SHA protection; `hdiutil verify`; owned read-only mount/copy/detach in finally; repeat on relocated Chinese-path copy; successful JSON with source revision and DMG SHA. Preserve prior verification output instead of overwriting. Bound subprocess and pool error cleanup.
- [ ] **Step 4:** Add CI on pull_request/workflow_dispatch using `macos-15-intel`, `DEVELOPER_DIR=/Applications/Xcode_16.4.app/Contents/Developer`, `MACOSX_DEPLOYMENT_TARGET=12.0`, `QT_QPA_PLATFORM=offscreen`, uv 0.11.7 and Python 3.11.15. Reuse current fully pinned checkout/setup-uv/upload-artifact action SHAs. Run native tests, target-wheel download/hash receipt/offline install/pip check, source runtime build (including both upstream suites), architecture-aware app build, DMG build and full candidate verification. Use fresh RUNNER_TEMP paths, 90-minute job bound and a bounded smoke step. Upload only DMG, sidecar, verification and provenance on success for 7 days; no Release.
- [ ] **Step 5:** Run focused and full local tests. Self-review and commit. Report exact evidence and remaining native-only checks; no push or subagents from implementer. The controller creates the stacked PR and drives native CI; native failures return to the responsible implementer for diagnosis/regression fixes.

## Task 3: Verified evidence and handoff

**Files:** Create `docs/verification/macos-intel-installer.md`; modify `docs/release-status.md`, `README.md`, `packaging/public-files.json` and this plan's checkboxes. Documentation only, after actual native results.

**Interfaces:** Consume the successful CI run, branch/test-merge source revisions, actual downloaded DMG/sidecar/provenance/verification JSON and full test/audit results supplied in the task brief. Historical candidate reports retain their own identity and hashes.

- [ ] **Step 1:** Controller pushes the reviewed feature branch and creates/attaches a PR based on `feat/windows-installer`. Wait for actual native gates; read bounded failing logs and delegate minimal root-cause fixes rather than skipping gates. No automatic merge or formal Release.
- [ ] **Step 2:** Controller downloads the successful artifact to a new directory under workspace outputs, compares actual DMG SHA-256 with sidecar and verification JSON, and checks source/provenance identities. Do not run Intel binaries on this arm64 host or install Rosetta.
- [ ] **Step 3:** Document exact tested source/run/DMG digest, native component/floor results, upstream test outcomes, frozen and relocated-copy results, signature status and limits. Add bilingual README/status links and allowlist. Explicitly distinguish newer-OS Intel CI from macOS 12 clean-machine acceptance and leave Windows hands-on deferred.
- [ ] **Step 4:** Controller runs full local regression and fresh committed-source allowlist export/audit. Preserve known provenance-review findings; zero blocking is not rights/security clearance. Obtain broad final branch review and resolve load-bearing findings.
- [ ] **Step 5:** Commit/push evidence, report candidate path and PR with remaining acceptance constraints. Keep branch/worktree for feedback; no published Release or merge.
