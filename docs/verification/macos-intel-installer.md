# Intel Mac 候选安装包验证 / Intel Mac candidate verification

记录日期：2026-10-28（Asia/Shanghai）。目标为 macOS 12.0、原生 x86_64；实际执行环境为较新系统的 `macos-15-intel` CI，不是 macOS 12 干净机器验收。此记录仅对应下列候选，不会更新历史 ARM/Windows 报告或现有 Release。

## 候选身份与可复核证据

- [PR #5](https://github.com/zhouz1995-svg/xuanpian/pull/5)，基于 `feat/windows-installer`；[成功的原生 CI run 40536403962](https://github.com/zhouz1995-svg/xuanpian/actions/runs/40536403962)。该 head 的 Intel 安装包、macOS arm64 测试、Windows 安装包、Windows 引擎及 Intel 引擎五项检查全部通过。
- 已审查的分支 head：`9cad60a2d236b0e6c03d2ee392378af2f02a9ef1`；CI 实际构建的干净 test-merge 源码：`454b05c0b4243c2f2b1e27a926eb01f9fe3cd305`，合入基线 `6b7b5c5118d5d46c2a5ca7928f1ef74758ca6a9e`。独立比较确认 head 与 test-merge 的 tree 同为 `9143a7fa2a4fb10faa2f37c982f3b4347782b68b`。后续文档提交不是此二进制的构建来源。
- DMG：`XuanPian-0.1.0-macos12-x86_64-local-ad-hoc.dmg`。
- DMG SHA-256：`de501d3acaffe2309f68ea14bfd4d87c440b32c967d295771f7ec824e9acd2ea`。实际下载字节的独立 `shasum` 与 sidecar、`verification.json` 一致。
- GitHub 临时 artifact ID：`12750633394`；ZIP 大小 `126589216` 字节，实际下载归档 SHA-256：`86b38c2a606604bf630dd2f263306bb90bd3626818c2a0d61741643714219cd8`。保留期 7 天，记录的到期时间为 `2026-11-03T16:31:21Z`，不是稳定的公开 Release 下载。

## 原生构建与验证结果

固定工具链：Xcode 16.4、SDK 15.5、Apple clang 17.0.0（clang-1700.0.13.5）、Python 3.11.15。未使用 Rosetta，不交付 universal2 包；应用 UI、technical-v8、数据库及照片流程不变。仅使用生成的合成照片和项目。

原生全量测试：`1586 passed, 2 skipped`，148.55 秒；两项跳过仅适用于 Windows。Perl 5.44.0 上游：`All tests successful`，2684 scripts、1379450 tests；自然发生可选项、平台及开发测试跳过，没有构建器强制排除。ExifTool 13.59 上游：`All tests successful`，114 files、603 tests、`Result: PASS`。Intel Perl 构建明确避免发现宿主可选库，但保留两套上游测试；ARM 旧配置、源码清单及锁文件未改动。

原生 CI 通过应用静态检查、DMG 构建及 `hdiutil verify`、offscreen 启动。原始应用和从只读挂载 DMG 经 `ditto` 复制到中文路径的应用，均通过 frozen Pillow 13.0.0、真实项目存储、technical-v8、人像评估、模块位于包内以及 ExifTool 640×480 元数据探针。`verification.json` 为 `PASS`，`source_mutations=0`；自有挂载已卸载。

下载后在 arm64 主机仅作静态复核，没有执行包内 Intel 代码：135 个 Mach-O 对象均具有所选 x86_64 切片，主程序仅 x86_64；切片 minOS 分布为 10.8: 1、10.9: 22、10.10: 24、10.13: 1、10.15: 5、11.0: 31、12.0: 51，最高 12.0。原生依赖图、身份、本地临时签名、模型与 notice 检查违规数为 0；镜像校验通过，只读自有挂载已卸载。这不是额外的 Intel 执行或旧系统验收。

包内 `build-provenance.json` 确认 x86_64、Python 3.11.15、SDK 15.5、干净的上述 test-merge 源码，以及：

| 输入或清单 | SHA-256 |
| --- | --- |
| Intel 依赖锁 | `363d7aed66c76748bf4d73cff89af2039ceab23df10214df83866d3e0c9aa439` |
| Intel 运行时源码清单 | `dacc7c889f37907b7b7d433038bcb5206410b3669df044d33e000718bc8b7e88` |
| 最终运行时清单 | `ed1d1032dbc1325fee607be2fc44b66a1303bf746455e65a11c9cd1c9ba94f82` |

## 验收边界与后续门禁

此候选仅为本地 ad-hoc 签名，无 Developer ID 签名或 Apple 公证；不应关闭 Gatekeeper。较新 Intel CI 和静态 minOS 检查不能证明 macOS 12 干净机器可用。Windows 10/11 真机试用仍暂缓。本任务不自动合并 PR，不创建正式 Release。

在 `9cad60a` 的独立本地回归为 `1586 passed, 2 skipped`（27.04 秒），同一提交的新建 allowlist 源码导出审计为 0 blocking、23 项既有 provenance/binary review；这些结果不属于后续文档 HEAD。文档提交后的全量回归、已提交源码的新导出/审计及完整分支复审仍为后续门禁。许可、来源、对应源码、重新链接及安全审查保持开放，`rights_status=unreviewed`；零阻塞和功能测试通过均不构成法律或安全放行。

参阅 [macOS 构建说明](../building-macos.md) 和 [当前交付状态](../release-status.md)。

## English

This native x86_64 candidate targets macOS 12.0 and passed packaging/runtime checks on `macos-15-intel` in the linked run. The exact source, DMG digest and provenance hashes above identify the tested binary, not a later documentation commit. Native tests passed (1586 passed, 2 Windows-only skips), as did both upstream runtime suites, frozen-app probes and the Chinese-path relocated DMG copy. Downloaded bytes matched the sidecar and verification JSON; static-only arm64 reinspection found 135 selected x86_64 Mach-O slices with minOS at most 12.0 and no inspection violations.

The artifact is temporary (7-day retention), locally ad-hoc signed, and neither Developer ID signed nor notarized. It is not a Release. macOS 12 clean-machine acceptance and Windows 10/11 hands-on acceptance are not complete. Rights/source/relinking/security review and post-documentation full regression/export/audit/branch review remain separate gates; zero blocking audit findings are not clearance. Historical candidates retain their original identities. No Rosetta, universal deliverable, automatic merge or formal Release is implied.
