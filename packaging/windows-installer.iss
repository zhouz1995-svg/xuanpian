; UTF-8, Windows x64 per-user candidate. Inputs supplied by build_windows.py.
[Setup]
AppId={{D8B27373-8C95-4B67-88EF-80CE9A3FC198}
AppName=选片工作台
AppVersion={#AppVersion}
AppPublisher=XuanPian
AppPublisherURL=https://github.com/zhouz1995-svg/xuanpian
DefaultDirName={localappdata}\Programs\XuanPian
DefaultGroupName=选片工作台
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19045
LicenseFile={#ProjectRoot}\LICENSE
SetupIconFile={#ProjectRoot}\assets\icons\app-icon.ico
UninstallDisplayIcon={app}\XuanPian.exe
OutputDir={#OutputDir}
OutputBaseFilename=XuanPian-{#AppVersion}-windows-x64-unsigned
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#AppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\选片工作台"; Filename: "{app}\XuanPian.exe"
Name: "{autodesktop}\选片工作台"; Filename: "{app}\XuanPian.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\XuanPian.exe"; Description: "{cm:LaunchProgram,选片工作台}"; Flags: nowait postinstall skipifsilent
