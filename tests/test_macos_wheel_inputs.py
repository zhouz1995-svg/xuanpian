"""Verify approved wheel bytes before any foreign dependency is installed."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


def _fixture(tmp_path):
    root = tmp_path / "source"
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    member = "demo-1.dist-info/licenses/LICENSE"
    snapshot = "packaging/licenses/macos-x86_64/demo.txt"
    text = b"Vendor terms\r\n"
    path = root / snapshot
    path.parent.mkdir(parents=True)
    path.write_bytes(text)
    wheel = wheels / "demo-1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, text)
    digest = hashlib.sha256(text).hexdigest()
    data = {"schema_version": 1,
            "target": {"platform": "macos", "architecture": "x86_64",
                       "minimum_macos": "12.0", "python": "3.11"},
            "rights_status": "unreviewed",
            "wheel_files": {wheel.name: {
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "notices": [{"member": member, "member_sha256": digest,
                             "snapshot": snapshot, "snapshot_sha256": digest}]}}}
    receipt = root / "packaging/macos-x86_64-wheel-notices.json"
    receipt.write_text(json.dumps(data))
    return wheels, receipt, data


def test_wheel_inputs_accept_exact_archive_and_raw_notice_bytes(tmp_path):
    from scripts.verify_macos_wheel_inputs import verify_wheel_inputs
    wheels, receipt, _ = _fixture(tmp_path)
    assert verify_wheel_inputs(wheels, receipt) is None


@pytest.mark.parametrize("mutation", [
    "missing_wheel", "extra_wheel", "archive_digest", "member_digest",
    "snapshot_digest", "snapshot_changed", "member_missing", "member_traversal",
    "snapshot_traversal", "wheel_traversal", "wheel_symlink", "directory_symlink",
    "receipt_symlink", "snapshot_symlink", "snapshot_parent_symlink",
    "member_symlink", "duplicate_member", "numeric_target", "wrong_arch",
    "empty_wheels", "digest_malformed", "unlisted_notice",
])
def test_wheel_inputs_reject_tampered_or_unsafe_inputs(tmp_path, mutation):
    from scripts.verify_macos_wheel_inputs import verify_wheel_inputs
    wheels, receipt, data = _fixture(tmp_path)
    filename, entry = next(iter(data["wheel_files"].items()))
    wheel = wheels / filename
    notice = entry["notices"][0]
    snapshot = receipt.parents[1] / notice["snapshot"]
    if mutation == "missing_wheel":
        wheel.unlink()
    elif mutation == "extra_wheel":
        (wheels / "extra.whl").write_bytes(b"extra")
    elif mutation == "archive_digest":
        entry["sha256"] = "0" * 64
    elif mutation == "member_digest":
        notice["member_sha256"] = "0" * 64
    elif mutation == "snapshot_digest":
        notice["snapshot_sha256"] = "0" * 64
    elif mutation == "snapshot_changed":
        snapshot.write_bytes(b"Vendor terms\n")
    elif mutation == "member_missing":
        notice["member"] = "absent.txt"
    elif mutation == "member_traversal":
        notice["member"] = "../LICENSE"
    elif mutation == "snapshot_traversal":
        notice["snapshot"] = "../LICENSE"
    elif mutation == "wheel_traversal":
        data["wheel_files"] = {"../" + filename: entry}
    elif mutation == "wheel_symlink":
        other = tmp_path / "real.whl"
        wheel.rename(other)
        wheel.symlink_to(other)
    elif mutation == "directory_symlink":
        other = tmp_path / "real-wheels"
        wheels.rename(other)
        wheels.symlink_to(other, target_is_directory=True)
    elif mutation == "receipt_symlink":
        other = receipt.with_suffix(".real")
        receipt.rename(other)
        receipt.symlink_to(other)
    elif mutation == "snapshot_symlink":
        other = snapshot.with_suffix(".real")
        snapshot.rename(other)
        snapshot.symlink_to(other)
    elif mutation == "snapshot_parent_symlink":
        parent = snapshot.parent
        other = tmp_path / "real-notices"
        parent.rename(other)
        parent.symlink_to(other, target_is_directory=True)
    elif mutation in {"member_symlink", "duplicate_member", "unlisted_notice"}:
        with zipfile.ZipFile(wheel, "a") as archive:
            if mutation == "member_symlink":
                info = zipfile.ZipInfo("linked.txt")
                info.create_system = 3
                info.external_attr = 0o120777 << 16
                archive.writestr(info, "elsewhere")
            elif mutation == "duplicate_member":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(notice["member"], b"ambiguous")
            else:
                archive.writestr("demo-1.dist-info/licenses/NOTICE", b"unlisted")
        entry["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    elif mutation == "numeric_target":
        data["target"]["minimum_macos"] = 12.0
    elif mutation == "wrong_arch":
        data["target"]["architecture"] = "arm64"
    elif mutation == "empty_wheels":
        data["wheel_files"] = {}
    elif mutation == "digest_malformed":
        entry["sha256"] = "not-a-sha"
    if mutation != "receipt_symlink":
        receipt.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        verify_wheel_inputs(wheels, receipt)


def test_wheel_verifier_cli_needs_only_the_standard_library(tmp_path):
    wheels, receipt, _ = _fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts/verify_macos_wheel_inputs.py"
    result = subprocess.run([sys.executable, "-I", "-S", str(script),
                             "--wheels", str(wheels), "--receipt", str(receipt)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
