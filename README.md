# XuanPian（选片）

[中文](#中文) · [English](#english)

<a id="中文"></a>

## 中文

XuanPian（选片）是一款注重隐私的桌面照片筛选助手。它可以帮助摄影师导入照片文件夹、对比连拍照片、查看对焦与曝光提示、标记保留照片并导出选片结果，全程无需将照片上传到云端。

0.1.0 是面向 Apple 芯片 Mac 的测试版本。分析结果仅用于辅助人工复查，不会自动判定一张照片是否可用。

开发分支的 `technical-v8` 已采用 ONNX Runtime CPU、YuNet 和 YOLOX-Nano，模型随源码内置，完全离线运行。跨平台目标为 macOS 12+（Intel / Apple Silicon）及 Windows 10 22H2 / Windows 11 x64；各目标的正式交付与干净桌面验收仍需分别完成，当前 Release 的支持范围仍以下方安装说明为准。迁移说明见 [ONNX 分析记录](docs/onnx-analysis.md)。

Windows x64 已有通过原生 CI 安装、运行和卸载验证的未签名候选，尚非正式 Release。详见 [Windows 构建说明](docs/building-windows.md) 与 [验证记录](docs/verification/windows-installer.md)。

各平台进度、暂缓事项和下一步见 [当前交付状态](docs/release-status.md)。

安全提示：开发分支已升级到 Pillow 13.0.0 安全版本；现有 Release 尚未包含此更新，请勿使用旧包处理不可信来源的图片。新候选包仍需完成发布验收。

### 主要功能

- 在本地导入和预览 JPEG 及受支持的 RAW 格式。
- 支持快捷键选片、保留/待定/舍弃标记、筛选与连拍分组。
- 提供主体优先的清晰度、曝光和人像复查提示。
- 使用本地项目保存复查状态，关闭后可继续处理。
- 标记保存在本地项目中，标记不会移动原始文件；只有明确选择“移动文件”导出时才会改变文件位置。
- 导出前会显示照片数量与文件名示例；移动文件或覆盖同名文件需要再次确认。
- 内置分析模型和元数据运行环境，无需账号或首次启动下载。

### 下载与安装

请从 [GitHub Releases](https://github.com/zhouz1995-svg/xuanpian/releases) 下载当前 `.dmg` 和对应的 SHA-256 校验文件。安装包仅支持 Apple 芯片（`arm64`），不支持 Intel Mac 和 Windows。

当前测试版使用本地临时签名，**尚未经过 Apple 公证**，macOS 可能显示安全警告。请勿关闭 Gatekeeper 或其他系统级安全保护。校验文件和拖入“应用程序”文件夹的具体步骤请参阅[安装指南](docs/installation.md)。

### 分析范围与限制

清晰度分析会优先检查检测到的主体。对于包含受支持元数据的 JPEG，它还可以结合自动对焦区域进行判断。目前明确支持的映射为 `Make=NIKON CORPORATION`、`Model=NIKON Z50_2`、`AFInfo2Version=0402`。其他相机、RAW 文件、缺失或不受支持的元数据、无效坐标以及元数据运行环境故障，都会使用保守的图像回退方案，不能据此断定自动对焦点位置。

大光圈浅景深、运动、遮挡或主体过小时，任何算法都无法保证眼部清晰度判断完全准确。“建议复查”表示应在合适倍率下检查主体，并不等同于淘汰照片。请为不可替代的照片保留备份，并在移动或导出前核对目标位置。详情参阅[隐私与数据处理说明](PRIVACY.md)。

当前版本提供三种项目内标记，但尚未实现五星评分或 XMP 写回；这些标记不会写入照片文件。

### 从源码运行

项目需要 Python 3.11，并采用 [GPL-3.0-only](LICENSE) 许可证。在 arm64 Mac 上安装锁定并校验哈希的依赖，然后运行测试：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

启动开发版本：

```bash
.venv/bin/python -m src.main
```

独立应用和 DMG 的构建流程请参阅 [macOS 构建说明](docs/building-macos.md)。参与贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；提交问题时请勿附带私人照片或元数据。

### 发布说明

XuanPian 0.1.0 是早期公开测试版，仅供 Apple 芯片 Mac 评估使用，尚未完成 Apple Developer ID 签名和公证。第三方库、模型与资源保留各自的许可证；当前的组件来源和许可核验记录见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

<a id="english"></a>

## English

XuanPian is a privacy-friendly desktop photo-review assistant. It helps photographers import a folder, compare likely bursts, review focus and exposure cues, mark keepers, and export a selection without uploading photos to a cloud service.

Version 0.1.0 is a beta for Apple Silicon Macs. Analysis results are review aids, not an automatic decision about whether a photo is usable.

The development branch uses ONNX Runtime CPU with bundled YuNet and YOLOX-Nano models (`technical-v8`). The target platforms are macOS 12+ (Intel / Apple Silicon) and Windows 10 22H2 / Windows 11 x64. Formal delivery and clean-desktop acceptance remain separate gates for each target; the existing Release keeps its documented support limits. See [the migration notes](docs/onnx-analysis.md).

An unsigned Windows x64 candidate has passed native CI installation, runtime and uninstall checks. It is not a Release. See [Windows build instructions](docs/building-windows.md) and [verification evidence](docs/verification/windows-installer.md).

See [current delivery status](docs/release-status.md) for platform progress, deferred acceptance and next steps.

Security: the development branch uses the Pillow 13.0.0 security release. Existing Releases do not include this update; do not use older packages for untrusted images. New candidates still require release acceptance.

### Highlights

- Local JPEG and supported RAW import and preview.
- Fast keyboard-driven culling, keep/maybe/discard marks, filters, and burst grouping.
- Subject-aware focus, exposure, and portrait-review cues.
- Persistent local project state, so a review can be closed and reopened.
- Marks are local project metadata. Marks do not move original files; file locations change only through an explicit move export.
- Export shows the photo count and a filename example; move and overwrite operations require a second confirmation.
- Bundled models and metadata runtime, with no account or first-run model download.

### Download and install

Download the current `.dmg` and its SHA-256 checksum from [GitHub Releases](https://github.com/zhouz1995-svg/xuanpian/releases). The packaged beta supports Apple Silicon (`arm64`) only; Intel Macs and Windows are not supported.

This beta is locally ad-hoc signed and is **not Apple-notarized**. macOS may display a security warning. Do not disable Gatekeeper or other system-wide security protections. See the [installation guide](docs/installation.md) for the checksum and drag-to-Applications steps.

### Analysis scope and limitations

Focus analysis prioritizes detected subjects. For supported JPEG metadata, it can also evaluate an AF-aware region. The exact AF metadata mapping currently supported is `Make=NIKON CORPORATION`, `Model=NIKON Z50_2`, `AFInfo2Version=0402`. Other cameras, RAW files, unsupported or missing metadata, invalid coordinates, and metadata-runtime failures use a conservative image-based fallback; they are not evidence of an AF point. There is no universal eye-sharpness guarantee, especially with shallow depth of field, motion, occlusion, or very small subjects.

Treat “review suggested” as a prompt to inspect the subject at useful magnification, not as a rejection. Keep backups of irreplaceable photos and check the destination before any explicit move or export action. More detail is available in [privacy and data handling](PRIVACY.md).

The current release provides three project-local marks, but does not yet implement five-star ratings or XMP write-back. Marks are not written into photo files.

### Build from source

The project requires Python 3.11 and is licensed under [GPL-3.0-only](LICENSE). On an arm64 Mac, install the hash-pinned dependencies and run the test suite:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Run the development app with:

```bash
.venv/bin/python -m src.main
```

For the standalone application and DMG workflow, see [building for macOS](docs/building-macos.md). Contributions should follow [CONTRIBUTING.md](CONTRIBUTING.md); bug reports must not include private photos or metadata.

### Release notes

XuanPian 0.1.0 is an early public beta. The current build is intended for evaluation on Apple Silicon and has not completed Apple Developer ID signing or notarization. Third-party libraries, models, and assets retain their own licenses; the bundled notice set and the project’s current provenance review are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
