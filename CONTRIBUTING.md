# 参与贡献 / Contributing

[中文](#中文) · [English](#english)

<a id="中文"></a>

## 中文

感谢你关注 XuanPian。提交修改前，请阅读[项目许可证](LICENSE)、[第三方组件说明](THIRD_PARTY_NOTICES.md)和[隐私说明](PRIVACY.md)。请勿向提交、Issue 或 Pull Request 添加私人照片、真实文件路径、完整 EXIF、项目数据库、日志、报告、凭据或签名材料；测试请优先使用合成素材。

### 开发环境

项目使用 Apple 芯片 Mac 和 Python 3.11。请在仓库根目录创建环境并安装带哈希校验的依赖：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
```

运行完整测试：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

文档修改至少还应运行 `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_public_documents.py -q`。arm64 发布构建请遵循 [macOS 构建说明](docs/building-macos.md)。

### 提交问题或修改

请先查看[现有 Issues](https://github.com/zhouz1995-svg/xuanpian/issues)，再提交经过脱敏的问题。修改建议通过单独分支和 Pull Request 提交，并说明用户可见变化及测试结果。文档修改不得意外改变自动对焦相机映射、清晰度阈值或分类行为；分析逻辑变化必须附带针对性回归测试。

安全问题请勿提交公开 Issue，请遵循 [SECURITY.md](SECURITY.md)。

---

<a id="english"></a>

## English

Thank you for your interest in XuanPian. Before submitting a change, read the [project license](LICENSE), [third-party component notes](THIRD_PARTY_NOTICES.md), and [privacy guidance](PRIVACY.md). Do not add private photos, real paths, full EXIF, project databases, logs, reports, credentials, or signing material to commits, issues, or pull requests. Prefer synthetic fixtures.

### Development environment

Development uses an Apple Silicon Mac and Python 3.11. From the repository root, create an environment and install the hash-pinned dependencies:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r packaging/requirements-macos-arm64.lock
.venv/bin/python -m pip check
```

Run the full test suite:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Documentation changes should also run `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_public_documents.py -q`. Follow the [macOS build instructions](docs/building-macos.md) for an arm64 release build.

### Issues and pull requests

Check [existing issues](https://github.com/zhouz1995-svg/xuanpian/issues) before filing a redacted report. Submit changes from a separate branch through a pull request, and state the user-visible change and test evidence. Documentation work must not silently change the accepted AF camera mapping, focus thresholds, or classification behavior. Analysis changes require focused regression coverage.

Do not report security vulnerabilities in a public issue; follow [SECURITY.md](SECURITY.md).
