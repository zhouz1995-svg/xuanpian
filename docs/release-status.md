# 当前交付状态 / Current delivery status

更新：2026-10-29。这里汇总开发分支进度；历史验证报告只证明各自记录的源码和产物，不会自动更新 GitHub Release。

## 当前方向：源码分享

项目以公开源码、供感兴趣的人自行运行和修改为目标，不要求上架商店或提供预编译安装包。入门使用 [README 源码步骤](../README.md#从源码运行)，开发与测试见 [贡献说明](../CONTRIBUTING.md)。签名、公证、商店准备及深入二进制溯源暂停；不再等待或追问已撤回的上游询问。已有构建脚本和证据保留为可选参考，未完成的发行事项不因此变为通过。许可证与必要第三方声明继续保留。

下表为历史安装包工程记录，不是源码运行的前置条件。

| 平台与目标 | 已有证据 | 尚缺或暂缓 |
| --- | --- | --- |
| Apple Silicon，macOS 12+ | Pillow 13 候选 DMG；包内分析、元数据、原生最低版本和镜像校验通过 | macOS 12 干净机器验收；Developer ID 签名及公证 |
| Intel Mac，macOS 12+ | 原生 `macos-15-intel` CI 候选 DMG；包内及中文路径迁移运行、135 个 Mach-O 目标切片检查和下载哈希复核通过 | macOS 12 干净机器验收；Developer ID 签名及公证；临时 CI 产物不是正式 Release |
| Windows x64，Windows 10 22H2 / 11 | 未签名候选安装器；Server 2022 CI 安装、运行、中文路径和卸载保留数据验证通过 | Windows 10/11 真机试用暂缓；Authenticode 签名及 SmartScreen 体验 |

## 接下来的工程顺序

1. 让别人能按文档安装依赖、运行应用和测试；避免把打包工具链要求误写成源码启动要求。
2. 继续改善选片功能、交互、性能和稳定性，以合成素材回归验证，保护原片与项目数据。
3. 保持源码、模型来源、许可证及第三方声明清晰；清理工作不上传个人路径、照片、凭据、临时构建和私有报告。安装包的进一步发行工程暂缓，历史细节保留在下方验证记录。

Windows 真机试用暂缓不妨碍继续源码、自动回归、Intel 构建或依赖审查工作；但恢复该项前不宣称 Windows 10/11 已通过真机验收。不自动合并 PR，不创建正式 Release。

## 验证记录

- [Pillow 13 与 Apple Silicon 候选](verification/pillow-security.md)
- [macOS 12 部署目标记录](verification/macos12-runtime.md)
- [Intel Mac 候选安装包](verification/macos-intel-installer.md)
- [当前依赖、安全查询与剩余发行工作](verification/current-dependency-review.md)
- [Windows 候选安装包](verification/windows-installer.md)
- [Mac 构建说明](building-macos.md) / [Windows 构建说明](building-windows.md)
- [当前公开下载说明](../README.md)

## English

Current direction: source sharing, so interested users can run and modify the app themselves. Start with README and CONTRIBUTING. Prebuilt installers and store publication are not required goals. Signing, notarization and deep binary provenance work are deferred; the withdrawn upstream inquiry will not be pursued. Existing packaging scripts and historical evidence remain optional references, not source-run prerequisites. Preserve licenses and required third-party notices; unfinished binary redistribution work is not declared complete. Priorities are usable setup instructions, app improvements, regression coverage and clean source sharing.

Follow-up source `1bd57d0` passed Intel run `40792952172`, Windows run `40792952145` and regular tests `40792952141`. Intel candidate upload succeeded. Its exact source and CI-reported digests are recorded in the current dependency review; this newest artifact has not yet been independently downloaded and rehashed locally. Earlier downloaded candidates retain their original identities. The legacy preflight test now checks off-owner execution and Qt event processing rather than asserting a 100ms latency target.

Apple Silicon has a locally verified Pillow 13/macOS 12-targeted candidate. The [Intel x86_64 candidate](verification/macos-intel-installer.md) passed native packaging, frozen runtime, Chinese-path relocation and downloaded-hash checks on `macos-15-intel`; this is not macOS 12 clean-machine acceptance or a Release. Its ad-hoc-signed CI artifact has 7-day retention. The unsigned Windows x64 candidate passed Server 2022 installation/runtime/uninstall checks. Windows 10/11 hands-on testing is deferred; it has not been completed.

The historical Intel candidate's post-documentation regression/export/audit/branch-review records are retained, as are the subsequent inspector-group cleanup and setuptools 84.1.0 changes. These records do not change the source-first priorities above. Zero blocking source-audit findings are not rights/security clearance. Existing releases do not acquire development-branch fixes automatically. No automatic merge or Release creation is planned.
