# Windows 候选安装包验证 / Windows candidate verification

日期：2026-10-27（Asia/Shanghai）。这是未签名候选的工程验证，不是正式发行或 Windows 10/11 真机使用验收。

## 可复核证据

- 分支源码提交：`fcf7226`，PR #3 以尚未合并的 macOS 安全分支为基础。
- CI 构建使用 PR 测试合并提交：`a08a687fd25b0a5e313cb3fc264d190358955378`。
- [Windows 原生构建与安装验收](https://github.com/zhouz1995-svg/xuanpian/actions/runs/40366403686/job/121725186294)：成功，191 项原生测试通过。
- 本地完整离屏测试：1479 passed, 2 skipped；跳过的是仅适用于 Windows 的 NTFS junction 和外部 SQLite 占用测试，两者均在原生 Windows CI 执行。
- Python 3.11.15、Pillow 13.0.0、`technical-v8`；依赖使用 Windows x64 哈希锁。
- 安装器：`XuanPian-0.1.0-windows-x64-unsigned.exe`。
- 安装器 SHA-256：`19f614f094d14b66450cb55c6af3027e853672ebac8f1ecf945de56328533bf6`。
- CI artifact：`XuanPian-windows-x64-unsigned-a08a687fd25b0a5e313cb3fc264d190358955378`，保留 7 天，不是长期下载入口或 GitHub Release。

候选附带 `verification.json`、`bundle-files.json`、`build-provenance.json` 和安装器校验文件。构建溯源记录编译器 banner/哈希、Windows 依赖锁、源码提交和元数据运行时清单摘要。

CI 日志中的实际编译器为 Inno Setup 6.7.1，EXE SHA-256 为 `eb6f4410c8db367a5f74127e8025ad2ccacc0afabbe783959d237df3050f97fb`；运行时清单 SHA-256 为 `15e8e8c76b82d3c9a09a75a76d557198fd7f9b8d0b1bd7d90bed025c981d0038`。

候选已下载到本地，核对了 GitHub artifact 归档 SHA-256 `e7665539ddbbf7eb946718e30531f2f3afd6d974ae00a01e786130404136c133`；实际安装器字节摘要与上述 SHA-256、校验文件及 `verification.json` 三方一致。

## 实际验证范围

1. 原生 Windows Server 2022 上执行数据库、迁移、跨进程互斥、NTFS junction 拒绝、离线模型和算法测试。
2. 在中文目录准备并运行内置 ExifTool，读取英文与中文文件名的合成 JPEG。
3. 检查全文件哈希、PE x64、必要 DLL 可见性、模型、notice、运行时及构建溯源。
4. 启动 onedir 程序，并以真实冻结子进程加载包内 Pillow、创建/关闭项目数据库、执行 technical-v8 分析；不从开发 Python 路径加载这些任务。
5. 静默安装到全新的中文目录，重复包检查、启动、冻结分析和包内 ExifTool 图片尺寸读取。
6. 静默卸载，确认程序 EXE 移除、AppData 合成数据哨兵保留、合成照片 SHA-256 不变。`source_mutations=0`。

没有读取、上传或改动用户照片。测试图像和项目均由验收脚本在临时位置生成。

## 本轮修复

- Windows 数据库生命周期锁使用 `msvcrt` 非阻塞字节锁；Mac 保留 `flock`。数据库格式、项目流程和选片算法不变。
- MSVC DLL 取自哈希锁定的 Qt wheel，并放入应用可见目录，不依赖主机 System32 中额外安装的 VC runtime。
- 安装器中文翻译从固定 Inno Setup 上游提交下载并校验摘要，不假定编译器自带非内置翻译。
- 照片路径通过 UTF-8 参数流传入 ExifTool；launcher 使用有溯源的进程级 UTF-8 清单变换，支持此次验收中的中文安装路径。没有改系统区域设置或使用 8.3 短路径。
- 验收进程超时会清理自己启动的 worker，并提供错误窗口诊断，避免无限等待。
- SQLite 测试夹具显式关闭连接，保留原有提交/回滚断言；Windows 特有的外部占用导致发布失败时，另行验证原库字节和业务数据不变、备份可用、释放占用后重试成功。生产迁移逻辑未改。

## 尚未完成的发行门禁

- Windows 10 22H2 / Windows 11 x64 干净桌面机器上的真实 GUI、照片工作流及长时间使用验收。
- Authenticode 签名、SmartScreen 体验和正式发行流程。
- Windows 原生依赖完整的许可、对应源码及安全审查；全文件检查不代表这些事项已清场。
- Intel Mac 安装包等其他目标的单独交付。现有 macOS Release 没有因本候选而更新。

公共仓库的 CI artifact 不是私密存储。短期候选不应代替正式安装文档中的 Release，也不应把 Windows Server CI 通过写成 Windows 10/11 桌面验收通过。

## English

The unsigned Windows x64 candidate passed 191 native tests plus real frozen analysis, Chinese-path metadata reads, installation, installed-app checks and data-preserving uninstall on Windows Server 2022. Local headless regression: 1479 passed, 2 Windows-only tests skipped locally and executed natively in CI.

The tested build uses PR merge revision `a08a687fd25b0a5e313cb3fc264d190358955378`; its installer SHA-256 is `19f614f094d14b66450cb55c6af3027e853672ebac8f1ecf945de56328533bf6`. Only synthetic images and projects were used. This is a short-lived, unsigned CI candidate, not a Release or Windows 10/11 clean-desktop acceptance. Signing and full third-party release review remain open.
