; 小爱宠物语音控制 · v3 客户端安装器脚本（Inno Setup 6）
; 用法：
;   1. 先运行 client/build_runtime.py 组装 build/XiaoAiPetControl/
;   2. 用 Inno Setup Compiler（ISCC.exe）编译本脚本：
;        ISCC.exe client\installer.iss
;   3. 产出 build/Output/小爱宠物语音控制-Setup-<版本>.exe

#define AppName "小爱宠物语音控制"
#define AppVersion "3.0.0"
#define AppPublisher "xiaomi-voice-pet-control"
#define DistDir "..\build\XiaoAiPetControl"

[Setup]
AppId={{8E4B2C91-6D3A-4F5E-9C1B-XIAOAIPETV3}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\XiaoAiPetControl
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\runtime\pythonw.exe
OutputDir=..\build\Output
OutputBaseFilename=小爱宠物语音控制-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; HA 配置含令牌，卸载时不删数据目录以外的内容由用户自己决定
UninstallFilesDir={app}\_uninstall

[Files]
; 整个便携目录原样进安装包（runtime + app + config + 客户端.vbs + 使用说明）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: """{app}\app\tray.pyw"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\icon.ico"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: """{app}\app\tray.pyw"""; WorkingDir: "{app}\app"; \
  IconFilename: "{app}\app\icon.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
  GroupDescription: "附加任务："
Name: "autostart"; Description: "开机自动启动（推荐）"; \
  GroupDescription: "附加任务："; Flags: checkedonce

[Registry]
; 开机自启：写 HKCU Run，指向托盘
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "XiaoAiPetControl"; \
  ValueData: """{app}\runtime\pythonw.exe"" ""{app}\app\tray.pyw"""; \
  Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\runtime\pythonw.exe"; \
  Parameters: """{app}\app\tray.pyw"""; WorkingDir: "{app}\app"; \
  Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 卸载前尝试结束托盘与 HA（存在则杀，不存在则忽略）
Filename: "{cmd}"; Parameters: "/C taskkill /IM pythonw.exe /F & exit 0"; \
  Flags: runhidden; RunOnceId: "KillTray"

[UninstallDelete]
; 保留用户数据：app/ 下的令牌与口令、config/ 的 HA 配置都不删。
; 如需完全清除，请手动删除安装目录。
