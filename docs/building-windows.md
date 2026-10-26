# Windows x64 候选安装包 / Windows candidate build

目标为 Windows 10 22H2 / Windows 11 x64。构建必须在 Windows x64 上执行；
Mac 上的源码测试不等于生成或验收 Windows 安装器。候选包未 Authenticode
签名，可能触发 SmartScreen，不能当成已签名正式版。

## 构建

需要干净的 Git 提交、uv、Python 3.11.15，以及 Inno Setup 6.5+。
简体中文语言文件从 Inno Setup 6.7.1 官方源码固定提交下载并校验 SHA-256，
不依赖编译器预装非内置语言；下载地址与摘要记录在构建溯源中。
CI 使用 Windows Server 2022 的 Inno Setup 6，实际编译器摘要和
版本 banner 记录在包内 build-provenance.json；编译器不随程序安装。

在 PowerShell 中执行，输出目录必须全新：

```powershell
uv python install 3.11.15
uv venv --python 3.11.15 .venv
uv pip install --python .venv/Scripts/python.exe --only-binary :all: --require-hashes -r packaging/requirements-windows-x64.lock
.venv/Scripts/python.exe scripts/prepare_windows_runtime.py --output build/metadata-runtime
$iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
.venv/Scripts/python.exe scripts/build_windows.py --runtime build/metadata-runtime --output build/windows-candidate --iscc $iscc
.venv/Scripts/python.exe scripts/check_windows_candidate.py build/windows-candidate
```

构建阶段需要下载依赖和固定 SHA-256 的官方 ExifTool 13.59 x64 压缩包。
`prepare_windows_runtime.py --archive <zip>` 支持使用已经下载的相同压缩包，
仍必须通过摘要检查。实际程序、模型和元数据运行时全部内置，运行无需联网。
保留上游 exiftool_files 全部模块、DLL、README 和 Strawberry Perl 许可证 ZIP；
不使用宿主 Perl，不从 PATH 寻找替代工具。

中文照片路径通过 UTF-8 标准输入参数流（`-charset filename=UTF8 -@ -`）交给
ExifTool，避免 Windows ANSI 命令行转换；保留禁用外部配置的 `-config ""`。
参数流仅接受绝对文件名，并拒绝换行、回车和 NUL，防止额外参数注入。
此路径与官方 ExifTool 的 [Windows Unicode 文件名说明](https://github.com/exiftool/exiftool/blob/13.59/exiftool)
所推荐的 UTF-8 参数文件方式一致；不宣称所有特殊 Unicode 文件名均已验证。

官方 launcher 的自身目录还受 ANSI 代码页影响。原生 Windows 准备阶段使用
已锁定 PyInstaller 的资源 API，在校验过原始哈希的 launcher 内嵌清单中添加
进程级 `activeCodePage=UTF-8`，保留上游 longPathAware；不改注册表、系统区域、
安装位置、Perl DLL/脚本或许可证。该 Windows 10 1903+ 机制属于
[微软支持的 UTF-8 进程代码页方案](https://learn.microsoft.com/en-us/windows/apps/design/globalizing/use-utf8-code-page)，
处于本项目 Windows 10 22H2+ 目标之内。
原始压缩包 SHA-256 不变；`launcher-transform.json` 记录派生前后哈希和配方，
`launcher-utf8.manifest` 保存嵌入内容，两者均进入运行时全文件清单。
包检查器还核对真实 PE 清单资源。声明此配方的实际运行时必须在 Windows
准备，不能将 Mac 静态解压产物当作最终 Windows 运行时。

## 验证与交付边界

`verify_windows_bundle.py` 校验全文件清单、PE x64、所需 DLL、离线模型、notice、
运行时和构建溯源。

运行库从已锁定 Qt 轮子中补齐到 bootstrap 可见目录，不从构建主机 System32
复制 MSVC DLL。校验按具体 loader 和对应包/Perl 子进程的 DLL 目录判断依赖，
不会用任意 vendor 子目录的同名文件充数；可选 delay-load 导入不作为普通
必需导入的清场结论。
`check_windows_candidate.py` 仅在临时目录生成合成照片，验证
中文路径、包内 Pillow/technical-v8、真实 ExifTool、离屏启动、静默安装、已安装
程序及卸载后的项目数据哨兵。它不读取或上传用户照片。

安装器按用户安装到 LocalAppData/Programs/XuanPian，不修改系统 PATH，不安装
服务，不要求管理员；卸载不清理 AppData 中的项目和缓存，也不处理照片。
简体中文和英文安装界面均可选。

PR 的 Windows candidate 工作流在原生 runner 上构建、验收后上传 7 天保留的
未签名 CI 候选与 SHA-256，不创建 GitHub Release。公共仓库的 CI artifact 是
可由具有相应 GitHub 访问权限的人下载的构建产物，并非私密存储；不得放入
个人照片或私有报告。Windows 10/11 干净机器、真实 GUI、SmartScreen/签名以及
Windows 原生组件完整发布审计仍是正式发行门禁。

## English

Build natively on Windows x64 with Python 3.11.15, the hash-locked dependency
closure and Inno Setup 6.5+. The Simplified Chinese installer translation is
downloaded from a fixed upstream source revision and hash-verified, not assumed
to be installed with the compiler. The onedir app bundles models and the approved
ExifTool distribution, with no host runtime fallback. CI verifies Chinese paths,
frozen analysis, installation and data-preserving uninstall using synthetic
images only. Unsigned, short-lived CI artifacts are not a signed Release or
Windows 10/11 clean-machine acceptance.
