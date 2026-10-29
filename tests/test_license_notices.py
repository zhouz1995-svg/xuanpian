"""Notice integrity is a packaging gate, not a redistribution grant."""

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts import build_wheel


def _fixture(root):
    files = {"LICENSE": b"Project terms\n", "THIRD_PARTY_NOTICES.md": b"Blocked\n",
             "packaging/third-party-components.json": b'{"release_status":"blocked"}\n',
             "packaging/licenses/example.txt": b"Upstream terms\n"}
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"schema_version": 1, "release_status": "blocked", "files": {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (root / "packaging/notice-manifest.json").write_text(json.dumps(manifest))
    return files


def test_valid_notice_payload_includes_manifest_and_exact_texts(tmp_path):
    files = _fixture(tmp_path)
    assert set(build_wheel.notice_payload(tmp_path)) == set(files) | {"packaging/notice-manifest.json"}


@pytest.mark.parametrize("mutation", ["missing", "changed", "symlink", "parent_symlink", "empty", "traversal", "omitted_license", "omitted_upstream"])
def test_invalid_notice_payload_is_rejected(tmp_path, mutation):
    _fixture(tmp_path)
    target = tmp_path / "packaging/licenses/example.txt"
    if mutation == "missing":
        target.unlink()
    elif mutation == "changed":
        target.write_text("changed")
    elif mutation == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "LICENSE")
    elif mutation == "parent_symlink":
        (tmp_path / "packaging/licenses").rename(tmp_path / "elsewhere")
        (tmp_path / "packaging/licenses").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    else:
        path = tmp_path / "packaging/notice-manifest.json"
        manifest = json.loads(path.read_text())
        if mutation == "empty":
            manifest["files"] = {}
        elif mutation == "traversal":
            manifest["files"]["../outside"] = "0" * 64
        elif mutation == "omitted_license":
            del manifest["files"]["LICENSE"]
        else:
            del manifest["files"]["packaging/licenses/example.txt"]
        path.write_text(json.dumps(manifest))
    with pytest.raises(build_wheel.CleanWheelBuildError):
        build_wheel.notice_payload(tmp_path)


def test_wheel_notice_verification_checks_bytes_not_just_presence(tmp_path):
    _fixture(tmp_path)
    wheel = tmp_path / "probe.whl"
    payload = build_wheel.notice_payload(tmp_path)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr("xuanpian-0.1.0.dist-info/licenses/" + name, data)
    build_wheel.verify_wheel_notices(wheel, payload)
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in payload.items():
            archive.writestr("xuanpian-0.1.0.dist-info/licenses/" + name,
                             b"altered" if name == "LICENSE" else data)
    with pytest.raises(build_wheel.CleanWheelBuildError):
        build_wheel.verify_wheel_notices(wheel, payload)


def test_bundle_notice_check_rejects_missing_or_changed_copy(tmp_path):
    from scripts import verify_macos_bundle as gate
    root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "Example.app/Contents/Resources/legal"
    app = destination.parents[2]
    assert gate._inspect_notices(app)
    for name, data in build_wheel.notice_payload(root).items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert gate._inspect_notices(app) == []
    (destination / "LICENSE").write_text("altered")
    assert gate._inspect_notices(app)


def test_clean_wheel_contains_reviewed_notice_snapshot(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wheel = build_wheel.build_clean_wheel(tmp_path, repo_root=root)
    with zipfile.ZipFile(wheel) as archive:
        prefix = "xuanpian-0.1.0.dist-info/licenses/"
        assert archive.read(prefix + "LICENSE") == (root / "LICENSE").read_bytes()
        assert archive.read(prefix + "THIRD_PARTY_NOTICES.md") == (root / "THIRD_PARTY_NOTICES.md").read_bytes()
        for name, data in build_wheel.notice_payload(root).items():
            assert archive.read(prefix + name) == data


def test_notice_change_must_be_staged_for_clean_wheel(tmp_path):
    _fixture(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="xuanpian"\n')
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / "LICENSE").write_text("unstaged change")
    with pytest.raises(build_wheel.CleanWheelBuildError, match="unstaged release input"):
        build_wheel.build_clean_wheel(tmp_path / "dist", repo_root=tmp_path)


def test_intel_receipt_reuses_exact_hashed_legal_texts_not_a_new_payload_type():
    root = Path(__file__).resolve().parents[1]
    receipt_path = root / "packaging/macos-x86_64-wheel-notices.json"
    assert receipt_path.is_file(), "Intel wheel notice receipt required"
    receipt = json.loads(receipt_path.read_text())
    manifest = json.loads((root / "packaging/notice-manifest.json").read_text())
    payload = build_wheel.notice_payload(root)
    assert receipt["rights_status"] == "unreviewed"
    assert manifest["release_status"] == "blocked"
    assert len(receipt["wheel_files"]) == 28
    assert "packaging/macos-x86_64-wheel-notices.json" not in payload
    for wheel, entry in receipt["wheel_files"].items():
        for notice in entry["notices"]:
            snapshot = notice["snapshot"]
            actual = hashlib.sha256((root / snapshot).read_bytes()).hexdigest()
            assert actual == notice["member_sha256"] == notice["snapshot_sha256"]
            assert manifest["files"][snapshot] == actual
            assert payload[snapshot] == (root / snapshot).read_bytes()
            assert any(origin["wheel"] == wheel
                       and origin["wheel_sha256"] == entry["sha256"]
                       and origin["member"] == notice["member"]
                       and origin["sha256"] == actual
                       and origin["path"] == snapshot
                       for origin in manifest["wheel_origins"])


def test_intel_lock_keeps_arm64_package_versions():
    import re
    root = Path(__file__).resolve().parents[1]
    pins = lambda name: re.findall(r"^([a-z0-9-]+)==([^\s]+)",
                                   (root / "packaging" / name).read_text(), re.MULTILINE)
    assert pins("requirements-macos-x86_64.lock") == pins("requirements-macos-arm64.lock")
