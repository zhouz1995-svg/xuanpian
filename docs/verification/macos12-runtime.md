# macOS 12 构建目标验证 / Deployment-target verification

后续：[Pillow 安全升级验证](pillow-security.md)记录了新的 13.0.0 候选包；
本记录中的旧候选及其依赖警告仍按原始产物保留，不会自动获得更新。

验证日期：2026-10-26。应用候选源码提交：`e7bb0fbb5b691bb89e2de5c439321021054e2d5e`。

## 结论

Apple Silicon 候选包已移除此前 Perl 的 macOS 26 和 NumPy 的 macOS 14
二进制最低版本限制，当前全部原生组件的部署目标均不高于 macOS 12。
这不是 macOS 12 实机兼容认证：本地验证仍在 macOS 26.3.1(a) arm64 上完成。
Intel 安装包、Windows 安装包及旧系统干净机器验收尚未完成。

## 改动

- `runtime-sources.json` 固定 `build.deployment_target=12.0`。
- Perl Configure、编译、链接、XS 扩展和测试使用同一目标环境；清理宿主编译器、链接器及 make 覆盖参数。
- 使用 NumPy 2.2.6 的 `macosx_11_0_arm64` 轮子，未降级包版本或改变清晰度算法。
- CI 和构建说明先按 macOS 12 下载带哈希锁的轮子，再从该目录离线安装，避免按构建宿主选择 macOS 14 轮子。
- 保留该 NumPy 轮子的原始许可证及所带库声明，并记录轮子与成员摘要。
- 应用声明最低 macOS 12.0；原有原生组件检查继续拒绝比声明版本更高的组件。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 项目全量测试 | 1436 passed；3 条既有 Pillow 弃用警告 |
| Perl 上游测试 | 2684 个脚本，1379417 项测试，上游汇总成功 |
| ExifTool 上游测试 | 114 个文件，603 项测试，PASS |
| 最终应用原生组件 | 136 个：85 个 minOS 11.0，51 个 minOS 12.0 |
| 应用资源、依赖路径、模型摘要、运行时清单和 ad-hoc 签名 | PASS，0 项违规 |
| 应用离屏启动 | 10.1 秒后仍运行，样式资源正常加载 |
| 安装包内部分析子进程 | 2 张真实照片，均成功返回 technical-v8，人物检测能力已评估，合计检测到 1 张人脸 |
| 包内 Perl/ExifTool 读取真实照片 | 成功读取 5568×3712 尺寸 |
| 原始照片安全 | 分析前后 SHA-256 一致，0 项修改 |
| DMG 校验及只读挂载后复制检查 | PASS；复制后的应用再次通过完整静态检查 |

真实照片分析子进程通过打包后的可执行程序启动，其导入路径限制为应用包内
Frameworks、base_library.zip 和 lib-dynload，不从源码树补齐依赖。
两张照片只用于包内功能冒烟验证，不是算法准确率评测。

独立代码审查确认编译和 XS 链接使用 12.0；发现的继承 `MAKEFLAGS`
覆盖风险已通过真实 make 回归测试复现、修复并复核。

## 本地候选产物

安装镜像：`选片APP-0.1.0-macos12-arm64-local-ad-hoc.dmg`

- 大小：122498163 字节。
- SHA-256：`a4c5dfcbe538c8d89437fd32d9095fd7e1c2866019e4bf3d27773f69f4040ab8`。
- 仅本地 ad-hoc 签名，未 Developer ID 签名或公证，未上传公开 Release。
- 旧安装包、已安装应用和原照片均未覆盖。

## 尚未完成

本次推送时 GitHub Dependabot 报告，当前 Pillow 11.3.0 位于多个严重
安全公告的受影响版本范围内，包括 `GHSA-5x8w-8jj5-q52h`、
`GHSA-v9pv-5gc8-w2m6`、`GHSA-332p-7hrc-92wf`、
`GHSA-w889-685w-c8fh` 和 `GHSA-8g5g-8vmr-9782`。
这些告警的利用条件尚未逐项复现；不能把功能测试通过理解为安全检查通过。
依赖升级及兼容性回归是独立的优先后续工作，本候选不用于公开发行或处理
不可信来源的图片。

需要 macOS 12 arm64 实机的安装、GUI 和完整离线流程验收；仅检查 Mach-O
最低版本无法证明所有系统 API 在旧系统上都可用。Intel Mac 和 Windows
10 22H2/11 x64 的完整安装包仍是后续工作。第三方原生组件、HEIF、Qt 和
模型的发布审计约束没有因本轮技术检查而自动解除。

## English summary

The arm64 candidate now declares macOS 12.0. All 136 bundled Mach-O objects have
deployment floors of 11.0 or 12.0. The full 1436-test suite, upstream Perl/ExifTool
tests, bundle inspection, offscreen startup, two frozen photo-analysis runs,
metadata extraction, DMG checksum verification and mounted-copy inspection passed.
This is host-side deployment-target verification, not acceptance on macOS 12.
No public binary release, Intel installer, or Windows installer was produced.
