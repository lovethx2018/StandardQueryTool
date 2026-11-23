; Inno Setup Script for StandardQueryTool v1.5.9
; 标准查询工具安装程序脚本
; 目标平台：Windows 10 / Windows 11（64 位）

#define MyAppName "标准查询工具"
#define MyAppVersion "1.5.9"
#define MyAppPublisher "黄加林"
#define MyAppURL "https://gitee.com/lovethx2024/app-update-store"
#define MyAppExeName "StandardQueryTool.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
LicenseFile=
; 绿色包由 PyInstaller 输出到 dist\，安装包成品写入 release\vX.Y.Z\
OutputDir=release\v1.5.9
OutputBaseFilename=StandardQueryTool_v1.5.9_win10_win11_py313_setup
SetupIconFile=SQT.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 2004 (19041) 及以上；Windows 11 全部版本
MinVersion=10.0.19041
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

; 覆盖安装时清掉历史遗留文件：
; 1) 旧版带版本号的主程序；
; 2) PyInstaller 5/Win7 时代的根目录运行时（新版全部收进 _internal，
;    残留的 ucrtbase/api-ms-win/python38 等会因 exe 同目录优先造成 DLL 影子）
[InstallDelete]
Type: files; Name: "{app}\StandardQueryTool_v*.exe"
Type: files; Name: "{app}\api-ms-win-*.dll"
Type: files; Name: "{app}\ucrtbase.dll"
Type: files; Name: "{app}\python3*.dll"
Type: files; Name: "{app}\*.pyd"
Type: files; Name: "{app}\base_library.zip"
Type: files; Name: "{app}\VCRUNTIME140*.dll"
Type: files; Name: "{app}\MSVCP140*.dll"
Type: files; Name: "{app}\libcrypto-1_1*.dll"
Type: files; Name: "{app}\libssl-1_1*.dll"
Type: files; Name: "{app}\libffi-7.dll"
Type: files; Name: "{app}\libopenblas*.dll"
Type: files; Name: "{app}\sqt.png"
Type: filesandordirs; Name: "{app}\PyQt5"
Type: filesandordirs; Name: "{app}\PIL"
Type: filesandordirs; Name: "{app}\certifi"
Type: filesandordirs; Name: "{app}\lxml"
Type: filesandordirs; Name: "{app}\pymupdf"
Type: filesandordirs; Name: "{app}\fontTools"
Type: filesandordirs; Name: "{app}\numpy"

[Files]
; 打包时先有 dist\ 绿色包，再跑 ISCC；成品 setup 直接进 release\
Source: "dist\StandardQueryTool_v1.5.9_win10_win11_py313\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "SQT.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\SQT.ico"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\SQT.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent