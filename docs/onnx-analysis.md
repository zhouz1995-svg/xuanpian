# 离线主体分析 / Offline subject analysis

当前开发源码使用 ONNX Runtime 1.19.2 CPU、YuNet 2023mar 和 YOLOX-Nano 0.1.1rc0。两个模型总计约 3.9 MB，运行时不下载模型。模型文件和来源记录在 [模型清单](../src/xuanpian/resources/models/manifest.json)。

人脸框、眼鼻关键点与人物框用于选择主体内部区域。相机提供有效 AF 区域时，仍优先使用 AF 双尺度判断。没有可用 AF 时，人脸使用眼鼻附近区域，人物使用上半身区域，其他物体使用中央 70% 区域。多个同等重要的主体、低纹理或检测故障会保守返回“建议复查”。

技术结果版本升级为 `technical-v8`。旧版本结果仍能读取，但需要重新分析才能作为当前结果使用；人工标记与项目结构不变。

## 平台范围

- 推理和图像运算使用 Python、ONNX Runtime、NumPy、Pillow、rawpy，不使用 Apple Vision 或 OpenCV。
- ONNX Runtime 1.19.2 提供 macOS 11+ universal2 轮子；1.20.1 和 1.23.2 的官方轮子要求 macOS 13，因此兼容基线固定为 1.19.2。
- Qt 6.7.3、PyQt 6.7.1、NumPy 2.2.6、Pillow 11.3.0 和 rawpy 0.24.0 固定为这一轮测试的依赖。
- 完整应用仍需在 macOS 12 Intel/arm64 和 Windows 10/11 x64 上验证。新的 arm64 打包目标为 macOS 12，并要求重新构建 Perl、选择兼容的 NumPy 轮子及检查全部原生组件；旧的 macOS 26 安装包不因此获得兼容性。构建目标不等于旧系统实机验收。
- HEIC/AVIF 保留现有可选 pillow-heif 插件；只解码发行组件另行处理。

## 开发验证

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[test,build,formats]'
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
.venv/bin/python scripts/vendor_analysis_models.py
```

模型恢复脚本只接受清单中固定的长度和 SHA-256，不会把下载到的任意文件重新登记为可信模型。

使用真实照片时，请将报告写到忽略的 `reports/` 目录，避免将私人路径和照片名提交到仓库：

```bash
.venv/bin/python scripts/calibrate_technical_samples.py /path/to/photos --onnx --report reports/onnx-calibration.json
```

清晰度阈值没有以“减少复查数量”为理由整体放宽。没有人工真值标签的样本对比只说明行为发生了变化，不能证明准确率提高。

本轮验证、真实照片对比和未完成的安装包工作记录在 [迁移验证记录](verification/onnx-migration.md)。

## English

The development engine uses CPU-only ONNX Runtime with two bundled, hash-verified models. YuNet supplies face boxes and five landmarks; YOLOX-Nano supplies COCO object boxes. Reliable autofocus metadata retains priority. Otherwise, focus is measured inside the selected subject; ambiguous scenes and capability failures request review.

No UI or project-schema migration is required. Stored historical results remain readable and are recomputed for technical-v8. Full native compatibility still needs target-machine tests, especially replacement of the existing macOS-26 Perl metadata runtime. HEIC codec packaging and Windows installers remain separate deliverables.
