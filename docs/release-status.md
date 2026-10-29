# 当前交付状态 / Current delivery status

更新：2026-10-29。这里汇总开发分支进度；历史验证报告只证明各自记录的源码和产物，不会自动更新 GitHub Release。

| 平台与目标 | 已有证据 | 尚缺或暂缓 |
| --- | --- | --- |
| Apple Silicon，macOS 12+ | Pillow 13 候选 DMG；包内分析、元数据、原生最低版本和镜像校验通过 | macOS 12 干净机器验收；Developer ID 签名及公证 |
| Intel Mac，macOS 12+ | 原生 `macos-15-intel` CI 候选 DMG；包内及中文路径迁移运行、135 个 Mach-O 目标切片检查和下载哈希复核通过 | macOS 12 干净机器验收；Developer ID 签名及公证；临时 CI 产物不是正式 Release |
| Windows x64，Windows 10 22H2 / 11 | 未签名候选安装器；Server 2022 CI 安装、运行、中文路径和卸载保留数据验证通过 | Windows 10/11 真机试用暂缓；Authenticode 签名及 SmartScreen 体验 |

## 接下来的工程顺序

1. 保持现有回归检查稳定。缩略图测试已改为先监听完成信号、再启动线程，并加入真实工作立即完成的回归场景；没有改变应用功能或延长等待超时。
2. Intel Mac 专用候选及其文档后的全量回归、源码导出审计和分支复审已完成，具体提交见验证记录；较新 Intel CI 不能代替 macOS 12 干净机器验收。后续源码已补齐验收脚本的专属进程组清理，不改变 UI、technical-v8、数据库格式或照片流程。
3. [当前依赖审查](verification/current-dependency-review.md) 已定位两个 setuptools 构建工具问题并将三平台输入更新为 84.1.0；修正后的 `1bd57d0` 已通过 Intel、Windows 和常规测试 CI，Intel 新候选上传成功。旧移动流程测试改用线程身份及 Qt 心跳验证，不将功能断言混作 100ms 性能基准。下一重点是 HEIF/x265 精确来源/授权和 Qt 等原生依赖的源码、通知及安全审查，再完成适用的签名、系统验收和发行检查。源码审计零阻塞或功能测试通过不等于这些门禁已完成。

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

Follow-up source `1bd57d0` passed Intel run `40792952172`, Windows run `40792952145` and regular tests `40792952141`. Intel candidate upload succeeded. Its exact source and CI-reported digests are recorded in the current dependency review; this newest artifact has not yet been independently downloaded and rehashed locally. Earlier downloaded candidates retain their original identities. The legacy preflight test now checks off-owner execution and Qt event processing rather than asserting a 100ms latency target.

Apple Silicon has a locally verified Pillow 13/macOS 12-targeted candidate. The [Intel x86_64 candidate](verification/macos-intel-installer.md) passed native packaging, frozen runtime, Chinese-path relocation and downloaded-hash checks on `macos-15-intel`; this is not macOS 12 clean-machine acceptance or a Release. Its ad-hoc-signed CI artifact has 7-day retention. The unsigned Windows x64 candidate passed Server 2022 installation/runtime/uninstall checks. Windows 10/11 hands-on testing is deferred; it has not been completed.

The recorded Intel candidate's post-documentation regression/export/audit/branch-review gates are complete. Follow-up source fixes inspector-group cleanup and raises the build tool to setuptools 84.1.0 for two advisories; fresh native validation is separate. Next: resolve the exact HEIF/x265 and other native dependency evidence described in the current dependency review, then applicable signing and platform acceptance gates. Zero blocking source-audit findings are not rights/security clearance. Existing releases do not acquire development-branch fixes automatically. No automatic merge or Release creation is planned.
