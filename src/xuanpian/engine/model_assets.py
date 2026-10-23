"""Runtime verification for packaged offline vision model assets."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path


MODEL_KEYS = frozenset({"face_detector", "object_detector"})


class ModelAssetError(RuntimeError):
    """Raised when a required packaged model is absent or modified."""


def verify_model(path: Path, expected_sha256: str) -> Path:
    """Return *path* only when it exists and matches its recorded digest."""
    if not path.is_file():
        raise ModelAssetError(f"模型资源缺失: {path.name}")
    if len(expected_sha256) != 64:
        raise ModelAssetError(f"模型资源摘要无效: {path.name}")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise ModelAssetError(f"模型资源 SHA-256 校验失败: {path.name}")
    return path


def verified_model_path(name: str) -> Path:
    """Locate and integrity-check an offline model declared in the manifest."""
    if name not in MODEL_KEYS:
        raise ModelAssetError(f"未知模型资源: {name}")

    resource_dir = files("src.xuanpian.resources").joinpath("models")
    manifest_path = Path(str(resource_dir.joinpath("manifest.json")))
    if not manifest_path.is_file():
        raise ModelAssetError("模型资源清单缺失")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest["models"][name]
        filename = entry["filename"]
        sha256 = entry["sha256"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ModelAssetError(f"模型资源清单无效: {name}") from exc

    return verify_model(Path(str(resource_dir.joinpath(filename))), sha256)
