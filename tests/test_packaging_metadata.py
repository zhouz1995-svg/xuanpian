"""Static metadata checks for the PyInstaller packaging deliverable (P3 Task 5).

These checks pass without PyInstaller installed; the actual build is executed
separately on a machine with PyInstaller available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import runpy
import subprocess
import sys
import tomllib
import zipfile

import pytest

from scripts import build_wheel

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "packaging" / "xuanpian.spec"
MANIFEST = REPO_ROOT / "src" / "xuanpian" / "resources" / "models" / "manifest.json"
CLEAN_WHEEL_BUILDER = REPO_ROOT / "scripts" / "build_wheel.py"


def test_spec_declares_app_and_bundle() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert 'name="选片APP"' in text
    assert 'name="选片APP.app"' in text
    assert 'bundle_identifier="com.xuanpian.app"' in text


def test_spec_forces_dark_macos_chrome() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert '"NSUserInterfaceStyle": "Dark"' in text


def test_spec_points_at_existing_icon_and_entry() -> None:
    assert (REPO_ROOT / "assets" / "icons" / "app-icon.icns").is_file()
    assert (REPO_ROOT / "src" / "main.py").is_file()


def test_spec_collects_runtime_assets() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert "styles.qss" in text
    assert "app-icon.png" in text
    assert '"src/xuanpian/ui"' in text
    assert '"src/xuanpian/resources/models"' in text


def test_spec_collects_onnxruntime_and_rawpy() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert "collect_submodules(\"onnxruntime\")" in text
    assert '"rawpy"' in text


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle-native dylib contract")
def test_spec_passes_onnxruntime_runtime_library_to_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AnalysisProbe:
        def __init__(self, *_args, **kwargs) -> None:
            self.binaries = kwargs["binaries"]
            self.datas = kwargs["datas"]
            self.pure = ()
            self.scripts = ()

    monkeypatch.setattr(
        "PyInstaller.utils.hooks.collect_submodules",
        lambda _package: [],
    )
    namespace = runpy.run_path(
        str(SPEC),
        init_globals={
            "SPECPATH": str(SPEC.parent),
            "Analysis": AnalysisProbe,
            "PYZ": lambda *_args, **_kwargs: object(),
            "EXE": lambda *_args, **_kwargs: object(),
            "COLLECT": lambda *_args, **_kwargs: object(),
            "BUNDLE": lambda *_args, **_kwargs: object(),
        },
    )

    assert any(
        Path(source).name == "onnxruntime_pybind11_state.so"
        and destination == "onnxruntime/capi"
        for source, destination in namespace["a"].binaries
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS bundle metadata")
def test_spec_embeds_project_version_and_runtime_macos_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AnalysisProbe:
        pure = ()
        scripts = ()
        binaries = ()
        datas = ()

        def __init__(self, *_args, **_kwargs) -> None:
            pass

    captured: dict[str, object] = {}

    def bundle_probe(*_args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("PyInstaller.utils.hooks.collect_submodules", lambda _: [])
    runpy.run_path(
        str(SPEC),
        init_globals={
            "SPECPATH": str(SPEC.parent),
            "Analysis": AnalysisProbe,
            "PYZ": lambda *_args, **_kwargs: object(),
            "EXE": lambda *_args, **_kwargs: object(),
            "COLLECT": lambda *_args, **_kwargs: object(),
            "BUNDLE": bundle_probe,
        },
    )
    metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    info = captured["info_plist"]
    assert isinstance(info, dict)
    assert info["CFBundleShortVersionString"] == metadata["project"]["version"]
    assert info["CFBundleVersion"] == metadata["project"]["version"]
    assert info["LSMinimumSystemVersion"] == "26.0"


def _project_scripts() -> dict[str, str]:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return project["project"]["scripts"]


def test_project_exposes_only_the_product_entry_point() -> None:
    assert _project_scripts() == {"xuanpian": "src.main:main"}


def test_model_manifest_contains_only_shared_subject_models() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert set(manifest["models"]) == {
        "face_detector",
        "object_detector",
    }
    filenames = {entry["filename"] for entry in manifest["models"].values()}
    assert filenames == {
        "yunet.onnx",
        "yolox_nano.onnx",
    }
    assert not (MANIFEST.parent / "face_landmarker.task").exists()


def test_release_path_parser_rejects_windows_unsafe_components() -> None:
    unsafe_paths = (
        b"",
        b".",
        b"../payload.py",
        b"/src/payload.py",
        b"C:payload.py",
        b"C:/payload.py",
        b"//server/share/payload.py",
        b"src/./payload.py",
        b"src/../payload.py",
        b"src/C:payload.py",
        b"src/NUL.py",
        b"src/con",
        b"src/CON .txt",
        b"src/AuX.tar.gz",
        b"src/COM9.bin",
        b"src/COM1 .txt",
        b"src/lPt1",
        "src/COM¹.py".encode("utf-8"),
        "src/lpt².txt".encode("utf-8"),
        b"src/name.",
        b"src/name ",
        b"src/name./payload.py",
        b"src/name /payload.py",
        b"src\\payload.py",
        b"src/control\x1f.py",
        b"src/nul\x00.py",
        b"src/less<than.py",
        b"src/greater>than.py",
        b'src/double"quote.py',
        b"src/pipe|name.py",
        b"src/question?.py",
        b"src/star*.py",
    )

    for raw_path in unsafe_paths:
        with pytest.raises(build_wheel.CleanWheelBuildError):
            build_wheel._release_path(raw_path)


def test_release_path_parser_accepts_portable_unicode_names() -> None:
    expected = "src/合法 名称/über-file.py"
    expected_nbsp = "src/legal\N{NO-BREAK SPACE}name.py"

    assert build_wheel._release_path(expected.encode("utf-8")).as_posix() == expected
    assert (
        build_wheel._release_path(expected_nbsp.encode("utf-8")).as_posix()
        == expected_nbsp
    )
    assert build_wheel._release_path(b"pyproject.toml").as_posix() == "pyproject.toml"


@pytest.mark.parametrize(
    "raw_paths",
    [
        (b"src/A.py", b"src/a.py"),
        (b"src/Stra\xc3\x9fe.py", b"src/STRASSE.py"),
        (b"src/a", b"src/a/b.py"),
        (b"src/a/b.py", b"src/a"),
    ],
)
def test_windows_staging_parser_rejects_collisions(
    raw_paths: tuple[bytes, bytes],
) -> None:
    paths = tuple(build_wheel._release_path(raw_path) for raw_path in raw_paths)

    with pytest.raises(build_wheel.CleanWheelBuildError):
        build_wheel._validate_staging_paths(paths)


def test_windows_staging_parser_accepts_distinct_portable_paths() -> None:
    paths = tuple(
        build_wheel._release_path(raw_path)
        for raw_path in (
            b"pyproject.toml",
            "src/合法 名称/über-file.py".encode("utf-8"),
            b"src/a.py",
            b"src/a-data/b.py",
        )
    )

    build_wheel._validate_staging_paths(paths)


def test_windows_staging_key_normalizes_case_and_trailing_dots_spaces() -> None:
    assert build_wheel._windows_staging_key(
        PurePosixPath("src/Name. ")
    ) == ("src", "name")


def test_index_entry_parser_applies_windows_staging_collision_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid = b"0" * 40
    index_output = b"".join(
        (
            b"100644 " + oid + b" 0\tpyproject.toml\0",
            b"100644 " + oid + b" 0\tsrc/A.py\0",
            b"100644 " + oid + b" 0\tsrc/a.py\0",
        )
    )

    def run_git(
        _repo_root: Path, arguments: list[str]
    ) -> subprocess.CompletedProcess[bytes]:
        assert arguments == [
            "ls-files",
            "--stage",
            "-z",
            "--",
            "pyproject.toml",
            "src",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "packaging/licenses",
            "packaging/notice-manifest.json",
            "packaging/third-party-components.json",
        ]
        return subprocess.CompletedProcess(
            arguments, 0, stdout=index_output, stderr=b""
        )

    monkeypatch.setattr(build_wheel, "_run_git", run_git)

    with pytest.raises(
        build_wheel.CleanWheelBuildError,
        match="Windows staging path collision",
    ):
        build_wheel._indexed_release_entries(Path("unused"))


def _workspace_build_artifact_snapshot() -> tuple[tuple[str, int, int], ...]:
    roots = [
        REPO_ROOT / "build",
        REPO_ROOT / "dist",
        *REPO_ROOT.glob("*.whl"),
        *REPO_ROOT.glob("*.egg-info"),
        *(REPO_ROOT / "src").rglob("*.egg-info"),
    ]
    entries: list[tuple[str, int, int]] = []
    for root in roots:
        paths = (root,) if not root.is_dir() else (root, *root.rglob("*"))
        for path in paths:
            if not path.exists():
                continue
            stat = path.stat()
            entries.append(
                (
                    path.relative_to(REPO_ROOT).as_posix(),
                    stat.st_size,
                    stat.st_mtime_ns,
                )
            )
    return tuple(sorted(entries))


def _build_clean_wheel(tmp_path: Path) -> Path:
    assert CLEAN_WHEEL_BUILDER.is_file(), "clean wheel build entry is required"
    before = _workspace_build_artifact_snapshot()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    build = _run_clean_wheel_builder(wheelhouse)
    assert build.returncode == 0, build.stdout + build.stderr
    assert _workspace_build_artifact_snapshot() == before
    wheels = tuple(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _run_clean_wheel_builder(wheelhouse: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CLEAN_WHEEL_BUILDER),
            "--outdir",
            str(wheelhouse),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CACHE_DIR": str(wheelhouse.parent / "pip-cache"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _initialize_release_repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    package = repository / "src" / "probe"
    package.mkdir(parents=True)
    (repository / "pyproject.toml").write_text(
        """\
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "index-probe"
version = "0.0.0"

[tool.setuptools]
package-dir = {"" = "src"}
packages = ["probe"]
""",
        encoding="utf-8",
    )
    (package / "__init__.py").write_bytes(b'ORIGIN = "index"\n')
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "add", "pyproject.toml", "src"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        check=True,
    )
    builder = repository / "scripts" / "build_wheel.py"
    builder.parent.mkdir()
    builder.write_bytes(CLEAN_WHEEL_BUILDER.read_bytes())
    return repository, package


def _run_repository_builder(
    repository: Path, wheelhouse: Path
) -> subprocess.CompletedProcess[str]:
    wheelhouse.mkdir()
    return subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "build_wheel.py"),
            "--outdir",
            str(wheelhouse),
        ],
        cwd=repository,
        env={
            **os.environ,
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CACHE_DIR": str(wheelhouse.parent / "pip-cache"),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_clean_wheel_rejects_tracked_symlink_without_reading_external_file(
    tmp_path: Path,
) -> None:
    repository, package = _initialize_release_repository(tmp_path)
    sentinel = b"EXTERNAL-CONTENT-MUST-NOT-ENTER-WHEEL\n"
    external = tmp_path / "external.py"
    external.write_bytes(sentinel)
    tracked_link = package / "external.py"
    try:
        tracked_link.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    subprocess.run(
        ["git", "-C", str(repository), "add", "src/probe/external.py"],
        check=True,
    )
    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--stage", "--", "src"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "120000" in tracked.stdout

    wheelhouse = tmp_path / "wheelhouse"
    result = _run_repository_builder(repository, wheelhouse)

    assert external.read_bytes() == sentinel
    assert result.returncode != 0, "tracked symlinks must be rejected"
    assert result.stderr == (
        "clean wheel build failed: unsupported indexed release mode 120000: "
        "src/probe/external.py\n"
    )
    assert str(repository) not in result.stderr
    assert "Traceback" not in result.stderr
    for wheel in wheelhouse.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            assert sentinel not in b"".join(
                archive.read(member) for member in archive.namelist()
            )


def test_clean_wheel_rejects_unstaged_tracked_release_input(tmp_path: Path) -> None:
    repository, package = _initialize_release_repository(tmp_path)
    (package / "__init__.py").write_bytes(b'ORIGIN = "worktree"\n')

    result = _run_repository_builder(repository, tmp_path / "wheelhouse")

    assert result.returncode == 1
    assert result.stderr == (
        "clean wheel build failed: unstaged release input must be staged: "
        "src/probe/__init__.py\n"
    )
    assert str(repository) not in result.stderr
    assert not tuple((tmp_path / "wheelhouse").glob("*.whl"))


def test_clean_wheel_rejects_untracked_release_input(tmp_path: Path) -> None:
    repository, package = _initialize_release_repository(tmp_path)
    (package / "new_module.py").write_bytes(b'ORIGIN = "untracked"\n')

    result = _run_repository_builder(repository, tmp_path / "wheelhouse")

    assert result.returncode == 1
    assert result.stderr == (
        "clean wheel build failed: untracked release input must be staged: "
        "src/probe/new_module.py\n"
    )
    assert str(repository) not in result.stderr
    assert not tuple((tmp_path / "wheelhouse").glob("*.whl"))


def test_clean_wheel_reads_index_blob_not_worktree_symlink_target(
    tmp_path: Path,
) -> None:
    repository, package = _initialize_release_repository(tmp_path)
    indexed = (package / "__init__.py").read_bytes()
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "update-index",
            "--assume-unchanged",
            "src/probe/__init__.py",
        ],
        check=True,
    )
    external = tmp_path / "external.py"
    sentinel = b'ORIGIN = "external-worktree-swap"\n'
    external.write_bytes(sentinel)
    worktree_source = package / "__init__.py"
    worktree_source.unlink()
    try:
        worktree_source.symlink_to(external)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")
    clean = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "diff",
            "--quiet",
            "--",
            "pyproject.toml",
            "src",
        ]
    )
    assert clean.returncode == 0

    wheelhouse = tmp_path / "wheelhouse"
    result = _run_repository_builder(repository, wheelhouse)

    assert result.returncode == 0, result.stdout + result.stderr
    assert external.read_bytes() == sentinel
    wheel = next(wheelhouse.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read("probe/__init__.py") == indexed
        assert sentinel not in b"".join(
            archive.read(member) for member in archive.namelist()
        )


def test_clean_wheel_allows_staged_release_change_and_builds_index_blob(
    tmp_path: Path,
) -> None:
    repository, package = _initialize_release_repository(tmp_path)
    staged = b'ORIGIN = "staged-index-change"\n'
    (package / "__init__.py").write_bytes(staged)
    subprocess.run(
        ["git", "-C", str(repository), "add", "src/probe/__init__.py"],
        check=True,
    )

    wheelhouse = tmp_path / "wheelhouse"
    result = _run_repository_builder(repository, wheelhouse)

    assert result.returncode == 0, result.stdout + result.stderr
    with zipfile.ZipFile(next(wheelhouse.glob("*.whl"))) as archive:
        assert archive.read("probe/__init__.py") == staged


def test_clean_wheel_rejects_gitlink_index_entry(tmp_path: Path) -> None:
    repository, _package = _initialize_release_repository(tmp_path)
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},src/vendor",
        ],
        check=True,
    )

    result = _run_repository_builder(repository, tmp_path / "wheelhouse")

    assert result.returncode == 1
    assert result.stderr == (
        "clean wheel build failed: unsupported indexed release mode 160000: "
        "src/vendor\n"
    )
    assert str(repository) not in result.stderr


def test_clean_wheel_rejects_nonzero_index_stage(tmp_path: Path) -> None:
    repository, _package = _initialize_release_repository(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(repository), "hash-object", "-w", "--stdin"],
        input=b'ORIGIN = "conflict"\n',
        check=True,
        capture_output=True,
    ).stdout.decode("ascii").strip()
    conflict = "src/probe/conflict.py"
    subprocess.run(
        ["git", "-C", str(repository), "update-index", "--index-info"],
        input=(
            f"100644 {blob} 1\t{conflict}\n"
            f"100644 {blob} 2\t{conflict}\n"
        ),
        check=True,
        text=True,
    )

    result = _run_repository_builder(repository, tmp_path / "wheelhouse")

    assert result.returncode == 1
    assert result.stderr == (
        "clean wheel build failed: indexed release input is not stage 0: "
        "src/probe/conflict.py\n"
    )
    assert str(repository) not in result.stderr


def test_concurrent_clean_builds_do_not_follow_or_share_predictable_temp(
    tmp_path: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    victim = tmp_path / "victim.bin"
    original = b"victim-must-remain-unchanged\n"
    victim.write_bytes(original)
    predictable_temp = wheelhouse / ".xuanpian-0.1.0-py3-none-any.whl.tmp"
    try:
        predictable_temp.symlink_to(victim)
    except OSError as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    command = [
        sys.executable,
        str(CLEAN_WHEEL_BUILDER),
        "--outdir",
        str(wheelhouse),
    ]
    environment = {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_CACHE_DIR": str(tmp_path / "pip-cache"),
    }
    processes = [
        subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=120) for process in processes]

    assert [process.returncode for process in processes] == [0, 0], results
    assert victim.read_bytes() == original
    assert predictable_temp.is_symlink()
    assert predictable_temp.resolve() == victim.resolve()
    assert not tuple(wheelhouse.glob(".xuanpian-wheel-publish-*"))
    wheels = tuple(wheelhouse.glob("*.whl"))
    assert len(wheels) == 1
    assert wheels[0].is_file() and not wheels[0].is_symlink()
    with zipfile.ZipFile(wheels[0]) as archive:
        assert archive.testzip() is None


def test_clean_wheel_excludes_removed_analysis_payload_and_contains_only_shared_models(
    tmp_path: Path,
) -> None:
    clean_wheel = _build_clean_wheel(tmp_path)
    banned_members = {
        "src/xuanpian/crop_prediction.py",
        "src/xuanpian/crop_review_cli.py",
        "src/xuanpian/crop_validation.py",
        "src/xuanpian/legacy_crop_prediction.py",
        "src/xuanpian/engine/face_crop_predictor.py",
        "src/xuanpian/engine/face_landmark_backend.py",
        "src/xuanpian/engine/af_exiftool.py",
        "src/xuanpian/engine/framing_analyzer.py",
        "src/xuanpian/ui/crop_review_window.py",
        "src/xuanpian/resources/models/face_landmarker.task",
    }
    with zipfile.ZipFile(clean_wheel) as archive:
        members = set(archive.namelist())

    assert banned_members.isdisjoint(members)
    assert {
        member
        for member in members
        if member.startswith("src/xuanpian/resources/models/")
    } == {
        "src/xuanpian/resources/models/manifest.json",
        "src/xuanpian/resources/models/yunet.onnx",
        "src/xuanpian/resources/models/yolox_nano.onnx",
    }


def test_built_wheel_exposes_only_loadable_product_entry_point(
    tmp_path: Path,
) -> None:
    """Exercise installed wheel metadata, not imports from the checkout."""
    clean_wheel = _build_clean_wheel(tmp_path)

    target = tmp_path / "installed"
    target.mkdir()
    with zipfile.ZipFile(clean_wheel) as archive:
        archive.extractall(target)
    outside = tmp_path / "outside-repository"
    outside.mkdir()
    probe = r"""
import importlib.metadata
import json
from pathlib import Path
import sys

target = Path(sys.argv[1]).resolve()
distribution = next(
    item
    for item in importlib.metadata.distributions(path=[str(target)])
    if item.metadata["Name"] == "xuanpian"
)
entries = {
    item.name: item
    for item in distribution.entry_points
    if item.group == "console_scripts"
}
expected = {"xuanpian": "src.main:main"}
assert {name: item.value for name, item in entries.items()} == expected
for name in expected:
    function = entries[name].load()
    module_path = Path(sys.modules[function.__module__].__file__).resolve()
    assert target == module_path or target in module_path.parents
print(json.dumps(sorted(expected)))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(target)],
        cwd=outside,
        env={
            **os.environ,
            "PYTHONPATH": str(target),
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == ["xuanpian"]


def test_runtime_and_packaging_expose_no_removed_crop_or_af_surfaces() -> None:
    removed_modules = {
        "crop_prediction.py",
        "crop_review_cli.py",
        "crop_validation.py",
        "legacy_crop_prediction.py",
        "face_crop_predictor.py",
        "face_landmark_backend.py",
        "af_exiftool.py",
        "framing_analyzer.py",
        "crop_review_window.py",
    }
    runtime_modules = {
        path.name for path in (REPO_ROOT / "src" / "xuanpian").rglob("*.py")
    }
    assert removed_modules.isdisjoint(runtime_modules)
    assert not (REPO_ROOT / "scripts" / "inspect_af_metadata.py").exists()
    scanned_paths = [
        *(REPO_ROOT / "src").rglob("*.py"),
        *(REPO_ROOT / "scripts").rglob("*.py"),
        REPO_ROOT / "pyproject.toml",
    ]
    banned_runtime_tokens = (
        "FramingAnalyzer",
        "framing_conclusion",
        "crop_review_cli",
        "face_landmarker",
        "xuanpian-crop",
        "src.xuanpian.crop_prediction",
        "src.xuanpian.crop_validation",
        "src.xuanpian.legacy_crop_prediction",
        "src.xuanpian.engine.face_crop_predictor",
        "src.xuanpian.engine.face_landmark_backend",
        "src.xuanpian.ui.crop_review_window",
        "af-review",
        "af_report",
        "AfReview",
        "af_exiftool",
        "inspect_af_metadata",
        "target_face_touches_edge",
        "expected_risk",
        "predicted_risk",
        "AF_REVIEW_FIELDS",
        "_AF_RISK_VALUES",
        "read_af_review",
        "write_af_review",
        "summarize_af_review",
        "_af_rows_from_snapshot",
        "主体可能被裁切",
    )
    legacy_reader = REPO_ROOT / "src" / "xuanpian" / "domain" / "technical_analysis.py"
    for path in scanned_paths:
        text = path.read_text(encoding="utf-8")
        legacy_value_is_narrowly_allowlisted = (
            path == legacy_reader
            and text.count("主体可能被裁切") == 1
            and any(
                "_LEGACY_FRAMING_CONCLUSIONS" in line
                and "主体可能被裁切" in line
                for line in text.splitlines()
            )
        )
        matches = {
            token
            for token in banned_runtime_tokens
            if token in text
            and not (
                token == "主体可能被裁切"
                and legacy_value_is_narrowly_allowlisted
            )
        }
        assert not matches, f"{path}: {sorted(matches)}"
