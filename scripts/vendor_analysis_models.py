#!/usr/bin/env python3
"""Restore the exact two pinned ONNX assets; never rewrite their digests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "src/xuanpian/resources/models"


def vendor_models(model_directory: Path = MODEL_DIRECTORY) -> dict[str, object]:
    manifest = json.loads(
        (model_directory / "manifest.json").read_text(encoding="utf-8")
    )
    for entry in manifest["models"].values():
        filename = entry["filename"]
        if Path(filename).name != filename or not filename.endswith(".onnx"):
            raise ValueError("Invalid model filename")
        destination = model_directory / filename
        if (
            destination.is_file()
            and hashlib.sha256(destination.read_bytes()).hexdigest() == entry["sha256"]
        ):
            continue
        temporary_path = None
        try:
            with NamedTemporaryFile(dir=model_directory, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                with urlopen(entry["url"], timeout=60) as response:
                    while chunk := response.read(1024 * 1024):
                        temporary.write(chunk)
            data = temporary_path.read_bytes()
            if (
                len(data) != entry["size_bytes"]
                or hashlib.sha256(data).hexdigest() != entry["sha256"]
            ):
                raise ValueError(
                    "Downloaded model does not match pinned digest: " + filename
                )
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    return manifest


if __name__ == "__main__":
    vendor_models()
