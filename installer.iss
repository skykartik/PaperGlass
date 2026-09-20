; PaperGlass installer (Inno Setup 6). Build it with make_installer.bat
#define MyAppName "PaperGlass"
#define MyAppVersion "2.0"
#define MyAppPublisher "PaperGlass"
#define MyAppExe "PaperGlass.exe"

[Setup]
AppId={{6F1C2B7E-3D54-4A8B-9C21-5B0E7D9A4C13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=PRIVACY.txt
OutputDir=installer
OutputBaseFilename=PaperGlass-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExe}
AppMutex=PaperGlass_single_instance
CloseApplications=yes
#if FileExists("paperglass.ico")
SetupIconFile=paperglass.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start {#MyAppName} when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\PaperGlass\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "PRIVACY.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExe}"" --tray"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExe}"; Flags: runhidden; RunOnceId: "StopPaperGlass"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', '{#MyAppName}');
  if CurUninstallStep = usPostUninstall then
  begin
    if not UninstallSilent then
      if MsgBox('Also delete your PaperGlass settings?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ExpandConstant('{userappdata}\{#MyAppName}'), True, True, True);
  end;
end;
