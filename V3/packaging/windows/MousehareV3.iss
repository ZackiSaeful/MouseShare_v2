#define AppName "Mousehare V3"
#define AppPublisher "Mousehare"
#define AppExeName "MousehareV3.exe"
#ifndef AppVersion
  #define AppVersion "3.0.0"
#endif

[Setup]
AppId={{27DD4B78-F8D5-471F-91E0-2E5A8D9ED105}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\MousehareV3
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
OutputDir=..\..\release
OutputBaseFilename=MousehareV3-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#AppExeName}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "..\..\dist\MousehareV3\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
