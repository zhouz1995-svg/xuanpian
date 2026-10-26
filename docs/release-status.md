# 当前交付状态 / Current delivery status

更新：2026-10-27。这里汇总开发分支进度；历史验证报告只证明各自记录的源码和产物，不会自动更新 GitHub Release。

| 平台与目标 | 已有证据 | 尚缺或暂缓 |
| --- | --- | --- |
| Apple Silicon，macOS 12+ | Pillow 13 候选 DMG；包内分析、元数据、原生最低版本和镜像校验通过 | macOS 12 干净机器验收；Developer ID 签名及公证 |
| Intel Mac，macOS 12+ | 原生 Intel CI 引擎测试 | Intel 专用运行时、安装包及包内完整验证；不能把引擎测试当作安装包验收 |
| Windows x64，Windows 10 22H2 / 11 | 未签名候选安装器；Server 2022 CI 安装、运行、中文路径和卸载保留数据验证通过 | Windows 10/11 真机试用暂缓；Authenticode 签名及 SmartScreen 体验 |

## 接下来的工程顺序

1. 保持现有回归检查稳定。缩略图测试已改为先监听完成信号、再启动线程，并加入真实工作立即完成的回归场景；没有改变应用功能或延长等待超时。
2. 补齐 Intel Mac 专用候选包。保持 UI、technical-v8、数据库格式和照片流程不变；采用原生 x86_64 构建，不以 arm64 包或引擎测试代替。
3. 在正式二进制发行前，完成各平台原生依赖的许可、对应源码和安全审查，以及适用的签名、系统验收和发行检查。源码审计零阻塞或功能测试通过，不等于这些门禁已完成。

Windows 真机试用暂缓不妨碍继续源码、自动回归、Intel 构建或依赖审查工作；但恢复该项前不宣称 Windows 10/11 已通过真机验收。不自动合并 PR，不创建正式 Release。

## 验证记录

- [Pillow 13 与 Apple Silicon 候选](verification/pillow-security.md)
- [macOS 12 部署目标记录](verification/macos12-runtime.md)
- [Windows 候选安装包](verification/windows-installer.md)
- [Mac 构建说明](building-macos.md) / [Windows 构建说明](building-windows.md)
- [当前公开下载说明](../README.md)

## English

Apple Silicon has a locally verified Pillow 13/macOS 12-targeted candidate; Intel Mac currently has engine CI coverage but no native installer. The unsigned Windows x64 candidate passed Server 2022 installation/runtime/uninstall checks. Windows 10/11 hands-on testing is deferred; it has not been completed.

Next: keep regression checks stable, build and verify an Intel-specific candidate, then complete the applicable third-party, signing and platform acceptance gates before a formal binary release. Existing releases do not acquire development-branch fixes automatically. No automatic merge or Release creation is planned.
