"""Validate the exact approved Intel wheel/notice closure before installation.

Standard library only: this gate runs before installing any selected wheels.
The receipt preserves evidence, not a redistribution or security clearance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


TARGET = {"platform": "macos", "architecture": "x86_64",
          "minimum_macos": "12.0", "python": "3.11"}


def _relative(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "\x00" in value or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError(f"unsafe input path: {value!r}")
    return PurePosixPath(value)


def _regular(path: Path, root: Path, *, directory: bool = False) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f"symlink input: {component}")
        if component == root:
            break
    else:
        raise ValueError(f"input outside trusted root: {path}")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"missing regular input: {path}")


def _digest(data: bytes, expected: str, label: str) -> None:
    if not isinstance(expected, str) or not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError(f"invalid SHA-256: {label}")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"SHA-256 mismatch: {label}")


def is_notice_member(name: str) -> bool:
    """Include vendored notices and every file in dist-info license folders."""
    path = PurePosixPath(name)
    return (any(part.endswith(".dist-info") and index + 1 < len(path.parts)
                and path.parts[index + 1].lower() == "licenses"
                for index, part in enumerate(path.parts))
            or bool(re.fullmatch(r"(licen[sc]es?|copying|authors|copyright|(?:thirdparty)?notices?)([._-].*)?",
                                 path.name, re.IGNORECASE)))


def verify_wheel_inputs(wheel_directory: Path, receipt: Path) -> None:
    wheel_directory, receipt = Path(wheel_directory).absolute(), Path(receipt).absolute()
    source = receipt.parent.parent
    _regular(receipt, source)
    _regular(wheel_directory, wheel_directory, directory=True)
    document = json.loads(receipt.read_bytes())
    if (set(document) != {"schema_version", "target", "rights_status", "wheel_files"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or document["target"] != TARGET or document["rights_status"] != "unreviewed"):
        raise ValueError("unsupported Intel wheel receipt metadata")
    wheels = document["wheel_files"]
    if not isinstance(wheels, dict) or not wheels:
        raise ValueError("empty wheel receipt")
    if {path.name for path in wheel_directory.iterdir()} != set(wheels):
        raise ValueError("wheel set differs from receipt")
    for filename, entry in wheels.items():
        if len(_relative(filename).parts) != 1 or not filename.endswith(".whl"):
            raise ValueError(f"unsafe wheel filename: {filename}")
        wheel = wheel_directory / filename
        _regular(wheel, wheel_directory)
        if not isinstance(entry, dict) or set(entry) != {"sha256", "notices"}:
            raise ValueError(f"invalid wheel receipt entry: {filename}")
        _digest(wheel.read_bytes(), entry["sha256"], filename)
        notices = entry["notices"]
        if not isinstance(notices, list):
            raise ValueError(f"invalid notices: {filename}")
        try:
            with zipfile.ZipFile(wheel) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if len(names) != len(set(names)):
                    raise ValueError(f"duplicate wheel members: {filename}")
                for info in infos:
                    _relative(info.filename.rstrip("/") if info.is_dir() else info.filename)
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise ValueError(f"symlink wheel member: {info.filename}")
                expected_members = {info.filename for info in infos
                                    if not info.is_dir() and is_notice_member(info.filename)}
                listed_members = []
                for notice in notices:
                    if not isinstance(notice, dict) or set(notice) != {
                            "member", "member_sha256", "snapshot", "snapshot_sha256"}:
                        raise ValueError(f"invalid notice receipt: {filename}")
                    member = str(_relative(notice["member"]))
                    snapshot = source / str(_relative(notice["snapshot"]))
                    _regular(snapshot, source)
                    if member not in names:
                        raise ValueError(f"missing notice member: {member}")
                    member_bytes, snapshot_bytes = archive.read(member), snapshot.read_bytes()
                    _digest(member_bytes, notice["member_sha256"], member)
                    _digest(snapshot_bytes, notice["snapshot_sha256"], str(snapshot))
                    if member_bytes != snapshot_bytes:
                        raise ValueError(f"notice snapshot differs from archive: {member}")
                    listed_members.append(member)
                if (len(listed_members) != len(set(listed_members))
                        or set(listed_members) != expected_members):
                    raise ValueError(f"notice member set differs from receipt: {filename}")
        except zipfile.BadZipFile as error:
            raise ValueError(f"invalid wheel archive: {filename}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    verify_wheel_inputs(args.wheels, args.receipt)
    print("Intel wheel archive and raw notice bytes: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
