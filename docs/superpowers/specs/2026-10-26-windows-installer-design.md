# Windows x64 候选安装包设计

## 范围与选择

目标为 Windows 10 22H2 / Windows 11 x64，无需用户安装 Python、Perl、模型或联网下载运行组件。
沿用 PyInstaller onedir，使用 Inno Setup 创建按用户安装的 EXE。保留可检查的程序目录，避免 onefile 启动时反复解压；MSIX 的证书/部署流程暂不纳入本轮。界面、technical-v8、数据库和照片操作不变。

Mac 只进行源码测试与静态准备，Windows 原生包在 GitHub windows-2022 runner 构建；Server 2022 验证不冒充 Windows 10/11 实机验收。Windows 改动使用独立分支，基于尚待合并的 macOS/Pillow 安全分支。

## 元数据边界

保留 macOS runtime schema 1 及其严格路径和哈希校验。增加仅描述 Windows x64 的 schema 2，固定执行 `exiftool/exiftool.exe -config "" -charset filename=UTF8`。每个 DLL、Perl 模块、脚本与上游 notice 均列入最终文件清单；打包后缺失、篡改、额外文件或不安全路径均拒绝，不回退到宿主 ExifTool。

原始输入为官方 ExifTool 13.59 Windows x64 包，固定版本与 Mac 相同，来源和 SHA-256 为：

- https://sourceforge.net/projects/exiftool/files/exiftool-13.59_64.zip/download
- `44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec`
- 独立摘要证据：https://www.exiftool.org/checksums.txt（同时列出同版源码、32 位包、64 位包与 Mac 包）。

只在安全临时目录解压，拒绝目录穿越、Windows drive/ADS/reserved 名称、符号链接和大小写冲突；将官方 launcher 按上游说明重命名为 exiftool.exe，保留 exiftool_files 和全部附带许可证。Windows runtime 固定在程序 `_internal/metadata-runtime` 内，资源根必须属于当前程序，不接受任意 `_MEIPASS` 或链接逃逸。

Windows 子进程环境仅保留必要系统与临时目录，不继承 PERL 注入变量或用户 PATH；关闭控制台窗口，按 UTF-8 接收 JSON。Mac 环境与调用约定保持兼容。

2026-10-27 原生验收修订：照片中文名通过 UTF-8 `-@ -` 标准输入传入，拒绝 CR/LF/NUL 与相对文件名。官方 launcher 在中文安装目录下会因 ANSI 自身路径转换找不到 Perl DLL；仅给该 launcher 的已有 PE 清单添加微软支持的进程级 `activeCodePage=UTF-8`。保留 longPathAware 和全部 Perl 文件，原始 launcher 哈希与配方绑定，派生 launcher、清单和变换记录进入完整哈希清单。无需全局 UTF-8 区域设置、管理员权限、ASCII 目录别名或 8.3 短路径。Windows 10 22H2+ 满足该机制的 1903+ 下限。中文 runtime 目录的原生预检先于打包执行，安装后再次读取中文照片。

## 构建与安装

Windows 专用 hash lock、spec、builder 和验证脚本分别管理，不复用 Mac 的 arm64/Info.plist/代码签名规则。资源包含 QSS、ICO/PNG、两个 ONNX 模型、legal snapshot 和校验后的 metadata-runtime。构建输出必须是新目录，并记录源码提交、脏状态、Python、依赖锁和运行时摘要。

Inno Setup 按用户安装到 LocalAppData/Programs/XuanPian，提供开始菜单和可选桌面快捷方式；最低版本限制为 10.0.19045，目标 x64。卸载只移除安装器登记的程序文件，不删除用户项目、缓存或照片。不安装服务，不修改系统 PATH，不强制管理员，不注册文件关联。

CI 使用已安装的 Inno Setup 6 工具链并记录编译器版本/摘要，编译器不随应用分发。项目仍为 GPL-3.0-only；官方 Windows ExifTool 的 Strawberry Perl/DLL 与 notice 作为独立发行组件记录，不把既有 Mac 许可审查当成 Windows 清场。

## 验收

先红绿测试 schema、路径/篡改拒绝、环境清理、资源根和控制台/UTF-8 调用。再在 Windows 构建程序与安装器，校验全部运行时和模型摘要，生成合成中文路径照片，使用包内 ExifTool 和分析子进程执行离线验证。实际静默安装到全新临时目录、启动已安装应用、卸载并确认项目哨兵保留。只将候选作为短期 CI artifact，不上传公开 Release；不上传用户照片。保留 Windows 10/11 干净机器、SmartScreen/签名及第三方发布审查作为后续门禁。
