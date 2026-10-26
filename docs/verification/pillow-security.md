# Pillow 安全升级验证 / Security-update verification

日期：2026-10-26。最终本地候选源码提交：`2bee0042af35b05c21c5fc9f24c836c449936d1a`。

## 范围

将 Pillow 11.3.0 固定升级为 13.0.0，使用上游发布的安全修复，不在应用内
自行修改 Pillow 解码器。其余 27 个锁定依赖未升级，UI、technical-v8 算法、
数据库和照片操作流程均未改变。

[官方发布说明](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)
列出了此次修复，包括此前记录的 `GHSA-5x8w-8jj5-q52h`、
`GHSA-v9pv-5gc8-w2m6`、`GHSA-332p-7hrc-92wf`、
`GHSA-w889-685w-c8fh` 和 `GHSA-8g5g-8vmr-9782`。
本次未在用户机器上执行这些漏洞的攻击样本，不将供应商修复和功能测试
描述为对每个漏洞利用条件的独立复现。

官方轮子仍覆盖 Python 3.11：arm64 macOS 11、Intel macOS 10.10 和
Windows x64。实际应用的 macOS 12 最低目标不变；轮子标签不等于旧系统验收。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 升级后完整测试 | 1438 passed，无警告 |
| 24 张既有真实样本升级前后对比 | 完整技术结果字典完全一致：17 清晰、7 建议复查、13 张人脸 |
| 原照片保护 | SHA-256 前后一致，0 项修改 |
| 格式冒烟 | 合成 HEIF、AVIF、TIFF、PNG、JPEG、WebP 实际编码后通过应用读取接口解码，尺寸和颜色符合预期 |
| 最终包内运行时 | 子进程实际报告 Pillow 13.0.0，未从源码环境补齐模块 |
| 包内真实照片分析 | 两张均成功，technical-v8 和人物检测能力正常 |
| 原生组件最低版本 | 135 个：84 个 minOS 11.0、51 个 minOS 12.0 |
| 完整 bundle 检查 | 依赖路径、运行时、资源、模型、notice 和签名检查通过，0 项违规 |
| 离屏启动 | 10 秒后仍正常运行，样式资源正常加载 |
| DMG 校验和只读挂载复制 | 通过；复制后的应用再次通过完整静态检查 |

1436 项既有测试首先在独立的 Pillow 13 环境通过，未要求修改应用代码。
另新增两项本轮发现的原生依赖检查器回归测试：新版 FreeType 与 HarfBuzz
互相依赖时，检查器原先因不断增长的继承 runpath 列表无限递归。修复只终止
当前加载链上的重复展开，仍检查外部依赖和不同加载上下文。回归验证循环里
的外部依赖仍被拒绝，没有为新版依赖跳过安全检查。

CI 的真实 make 测试原先使用虚构 SDKROOT；在 full-Xcode runner 上失败。
现改用 xcrun 查询的真实 SDK，保留 MAKEFLAGS 注入及目标 12.0 断言。
安全升级提交 `616ce1c` 已通过 macOS arm64 全量测试、Intel Mac 和 Windows
引擎测试；最终合并必须通过 [PR #2 的最新检查](https://github.com/zhouz1995-svg/xuanpian/pull/2/checks)。

独立只读审查核验了全部 83 个 Pillow 发布摘要、其他锁定项未变、许可证
逐字节一致、当前加载链循环保护以及合成测试数据的 hash-bound 审计豁免。
未发现需要阻断继续验证的问题。

## 本地候选

`选片APP-0.1.0-pillow13-macos12-arm64-local-ad-hoc.dmg`

- 大小：123173699 字节。
- SHA-256：`f62467d21770fef5ba100ee5b449b837c9f955bb641a5352fb1e5da26d769e29`。
- 本地 ad-hoc 签名，未 Developer ID 签名或公证，未上传公开 Release。
- 构建采用新的目录，未覆盖旧候选、已安装应用或照片。

## 仍然成立的限制

这不是整个依赖图的安全清场；Qt、HEIF 等组件的安全和发布义务仍需单独审查。
仓库默认分支的 Dependabot 告警不会因为 PR 尚未合并而自动关闭，旧安装包
也不会被源码升级自动修复。请勿使用旧包处理不可信图片。

macOS 12 arm64 干净实机验收、Intel 安装包和 Windows 10 22H2/11 x64
安装包仍未完成；当前 CI 使用较新 macOS 和 Windows Server runner。
照片样本对比只证明本次升级未改变这些样本的结果，不证明算法准确率。

## English summary

Pillow is pinned to the upstream 13.0.0 security release; all other locked
packages and application behavior remain unchanged. The 1438-test suite,
24-photo exact-result comparison, six-format smoke checks, frozen Pillow-version
and photo-analysis checks, bundle inspection, startup and mounted-DMG-copy checks
passed locally. All 135 native objects target macOS 12 or earlier. This is not
whole-application security clearance, old-OS acceptance, notarization, or a public
binary release. See PR #2 for the latest cross-platform CI gate.
