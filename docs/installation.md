# 在 macOS 上安装 XuanPian / Install XuanPian on macOS

[中文](#中文) · [English](#english)

<a id="中文"></a>

## 中文

XuanPian 0.1.0 是面向 Apple 芯片（`arm64`）Mac 的测试版本。该安装包不支持 Intel Mac 和 Windows。应用已经内置 Python、Qt、Perl/ExifTool 和分析模型，正常安装不需要 Homebrew，也不需要首次启动下载。

### 打开下载文件之前

请从同一个 GitHub Release 下载 `.dmg` 和 SHA-256 校验文件。在“终端”中先验证下载文件：

```bash
shasum -a 256 XuanPian-0.1.0-arm64-local-ad-hoc.dmg
```

输出结果必须与 Release 页面及 `.sha256` 文件中的校验值完全一致。如果文件名不同，请将示例命令中的文件名替换为实际下载的文件名。

### 安装步骤

1. 打开已经校验的 `.dmg`。
2. 将 `选片APP.app` 拖到镜像中的“Applications（应用程序）”快捷方式。
3. 推出磁盘映像。
4. 从“应用程序”文件夹打开已经复制的应用。

安装过程不会运行管理员安装脚本。0.1.0 测试版使用本地临时签名，**尚未经过 Apple 公证**，因此 macOS 可能警告或阻止应用打开。请勿关闭 Gatekeeper 或其他系统级安全保护。如果不希望运行未经公证的测试软件，可以选择从源码运行，或等待后续公证版本。

安装包声明的最低系统目标为 macOS 26.0，实际测试版本以 Release 说明为准。首次使用时建议先备份重要照片，并使用少量照片副本试运行。分析和保留/待定/舍弃标记不会改写或移动原图；只有明确选择“移动文件”导出时才会改变文件位置。移动文件或覆盖同名文件前，应用会显示二次确认。

相关说明：[隐私与数据处理](../PRIVACY.md)、[分析限制](../README.md#分析范围与限制)、[从源码构建](building-macos.md)。

---

<a id="english"></a>

## English

XuanPian 0.1.0 is a beta for Apple Silicon (`arm64`) Macs. Intel Macs and Windows are not supported by this package. Python, Qt, Perl/ExifTool, and the analysis models are included in the app; normal installation does not require Homebrew or a first-run download.

### Before opening the download

Download the `.dmg` and published SHA-256 checksum from the same GitHub Release. In Terminal, verify the file before opening it:

```bash
shasum -a 256 XuanPian-0.1.0-arm64-local-ad-hoc.dmg
```

The result must exactly match the checksum shown on the release page and in the `.sha256` file. If the filename differs, replace the example filename with the downloaded filename.

### Install

1. Open the verified `.dmg`.
2. Drag `选片APP.app` to the `Applications` shortcut.
3. Eject the disk image.
4. Open the copied app from Applications.

There is no administrator installer script. The 0.1.0 beta is locally ad-hoc signed and is **not Apple-notarized**, so macOS may warn or prevent it from opening. Do not disable Gatekeeper or other system-wide security controls. If you are not comfortable running a non-notarized beta, build from source or wait for a future notarized release.

The packaged binary declares macOS 26.0 as its minimum target; the release notes state the versions actually tested. Before importing irreplaceable work, keep a backup and try a small copied folder first. Analysis and keep/maybe/discard marks do not rewrite or move originals. File locations change only through an explicit move export, and the app asks for confirmation before moving files or overwriting same-name files.

See [privacy and data handling](../PRIVACY.md), [analysis limitations](../README.md#analysis-scope-and-limitations), and [building from source](building-macos.md).
