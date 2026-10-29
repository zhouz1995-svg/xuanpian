# XuanPian（选片）

[中文](#中文) · [English](#english)

<a id="中文"></a>

## 中文

XuanPian（选片）是一款注重隐私的桌面照片筛选助手。它可以帮助摄影师导入照片文件夹、对比连拍照片、查看对焦与曝光提示、标记保留照片并导出选片结果，全程无需将照片上传到云端。

项目当前以**分享源码、供感兴趣的人自行运行和修改**为目标，不以上架商店、销售或提供安装包为前提。分析结果仅用于辅助人工复查，不会自动判定一张照片是否可用。

开发分支的 `technical-v8` 已采用 ONNX Runtime CPU、YuNet 和 YOLOX-Nano，模型随源码内置，完全离线运行。跨平台目标为 macOS 12+（Intel / Apple Silicon）及 Windows 10 22H2 / Windows 11 x64；各目标的正式交付与干净桌面验收仍需分别完成，当前 Release 的支持范围仍以下方安装说明为准。迁移说明见 [ONNX 分析记录](docs/onnx-analysis.md)。

建议从下方[源码运行步骤](#从源码运行)开始。打包是可选的进阶操作；已有候选构建和系统验收边界保留在 [当前交付状态](docs/release-status.md)，不应理解为所有目标系统均已通过真机验证。

安全提示：开发分支已升级到 Pillow 13.0.0 安全版本；现有 Release 尚未包含此更新，请勿使用旧包处理不可信来源的图片。新候选包仍需完成发布验收。

### 主要功能

- 在本地导入和预览 JPEG 及受支持的 RAW 格式。
- 支持快捷键选片、保留/待定/舍弃标记、筛选与连拍分组。
- 提供主体优先的清晰度、曝光和人像复查提示。
- 使用本地项目保存复查状态，关闭后可继续处理。
- 标记保存在本地项目中，标记不会移动原始文件；只有明确选择“移动文件”导出时才会改变文件位置。
- 导出前会显示照片数量与文件名示例；移动文件或覆盖同名文件需要再次确认。
- 分析模型随源码提供，运行无需账号或下载模型；源码模式的 AF 元数据支持另见下文。

### 分析范围与限制

清晰度分析会优先检查检测到的主体。对于包含受支持元数据的 JPEG，它还可以结合自动对焦区域进行判断。目前明确支持的映射为 `Make=NIKON CORPORATION`、`Model=NIKON Z50_2`、`AFInfo2Version=0402`。其他相机、RAW 文件、缺失或不受支持的元数据、无效坐标以及元数据运行环境故障，都会使用保守的图像回退方案，不能据此断定自动对焦点位置。

大光圈浅景深、运动、遮挡或主体过小时，任何算法都无法保证眼部清晰度判断完全准确。“建议复查”表示应在合适倍率下检查主体，并不等同于淘汰照片。请为不可替代的照片保留备份，并在移动或导出前核对目标位置。详情参阅[隐私与数据处理说明](PRIVACY.md)。

当前版本提供三种项目内标记，但尚未实现五星评分或 XMP 写回；这些标记不会写入照片文件。

### 从源码运行

项目采用 [GPL-3.0-only](LICENSE) 许可证。下载或克隆**本 README 所在分支**的完整源码，在仓库根目录操作；推荐原生架构的 64 位 Python 3.11，不要把某个平台的 `.venv` 复制到另一个平台。安装依赖需要联网，正常分析不上传照片。

macOS（Apple Silicon）：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --only-binary :all: --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
.venv/bin/python -m src.main
```

macOS（Intel）：上述步骤的依赖锁改为 `packaging/requirements-macos-x86_64.lock`，其余命令相同。请使用原生 Intel Python，不通过 Rosetta 混装架构。

Windows x64（PowerShell，不要求激活虚拟环境）：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary :all: --require-hashes -r packaging/requirements-windows-x64.lock
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m src.main
```

三份现有锁包含运行、测试和可选打包工具，安装包较大；不必执行打包脚本即可运行应用。Windows 10/11 真机试用仍未完成。安装失败时先检查 Python 版本、架构及所选锁，勿随意删除哈希校验或升级单个依赖。

源码模式下，安装当前受支持的 ExifTool 并保证终端执行 `exiftool -ver` 成功，才可读取受支持 JPEG 的 AF 元数据。没有该工具或读取失败时会使用图像 fallback，不阻止基本选片。它不是通过 pip 安装的 Python 依赖，打包版的内置运行时与源码环境不同。

运行测试与贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。独立应用、DMG 和 Windows 安装器仅为可选进阶路径，见 [macOS 构建说明](docs/building-macos.md) / [Windows 构建说明](docs/building-windows.md)；这些打包配方可能要求特定编译工具链，不是从源码启动界面的前置条件。

### 可选：历史安装包

[GitHub Releases](https://github.com/zhouz1995-svg/xuanpian/releases) 中的历史 `.dmg` 和 SHA-256 文件仅对应当时的 Apple 芯片（`arm64`）测试版，不包含当前源码的全部修正，也不是本项目当前推荐的入门路径。

历史测试版使用本地临时签名，**尚未经过 Apple 公证**，macOS 可能显示安全警告。请勿关闭 Gatekeeper 或其他系统级安全保护。历史步骤见[安装指南](docs/installation.md)。

### 发布说明

XuanPian 0.1.0 是早期公开测试版，仅供 Apple 芯片 Mac 评估使用，尚未完成 Apple Developer ID 签名和公证。第三方库、模型与资源保留各自的许可证；当前的组件来源和许可核验记录见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

<a id="english"></a>

## English

XuanPian is a privacy-friendly desktop photo-review assistant. It helps photographers import a folder, compare likely bursts, review focus and exposure cues, mark keepers, and export a selection without uploading photos to a cloud service.

The current goal is to share source for interested users to run and modify themselves, not to require store publication, sales or prebuilt installers. Analysis results are review aids, not an automatic decision about whether a photo is usable.

The development branch uses ONNX Runtime CPU with bundled YuNet and YOLOX-Nano models (`technical-v8`). The target platforms are macOS 12+ (Intel / Apple Silicon) and Windows 10 22H2 / Windows 11 x64. Formal delivery and clean-desktop acceptance remain separate gates for each target; the existing Release keeps its documented support limits. See [the migration notes](docs/onnx-analysis.md).

Start with [running from source](#build-from-source). Packaging is optional. Historical candidates and platform acceptance limits remain in [current delivery status](docs/release-status.md); they do not prove clean-machine acceptance on every target OS.

Security: the development branch uses the Pillow 13.0.0 security release. Existing Releases do not include this update; do not use older packages for untrusted images. New candidates still require release acceptance.

### Highlights

- Local JPEG and supported RAW import and preview.
- Fast keyboard-driven culling, keep/maybe/discard marks, filters, and burst grouping.
- Subject-aware focus, exposure, and portrait-review cues.
- Persistent local project state, so a review can be closed and reopened.
- Marks are local project metadata. Marks do not move original files; file locations change only through an explicit move export.
- Export shows the photo count and a filename example; move and overwrite operations require a second confirmation.
- Models included with source, with no account or first-run model download. Source-mode AF metadata support is described below.

### Analysis scope and limitations

Focus analysis prioritizes detected subjects. For supported JPEG metadata, it can also evaluate an AF-aware region. The exact AF metadata mapping currently supported is `Make=NIKON CORPORATION`, `Model=NIKON Z50_2`, `AFInfo2Version=0402`. Other cameras, RAW files, unsupported or missing metadata, invalid coordinates, and metadata-runtime failures use a conservative image-based fallback; they are not evidence of an AF point. There is no universal eye-sharpness guarantee, especially with shallow depth of field, motion, occlusion, or very small subjects.

Treat “review suggested” as a prompt to inspect the subject at useful magnification, not as a rejection. Keep backups of irreplaceable photos and check the destination before any explicit move or export action. More detail is available in [privacy and data handling](PRIVACY.md).

The current release provides three project-local marks, but does not yet implement five-star ratings or XMP write-back. Marks are not written into photo files.

### Build from source

Licensed under [GPL-3.0-only](LICENSE). Download or clone the complete source from the branch containing this README and work from its root. Use native 64-bit Python 3.11; never copy virtual environments between platforms. Dependency installation requires network access; photo analysis does not upload photos. On Apple Silicon macOS:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --only-binary :all: --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
.venv/bin/python -m src.main
```

On Intel macOS, use `packaging/requirements-macos-x86_64.lock` instead; the other commands are unchanged. Use native Intel Python without Rosetta. On Windows x64, use PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install --only-binary :all: --require-hashes -r packaging/requirements-windows-x64.lock
.venv\Scripts\python.exe -m pip check
.venv\Scripts\python.exe -m src.main
```

The existing locks include runtime, test and optional build tools. No packaging script is required to launch the app. Windows 10/11 hands-on acceptance is still incomplete. If installation fails, check Python, architecture and lock selection rather than disabling hashes or upgrading individual dependencies.

Source mode looks for `exiftool` on PATH. Install a supported ExifTool separately and verify `exiftool -ver` to enable supported JPEG AF metadata; missing/failed metadata uses the image-based fallback and does not prevent basic review. The packaged metadata runtime is not automatically present in a source checkout.

For tests and contributions, see [CONTRIBUTING.md](CONTRIBUTING.md). Standalone builds are optional: [macOS](docs/building-macos.md) / [Windows](docs/building-windows.md). Their fixed compiler/toolchain requirements are not prerequisites for launching the source app. Do not include private photos or metadata in reports.

### Optional: historical installers

[GitHub Releases](https://github.com/zhouz1995-svg/xuanpian/releases) contains historical Apple Silicon (`arm64`) `.dmg` builds and checksums. They do not contain every current source fix and are not the recommended starting point.

These builds are locally ad-hoc signed and **not Apple-notarized**. Do not disable Gatekeeper or other system-wide protections. Historical steps are in the [installation guide](docs/installation.md).

### Release notes

XuanPian 0.1.0 is an early public beta. The current build is intended for evaluation on Apple Silicon and has not completed Apple Developer ID signing or notarization. Third-party libraries, models, and assets retain their own licenses; the bundled notice set and the project’s current provenance review are recorded in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
