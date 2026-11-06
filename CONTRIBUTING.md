# 参与贡献 / Contributing

[中文](#中文) · [English](#english)

<a id="中文"></a>

## 中文

感谢你关注 XuanPian。提交修改前，请阅读[项目许可证](LICENSE)、[第三方组件说明](THIRD_PARTY_NOTICES.md)和[隐私说明](PRIVACY.md)。请勿向提交、Issue 或 Pull Request 添加私人照片、真实文件路径、完整 EXIF、项目数据库、日志、报告、凭据或签名材料；测试请优先使用合成素材。

### 开发环境

项目以源码分享为主，推荐原生 64 位 Python 3.11。三平台安装与启动命令见 [README](README.md#从源码运行)。在 macOS 上按本机架构选择锁文件；以下为 Apple Silicon 示例，Intel 应改用 `packaging/requirements-macos-x86_64.lock`：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
```

Windows x64 使用 README 的 PowerShell 安装步骤和 `packaging/requirements-windows-x64.lock`，不要求安装 Inno Setup、签名证书或执行打包脚本。

运行完整测试：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

文档修改至少还应运行 `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_public_documents.py -q`。若自愿制作 arm64 安装包，可参考 [macOS 构建说明](docs/building-macos.md)。

Windows 测试（PowerShell）：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:QT_QPA_PLATFORM
```

签名、公证、商店和深度二进制来源审查暂不作为日常源码贡献的前置条件；现有历史记录保留，未完成项也不改成已通过。许可证和必要第三方声明继续保留。

### 提交问题或修改

请先查看[现有 Issues](https://github.com/zhouz1995-svg/xuanpian/issues)，再提交经过脱敏的问题。修改建议通过单独分支和 Pull Request 提交，并说明用户可见变化及测试结果。文档修改不得意外改变自动对焦相机映射、清晰度阈值或分类行为；分析逻辑变化必须附带针对性回归测试。

安全问题请勿提交公开 Issue，请遵循 [SECURITY.md](SECURITY.md)。

---

<a id="english"></a>

## English

Thank you for your interest in XuanPian. Before submitting a change, read the [project license](LICENSE), [third-party component notes](THIRD_PARTY_NOTICES.md), and [privacy guidance](PRIVACY.md). Do not add private photos, real paths, full EXIF, project databases, logs, reports, credentials, or signing material to commits, issues, or pull requests. Prefer synthetic fixtures.

### Development environment

This is a source-first project. Use native 64-bit Python 3.11 and the platform instructions in [README](README.md#build-from-source). The macOS example below is for Apple Silicon; Intel uses `packaging/requirements-macos-x86_64.lock` instead:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
```

Run the full test suite:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Documentation changes should also run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_public_documents.py -q`. For an optional arm64 package, refer to the [macOS build instructions](docs/building-macos.md).

Windows x64 uses the README PowerShell instructions and `packaging/requirements-windows-x64.lock`. To test, set `$env:QT_QPA_PLATFORM = "offscreen"`, run `.venv\Scripts\python.exe -m pytest -q`, then remove the temporary setting with `Remove-Item Env:QT_QPA_PLATFORM`. Inno Setup, signing certificates and packaging scripts are not prerequisites for source contributions. Store preparation and deep binary provenance work are deferred, without relabeling unfinished historical gates as complete. Preserve licenses and required third-party notices.

### Issues and pull requests

Check [existing issues](https://github.com/zhouz1995-svg/xuanpian/issues) before filing a redacted report. Submit changes from a separate branch through a pull request, and state the user-visible change and test evidence. Documentation work must not silently change the accepted AF camera mapping, focus thresholds, or classification behavior. Analysis changes require focused regression coverage.

Do not report security vulnerabilities in a public issue; follow [SECURITY.md](SECURITY.md).
