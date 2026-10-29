# 当前依赖审查 / Current dependency review

检查日期：2026-10-28（Asia/Shanghai）。这是当前构建输入的工程核验，不是整体法律或安全放行。既有安装包仍以各自验证报告记录的源码、哈希为准；本次源码更新不会追溯改变旧二进制。

## 本轮实际推进

- 补齐 Mac 验收脚本的失败清理：检查进程使用专属 POSIX session，超时或非零退出时对其进程组先 TERM、限时等待，再 KILL。真实测试让后代进程忽略 TERM，并检查无关进程仍存活；不按进程名全局终止。修正提交为 `44510cc`，当时完整回归为 `1589 passed, 2 skipped`。不改变选片算法、界面、项目格式或原照片。
- 查询三份当前锁文件的 32 个顶层 Python 包，发现 setuptools 79.0.1 的两个独立问题。`PYSEC-2026-3447` 是首项的别名，不是第三个漏洞。
- 将构建工具统一更新到 setuptools 84.1.0，构建后端和 build extra 同时拒绝旧版本，三平台锁只改变 setuptools 的版本/批准哈希；应用运行库版本不变。
- 重新下载并验证 macOS arm64 28 个、macOS x86_64 28 个、Windows x64 31 个锁定轮子。Windows 的跨平台下载使用 `--no-deps`：pip 在 Darwin 主机上会误按宿主 marker 追加 macholib，真实 Windows 依赖一致性仍须由原生 CI 的安装与 `pip check` 验证。没有安装或执行外国架构轮子。
- Intel 输入收据更新为 28 个轮子、94 个原始 notice 成员，新增 4 个原文快照；历史 wheel-origin 记录和既有文本保留，不用新版本覆盖历史证据。独立的新 arm64 环境离线安装并通过 `pip check`，旧测试环境未改动。

两个问题的上游范围分别是：macOS sdist 的 Unicode 规范化排除绕过（83.0.0 修复）以及 Windows legacy wheel 安装的路径逃逸（84.1.0 修复）。这是构建/安装工具风险，不能据此断言应用处理照片存在同类漏洞。[sdist 公告](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c)、[83.0.0 修复日志](https://setuptools.pypa.io/en/latest/history.html#v83-0-0)、[legacy wheel 公告](https://github.com/pypa/setuptools/security/advisories/GHSA-xmj3-9gf7-h52w)

setuptools 的目标纯 Python 轮子为 `setuptools-84.1.0-py3-none-any.whl`，PyPI 公布且实际下载匹配的 SHA-256 为 `5231689c6ac8156cd155cfb98c3fd6fb252c3e7e9bd3d78b64d3c55f132624c9`；发行文件未被 yanked，要求 Python >=3.10，与当前 3.11 匹配。[PyPI 发行数据](https://pypi.org/pypi/setuptools/84.1.0/json)

新隔离的 arm64 环境使用 macOS 12 兼容目标轮子、Python 3.11.15、setuptools 84.1.0，完整回归为 `1589 passed, 2 skipped`（44.68 秒），包括实际干净源码 wheel 构建及 notice 检查。三平台独立解析得到的版本集合与各自锁文件一致；这不替代待完成的原生安装包构建。

## 安全查询的明确范围

升级后的 32 个顶层包，加上从上述 setuptools 轮子 METADATA 提取的 12 个内嵌 Python 依赖，共 44 个名称/版本组合，经 OSV PyPI `querybatch` 查询未返回匹配记录。可复核的输入、锁文件摘要与结果保存在 [查询快照](dependency-security-2026-10-28.json)。

这只是查询当日、所选数据库及包版本的结果，不是“没有漏洞”证明。它不覆盖 Qt、LibRaw、HEIF 编解码器、CPython/Perl、Windows DLL、安装器、模型训练数据或其他未列出的内嵌代码；也不是对 GitHub 默认分支全部历史依赖告警的关闭。构建工具和测试包不自动等于应用内实际分发组件。

源码审计的 23 个 provenance/binary review 项是二进制来源复核标记，不等于 23 个漏洞或 23 项未授权。图标已有作者确认；当前模型范围仅有两个 ONNX 文件。旧 MediaPipe/OpenCV 条目属于历史版本，不应反复当成当前依赖。

## 当前证据与剩余工作

| 范围 | 已确认的证据 | 尚需闭合的发行工作 |
| --- | --- | --- |
| PyQt6 6.7.1 / Qt 6.7.3 | PyQt 为 GPLv3/商业双许可；GPL 轮子配 LGPL Qt。已保留当前轮子 notice，不沿用旧 r5 的版本结论。 | 对实际 Qt 模块和内嵌库逐项归源；准备对应源码、修改记录和适用的替换/重新链接说明。仅公开应用源码不足以自动完成这些事项。[Riverbank](https://www.riverbankcomputing.com/software/pyqt/)、[Qt 义务说明](https://www.qt.io/development/open-source-lgpl-obligations) |
| pillow-heif 1.5.0 / HEIF 原生库 | 外层源码 BSD-3-Clause；实际轮子的 bundled notice 却声明 GPLv2 并引用旧库版本。Intel 轮子实际含 `libheif.1.23.1.dylib`、`libx265.216.dylib`、`libde265.0.2.1.dylib`。 | 优先核对这些精确二进制的源码、授权选项、兼容性及适用专利条件。既不能用外层 BSD 覆盖内嵌库，也不能仅凭旧 notice 判断已兼容或不兼容。[外层许可证](https://github.com/bigcat88/pillow_heif/blob/v1.5.0/LICENSE.txt)、[实际轮子 notice](../../packaging/licenses/wheels/200ce164421a7ec6f0742dcacf100551847ad7b16ac58e6f30dd0f36fd8fcf7e.txt) |
| rawpy 0.24.0 / LibRaw | 上游说明包装层为 MIT，轮子不包含 GPL demosaic packs；轮子中保留了 LibRaw 的 LGPL-2.1 文本。 | 核对当前各平台 LibRaw 及其 LCMS/JPEG 等实际二进制版本、对应源码与通知，不把包装层许可当作全部闭合。[固定版本上游说明](https://github.com/letmaik/rawpy/blob/v0.24.0/README.md) |
| NumPy / Pillow / ONNX Runtime | 当前轮子和已知第三方 notice 有哈希证据；Pillow 已为 13.0.0。 | 逐平台核对实际冻结包的 OpenBLAS、图像编解码器、ORT 内嵌库和对应版本；顶层 OSV 结果不能覆盖这一层。 |
| YuNet | 固定 OpenCV Zoo commit 的模型目录 MIT 许可和精确权重哈希已保存。 | 分发时保留版权与许可通知；不扩大为所有人脸数据或模型的授权结论。[固定许可文本](https://github.com/opencv/opencv_zoo/blob/48b10fa505aeb51fbd127fc60a5c660040e823b7/models/face_detection_yunet/LICENSE) |
| YOLOX-Nano | 官方 `0.1.1rc0` release 资产、精确哈希及同 tag Apache-2.0 文本已保存。 | 未找到单独的权重专属模型卡；保持 partial，不把仓库许可重新描述为独立的权重专属授权。参阅 [模型来源](../model-provenance.md)。 |
| macOS Perl / ExifTool | 版本、源码归档哈希、编译工具链、原始许可及两套上游测试均有记录。 | 为实际发行归档组织精确对应源码和构建材料，不能只给一个上游首页。 |
| Windows ExifTool / Strawberry Perl / MSVC DLL | 原始 runtime 及 `Licenses_Strawberry_Perl.zip` 已保留，DLL 输入来源固定。 | 仍需逐 DLL 的许可和适用再分发条件核对；Windows 10/11 真机验收保持暂缓。 |
| FlatBuffers / 构建工具 notice | 固定 FlatBuffers tag 的 Apache-2.0 文本可定位，轮子自身无独立许可证成员；setuptools 新轮子及其 vendored notice 已保存。 | 将缺失的上游归属材料补入实际发行材料；不把构建环境完整包列表直接写成应用 SBOM。[FlatBuffers 固定许可](https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE) |

## HEIF 发布来源核验（2026-10-28）

在独立工具环境中使用 PyPI 推荐的 `pypi-attestations 0.0.30`，对三份实际锁定轮子执行 `verify pypi --repository https://github.com/bigcat88/pillow_heif <本地轮子>`；三次均退出 0 并输出 `OK`。这是发布身份与文件完整性的密码学验证，不是仅解析证明 JSON，也未安装或执行外国架构代码。[PyPI 核验方法](https://docs.pypi.org/attestations/consuming-attestations/)

| pillow-heif 1.5.0 / CPython 3.11 轮子目标 | 实际 SHA-256 | 发布证明核验 |
| --- | --- | --- |
| macOS 11+ arm64 | `88d74a74b144c2e6e99e18e23a1637aef5f63ad32c718c8358da87ea81f94194` | 通过 |
| macOS 10.15+ x86_64 | `e07ad41fde21396c01b14f2f3026b26c85a4d8696e7b9514b6f43b29f9cc08fa` | 通过 |
| Windows amd64 | `d3cb8c18e5939839cf76a1617f4df5e0b497d1e3a8260b273590168e72d7f8f8` | 通过 |

Intel 发布证明关联的 workflow 为 `wheels.yml`，源码 commit 为 `16c3dd8249f56fa07ab4f1350bd73e7a20b95bb1`（`v1.5.0`）。该固定 commit 的 macOS/Linux 构建脚本指向 x265 4.2、libheif 1.23.1 和 libde265 1.1.1；Intel 轮子内 x265 静态字符串为 `4.2+1-e444744`。这为旧 notice 的版本偏差提供了进一步线索，但 dylib ABI 文件名不等于源码版本。[固定构建脚本](https://github.com/bigcat88/pillow_heif/blob/16c3dd8249f56fa07ab4f1350bd73e7a20b95bb1/libheif/build_libs.py)

上述脚本未校验所下载源码归档的内容哈希；PyPI Publish attestation 也不是完整的构建材料证明。因此不能据此把三个平台全部内嵌库标记为已归源，不能认定授权、对应源码、重新链接或专利条件已闭合。保留原轮子 notice 不作改写；下一步仍须核对精确编解码器源码、构建选项与发行义务，不移除 HEIF 功能。

本次源码 `3114025` 的 Windows 构建 run `40611971939`、常规测试 run `40611972008` 和 Intel run `40611971947` 均已通过。Intel 包含完整 Perl/ExifTool 上游测试、原生构建、包内检查及中文路径迁移运行；实际构建的 PR test-merge 源码为 `33deb6389b8326838fa8e0a0e85a1a22ff17e191`。这不替代下面保留的历史失败记录或旧候选身份，也不等于 macOS 12 / Windows 10、11 真机验收。

## 接下来的优先级

2026-10-29 测试修正：上述慢预检用例经 `tests/test_culling_move_integration.py` 的夹具替换入口，覆盖的是旧文件移动流程，不是当前数据库标记入口。改用工作线程身份与预检仍阻塞时 Qt 心跳可处理的断言，保留待处理状态、存储读取、取消、关闭及文件结果检查；不再把该功能测试当作 100ms 性能基准，也不宣称已达到该性能目标。预检等待由测试显式释放，`finally` 保证异常退出时释放，不再依赖一秒自动放行。临时把预检移回主线程后两例均失败，恢复生产代码后两例通过；完整本地回归 `1589 passed, 2 skipped`（35.00 秒）。没有改动应用代码，新的原生 CI 结果仍须单独确认。

进一步源码核对：x265 4.2 的官方 `source/x265.h` 声明 GPL-2.0-or-later（另有商业授权选项）；libheif 1.23.1 和 libde265 1.1.1 的公开头文件声明 LGPL-3.0-or-later。不能仅凭旧 notice 的“GPLv2”推断 x265 是 GPL-2.0-only，也不能由一个头文件推断全部源码文件或实际轮子已完成授权审查。[x265 4.2 头文件](https://bitbucket.org/multicoreware/x265_git/raw/4.2/source/x265.h)、[libheif 头文件](https://github.com/strukturag/libheif/blob/v1.23.1/libheif/api/libheif/heif.h)、[libde265 头文件](https://github.com/strukturag/libde265/blob/v1.1.1/libde265/de265.h)

Windows 的固定发布脚本使用独立的 [PKGBUILD](https://github.com/bigcat88/pillow_heif/blob/16c3dd8249f56fa07ab4f1350bd73e7a20b95bb1/libheif/windows/mingw-w64-libheif/PKGBUILD)：libheif 1.23.1 源码归档指定 SHA-256 `0de0327f60fcd47de90d5654c6fe152232738d60d84fe084ec3e0f35e03b166a`，x265/libde265 则为外部 MSYS2 依赖。此处只是脚本中的预期摘要，尚非本地下载复算；不能把 macOS 构建脚本的依赖版本直接套到 Windows 轮子。

最新源码 `682f8f0` 的常规测试和 Windows CI 通过，Intel run `40697481593` 在应用测试阶段失败：`test_slow_preflight_is_off_thread_with_immediate_pending_and_safe_close[False]` 测得 0.165 秒，未满足硬编码的 0.1 秒上限（1588 通过、1 失败、2 跳过）。本轮与上一源码仅文档不同；目前证据支持检查此测试对机器调度的敏感性，但尚未据此排除真实响应问题。该轮没有新安装包，不以旧候选通过代替最新结果；不扩大超时、跳过或自动重跑来掩盖失败。

下载后复核：run `40611971947` 的 artifact `12783959446` 实际 ZIP SHA-256 为 `d9342fa187dd1190c6fd65242c41daed05c93c4c7ec96863e354c3d5b1bdcaf4`；DMG SHA-256 为 `a423de658fef9aacc40a028dafbdb0d3603519f83e62fb54ca23702dc47c94bd`，与 sidecar、验证 JSON 和上述源码身份一致。本地只读挂载复查 135 个原生组件，x86_64 切片、最低系统版本不超过 12.0、签名、模型及通知检查全部通过；未执行 Intel 代码。

后续纯文档源码 `fb4aafb` 的常规测试及 Windows CI 通过。Intel run `40685464334` 的构建和验收步骤全部通过，但 GitHub `CreateArtifact` 接口连续五次请求超时，最终上传步骤失败；整轮仍记为失败，没有可交付的新上传产物，不把这一轮标为成功，也不修改已下载候选的身份。无需为上传错误修改应用算法或跳过测试。

1. 新锁已通过原生 Intel/Windows CI 完整安装、构建和自动验收；保留原有 Perl/ExifTool 上游测试，未删除或修改失败测试来获得绿色结果。继续保留系统真机验收门禁。
2. 先闭合 HEIF/x265 的精确源与授权差异，再核对 Qt/LibRaw 等实际原生闭包。若需更换编解码构建，应另行评估三平台兼容性；本轮不移除 HEIF 功能、不静默替换依赖。
3. 准备与实际二进制匹配的第三方源码/通知及适用替换说明，再进行系统实机、签名和正式发行门禁。项目 GPL-3.0-only 选择不重新许可第三方作品。

最新文档 HEAD 的一次 Intel 重建曾在 Perl `Benchmark.t` 失败：测速 1317/s 与 1318/s 的比较显示为 0%，触发严格大于零的断言。该失败 run `40545733881` 保持失败，不改写成通过；已验证的历史候选仍对应 [原验证记录](macos-intel-installer.md)。这不被宣称是已由进程清理或 setuptools 升级解决的问题。

## English

This engineering review separates the current 32 top-level Python packages and 12 setuptools-vendored versions from historical inventories and the shipped native closure. setuptools is raised to 84.1.0 for two distinct upstream advisories; duplicate database aliases are not counted as additional issues. Exact platform wheels and raw notices are checked, while old candidate identities and original notice evidence remain immutable.

The dated OSV snapshot returned no matches for the 44 selected Python name/version pairs. It does not clear bundled native libraries, models, installers, licensing, patents or corresponding-source duties. PyPI publisher attestations for the three exact pillow-heif wheels were cryptographically verified; this does not prove the complete native build materials or resolve stale bundled notices. HEIF/x265 version/license attribution and the actual Qt/LibRaw/native closure remain priority release work. No application feature, algorithm, database format or user photo was changed. Windows, regular test and full Intel native verification runs passed for source `3114025` (Intel test-merge source `33deb6389b8326838fa8e0a0e85a1a22ff17e191`). Windows hands-on remains deferred. No automatic merge or formal Release is authorized.
