"""Stage a new macOS DMG from a verified application bundle."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_macos_bundle import inspect_bundle


def _publish_pair(candidate: Path, staged_sidecar: Path, output: Path, sidecar: Path) -> None:
    """Publish without replacement, serializing cooperating builders in the parent."""
    if output.parent != sidecar.parent:
        raise ValueError("DMG and sidecar must share a parent directory")
    directory_fd = os.open(output.parent, os.O_RDONLY)
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        # Hard links are atomic and fail with EEXIST even for dangling symlinks.
        # Both staged files live on this same filesystem in TemporaryDirectory.
        os.link(candidate, output)
        source_stat = candidate.stat()
        try:
            os.link(staged_sidecar, sidecar)
        except OSError:
            try:
                current = output.stat(follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == (
                source_stat.st_dev, source_stat.st_ino
            ):
                output.unlink()
            raise
    finally:
        os.close(directory_fd)


def build_dmg(app: Path, output: Path) -> Path:
    """Create a new unsigned-for-distribution DMG and SHA-256 sidecar."""
    app, output = Path(app), Path(output)
    if app.is_symlink() or not app.is_dir() or app.suffix != ".app":
        raise ValueError(f"app must be an existing regular .app: {app}")
    sidecar = output.with_suffix(output.suffix + ".sha256")
    if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        raise FileExistsError(f"DMG output or sidecar already exists: {output}")
    violations = inspect_bundle(app)
    if violations:
        raise ValueError(f"app failed bundle inspection: {len(violations)} violation(s): {violations[:3]}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".xuanpian-dmg-", dir=output.parent) as temporary:
        root = Path(temporary)
        stage = root / "stage"
        stage.mkdir()
        subprocess.run(["/usr/bin/ditto", "--rsrc", "--extattr", "--acl",
                        str(app), str(stage / app.name)], check=True)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        candidate = root / output.name
        subprocess.run(["/usr/bin/hdiutil", "create", "-volname", "选片APP",
                        "-srcfolder", str(stage), "-format", "UDZO", str(candidate)], check=True)
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
            raise FileExistsError(f"DMG output appeared during build: {output}")
        staged_sidecar = root / sidecar.name
        staged_sidecar.write_text(f"{digest.hexdigest()}  {output.name}\n", encoding="utf-8")
        _publish_pair(candidate, staged_sidecar, output, sidecar)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build_dmg(args.app, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
